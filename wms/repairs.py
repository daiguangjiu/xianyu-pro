"""报修工单（C 档）：线上 / 线下双通道。

这个模块存在的唯一理由：**不建待修台账，货寄出去就等于丢了** ——
钱垫着、货在别人手里、没人跟进。所以「承诺时效 + 超时标记」是这里的核心。

回库必须重新检测定级，**不得沿用送修前等级**（维修过程可能引入新伤）。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .timeutil import now as _now
from .models import (
    REPAIR_C1,
    REPAIR_C2,
    REPAIR_CANCELLED,
    REPAIR_CLOSED,
    REPAIR_OPEN,
    REPAIR_RETURNED,
    REPAIR_SENT,
    REPAIR_TIMEOUT_DAYS,
    STATUS_REWORK,
    WH_PENDING,
    WmsItem,
    WmsRepairOrder,
)
from . import audit
from . import ledger
from . import state


def next_repair_no(session: Session, at=None) -> str:
    moment = at or _now()
    prefix = "BX" + moment.strftime("%y%m%d")
    cnt = int(
        session.scalar(
            select(func.count()).select_from(WmsRepairOrder).where(
                WmsRepairOrder.order_no.like(f"{prefix}%")
            )
        ) or 0
    )
    return f"{prefix}{cnt + 1:03d}"


def create(session: Session, item: WmsItem, data, operator: str = "") -> WmsRepairOrder:
    """开报修工单：C1 线上要工单号，C2 线下要网点名 + 电话（送修前必须确认受理）。"""
    if item is None:
        raise ValueError("货品不存在")
    channel = str(data.get("channel") or REPAIR_C1).upper()
    if channel not in (REPAIR_C1, REPAIR_C2):
        raise ValueError("报修渠道只支持 C1 线上 / C2 线下")
    vendor = str(data.get("vendor") or "").strip()
    ticket_no = str(data.get("ticket_no") or "").strip()
    if not vendor:
        raise ValueError("报修渠道 / 网点不能为空")
    if channel == REPAIR_C1 and not ticket_no:
        raise ValueError("C1 线上报修必须填凭证号（工单号 / 寄修单号）")
    if channel == REPAIR_C2 and not str(data.get("contact_phone") or "").strip():
        raise ValueError("C2 线下报修必须留网点电话（送修前要电话确认受理）")
    if not str(data.get("serial_no") or item.serial_no or "").strip():
        raise ValueError("报修必须录序列号 / IMEI（官方无凭证不受理）")
    if not str(data.get("fault_desc") or "").strip():
        raise ValueError("报修必须填故障描述（争议时是唯一凭证）")

    try:
        days = int(data.get("promise_days") or REPAIR_TIMEOUT_DAYS)
    except (TypeError, ValueError):
        days = REPAIR_TIMEOUT_DAYS
    days = max(days, 1)

    row = WmsRepairOrder(
        order_no=next_repair_no(session),
        item_id=item.id,
        barcode=item.barcode,
        category=item.category,
        title=item.title,
        serial_no=str(data.get("serial_no") or item.serial_no or "").strip(),
        channel=channel,
        vendor=vendor,
        ticket_no=ticket_no,
        contact_name=str(data.get("contact_name") or "").strip(),
        contact_phone=str(data.get("contact_phone") or "").strip(),
        contact_addr=str(data.get("contact_addr") or "").strip(),
        fault_desc=str(data.get("fault_desc") or "").strip(),
        photos=list(data.get("photos") or []),
        promise_days=days,
        paid_amount=float(data.get("paid_amount") or 0.0),
        status=REPAIR_OPEN,
        operator=str(operator or ""),
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(row)
    session.flush()
    audit.log(session, "repair", row.id, "repair", after=f"开单 {row.order_no}",
              operator=operator, note=f"{vendor} {ticket_no}")
    session.commit()
    return row


def send(session: Session, repair_id: int, waybill_no: str = "", operator: str = "") -> WmsRepairOrder:
    """送修出库：货品转「报修在外」（在途资产 + 垫付资金，单独计）。"""
    row = session.get(WmsRepairOrder, repair_id)
    if not row:
        raise ValueError("报修工单不存在")
    if row.status != REPAIR_OPEN:
        raise ValueError(f"「{row.status}」状态不能送修（只有待送修能发）")
    item = session.get(WmsItem, row.item_id)
    if not item:
        raise ValueError("货品不存在")
    warehouse, status = state.route_of("C")
    ledger.apply(
        session, item,
        biz_type="OUT",
        biz_id=row.order_no,
        to_warehouse=warehouse,
        to_status=status,
        operator=operator,
        remark=f"报修送修 {row.vendor}（凭证 {row.ticket_no}）",
        idempotent_key=f"REPAIR-OUT:{row.order_no}:{item.id}",
        check_transition=False,
    )
    row.status = REPAIR_SENT
    row.sent_at = _now()
    row.promise_date = _now() + timedelta(days=int(row.promise_days or REPAIR_TIMEOUT_DAYS))
    row.waybill_no = str(waybill_no or "")
    row.updated_at = _now()
    audit.log(session, "repair", row.id, "repair", after="送修出库", operator=operator,
              note=f"承诺 {row.promise_days} 天")
    session.commit()
    return row


def mark_timeouts(session: Session) -> int:
    """超时扫描（定时任务）：超过承诺时效标 timeout_flag，进「待处理」列表。"""
    rows = list(
        session.scalars(
            select(WmsRepairOrder).where(
                WmsRepairOrder.status == REPAIR_SENT,
                WmsRepairOrder.timeout_flag.is_(False),
                WmsRepairOrder.promise_date.isnot(None),
                WmsRepairOrder.promise_date <= _now(),
            )
        ).all()
    )
    for row in rows:
        row.timeout_flag = True
        row.updated_at = _now()
    if rows:
        session.commit()
    return len(rows)


def timeouts(session: Session, limit: int = 50) -> list[WmsRepairOrder]:
    """待处理：超时未回的报修（钱垫着、货在别人手里的就是这批）。"""
    return list(
        session.scalars(
            select(WmsRepairOrder)
            .where(WmsRepairOrder.status == REPAIR_SENT, WmsRepairOrder.timeout_flag.is_(True))
            .order_by(WmsRepairOrder.promise_date.asc())
            .limit(limit)
        ).all()
    )


def return_back(session: Session, repair_id: int, operator: str = "") -> WmsRepairOrder:
    """修回：货品回待检仓并**强制重新检测定级**（不允许直接回可售）。"""
    row = session.get(WmsRepairOrder, repair_id)
    if not row:
        raise ValueError("报修工单不存在")
    if row.status not in (REPAIR_SENT,):
        raise ValueError(f"「{row.status}」状态不能登记修回")
    item = session.get(WmsItem, row.item_id)
    if not item:
        raise ValueError("货品不存在")
    ledger.apply(
        session, item,
        biz_type="MOVE",
        biz_id=row.order_no,
        to_warehouse=WH_PENDING,
        to_status=STATUS_REWORK,
        operator=operator,
        remark="报修修回，强制重新检测定级（不得沿用送修前等级）",
        idempotent_key=f"REPAIR-BACK:{row.order_no}:{item.id}",
        check_transition=False,
    )
    item.need_recheck = True
    row.status = REPAIR_RETURNED
    row.returned_at = _now()
    row.timeout_flag = False
    row.updated_at = _now()
    audit.log(session, "repair", row.id, "repair", after="修回待重检", operator=operator)
    session.commit()
    return row


def close(session: Session, repair_id: int, operator: str = "", note: str = "") -> WmsRepairOrder:
    """结案：重检完成（或已转拆件）后关单。"""
    row = session.get(WmsRepairOrder, repair_id)
    if not row:
        raise ValueError("报修工单不存在")
    item = session.get(WmsItem, row.item_id)
    if item and str(item.status or "").upper() == state.STATUS_OUT_REPAIR:
        raise ValueError("货还在外面（在途），先登记修回或撤单")
    row.status = REPAIR_CLOSED
    row.closed_at = _now()
    row.result_note = str(note or "")
    row.timeout_flag = False
    row.updated_at = _now()
    audit.log(session, "repair", row.id, "repair", after="结案", operator=operator, note=note)
    session.commit()
    return row


def cancel(session: Session, repair_id: int, reason: str = "", operator: str = "") -> WmsRepairOrder:
    """撤单：货回仓后**重新检测定级**（可能降档）。撤单必须填原因（主管审批）。"""
    row = session.get(WmsRepairOrder, repair_id)
    if not row:
        raise ValueError("报修工单不存在")
    if not str(reason or "").strip():
        raise ValueError("撤单必须填原因（主管审批用）")
    item = session.get(WmsItem, row.item_id)
    if item and str(item.status or "").upper() == state.STATUS_OUT_REPAIR:
        ledger.apply(
            session, item,
            biz_type="MOVE",
            biz_id=row.order_no,
            to_warehouse=WH_PENDING,
            to_status=STATUS_REWORK,
            operator=operator,
            remark=f"报修撤单回仓，重新检测定级（原因：{reason}）",
            idempotent_key=f"REPAIR-CANCEL:{row.order_no}:{item.id}",
            check_transition=False,
        )
        item.need_recheck = True
    row.status = REPAIR_CANCELLED
    row.closed_at = _now()
    row.result_note = str(reason or "")
    row.timeout_flag = False
    row.updated_at = _now()
    audit.log(session, "repair", row.id, "repair_cancel", after="撤单",
              operator=operator, note=reason)
    session.commit()
    return row


def list_orders(session: Session, q: str = "", status: str = "",
                only_timeout: bool = False) -> list[WmsRepairOrder]:
    stmt = select(WmsRepairOrder)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                WmsRepairOrder.order_no.like(like),
                WmsRepairOrder.barcode.like(like),
                WmsRepairOrder.vendor.like(like),
                WmsRepairOrder.ticket_no.like(like),
                WmsRepairOrder.serial_no.like(like),
            )
        )
    if status:
        stmt = stmt.where(WmsRepairOrder.status == status)
    if only_timeout:
        stmt = stmt.where(WmsRepairOrder.timeout_flag.is_(True))
    return list(session.scalars(stmt.order_by(WmsRepairOrder.id.desc())).all())


def stats(session: Session) -> dict:
    """报修时效：平均天数 / 超时单数 / 撤单率 / 垫付总额（管理层最该盯的两个数之一）。"""
    rows = list(session.scalars(select(WmsRepairOrder)).all())
    sent = [r for r in rows if r.status == REPAIR_SENT]
    timeout = [r for r in rows if r.timeout_flag]
    cancelled = [r for r in rows if r.status == REPAIR_CANCELLED]
    done = [r for r in rows if r.returned_at and r.sent_at]
    days = [
        (r.returned_at - r.sent_at).days for r in done
        if r.returned_at and r.sent_at
    ]
    return {
        "total": len(rows),
        "in_transit": len(sent),
        "timeout": len(timeout),
        "cancelled": len(cancelled),
        "cancel_rate": round(len(cancelled) / len(rows), 4) if rows else 0.0,
        "paid": round(sum(float(r.paid_amount or 0) for r in rows), 2),
        "paid_transit": round(sum(float(r.paid_amount or 0) for r in sent), 2),
        "avg_days": round(sum(days) / len(days), 1) if days else 0.0,
    }
