"""报修工单（C 档）：品牌官方寄回维修 / 品牌到家维修 / 自行维修三通道。

这个模块存在的唯一理由：**不建待修台账，货寄出去就等于丢了** ——
钱垫着、货在别人手里、没人跟进。所以「承诺时效 + 超时标记」是这里的核心。

状态流（2026-10-15 用户拍板）：待送出 → **送出** → **寄回**（在途回程，只记单号）
→ **确认再入库**（货真回仓才搬账）→ 结案 / 转拆件。
回库必须重新检测定级，**不得沿用送修前等级**（维修过程可能引入新伤）。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .timeutil import now as _now
from .models import (
    GRADE_D,
    REPAIR_C1,
    REPAIR_C2,
    REPAIR_CANCELLED,
    REPAIR_CH_BRAND_SEND,
    REPAIR_CHANNELS,
    REPAIR_CLOSED,
    REPAIR_OPEN,
    REPAIR_RESTOCKED,
    REPAIR_RETURNED,
    REPAIR_SENT,
    REPAIR_STATUS_LABELS,
    REPAIR_TIMEOUT_DAYS,
    STATUS_OUT_REPAIR,
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
    """开报修工单（2026-10-15 用户拍板）：通道固定三选（品牌官方寄回 / 品牌到家 /
    自行维修），SN 序列号与故障描述必填（争议时的唯一凭证），费用拆维修费 + 快递费。"""
    if item is None:
        raise ValueError("货品不存在")
    channel = str(data.get("channel") or REPAIR_CH_BRAND_SEND).upper()
    if channel not in REPAIR_CHANNELS + (REPAIR_C1, REPAIR_C2):
        raise ValueError("维修通道只能是：品牌官方寄回维修 / 品牌到家维修 / 自行维修")
    vendor = str(data.get("vendor") or "").strip()
    ticket_no = str(data.get("ticket_no") or "").strip()
    if not str(data.get("serial_no") or item.serial_no or "").strip():
        raise ValueError("维修商品的 SN 序列号必填（官方无凭证不受理）")
    if not str(data.get("fault_desc") or "").strip():
        raise ValueError("故障描述必填（争议时是唯一凭证）")

    try:
        days = int(data.get("promise_days") or REPAIR_TIMEOUT_DAYS)
    except (TypeError, ValueError):
        days = REPAIR_TIMEOUT_DAYS
    days = max(days, 1)

    repair_fee = round(float(data.get("repair_fee") or 0.0), 2)
    express_fee = round(float(data.get("express_fee") or 0.0), 2)
    paid = round(float(data.get("paid_amount") or 0.0), 2) or repair_fee + express_fee

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
        paid_amount=paid,
        repair_fee=repair_fee,
        express_fee=express_fee,
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


def return_back(session: Session, repair_id: int, operator: str = "",
                waybill_no: str = "") -> WmsRepairOrder:
    """寄回登记（2026-10-15 用户拍板）：货还在**在途回程**，只记单号不搬账 ——
    人真正点「确认再入库」才把仓与状态搬回来，避免「纸面到货、手里没货」。"""
    row = session.get(WmsRepairOrder, repair_id)
    if not row:
        raise ValueError("报修工单不存在")
    if row.status != REPAIR_SENT:
        raise ValueError(f"「{REPAIR_STATUS_LABELS.get(row.status, row.status)}」状态不能登记寄回（只有送出能寄回）")
    row.status = REPAIR_RETURNED
    row.returned_at = _now()
    if waybill_no:
        row.waybill_no = str(waybill_no)
    row.timeout_flag = False
    row.updated_at = _now()
    audit.log(session, "repair", row.id, "repair", after="寄回（在途回程）", operator=operator,
              note=f"回程单号 {waybill_no or row.waybill_no or '—'}")
    session.commit()
    return row


def restock(session: Session, repair_id: int, operator: str = "") -> WmsRepairOrder:
    """确认再入库（2026-10-15 用户拍板）：货真回到仓里才搬账 ——
    回待检仓并**强制重新检测定级**（不允许直接回可售）。"""
    row = session.get(WmsRepairOrder, repair_id)
    if not row:
        raise ValueError("报修工单不存在")
    if row.status not in (REPAIR_RETURNED,):
        raise ValueError(f"「{REPAIR_STATUS_LABELS.get(row.status, row.status)}」状态不能确认再入库（先登记寄回）")
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
        remark="报修寄回确认入库，强制重新检测定级（不得沿用送修前等级）",
        idempotent_key=f"REPAIR-BACK:{row.order_no}:{item.id}",
        check_transition=False,
    )
    item.need_recheck = True
    row.status = REPAIR_RESTOCKED
    row.updated_at = _now()
    audit.log(session, "repair", row.id, "repair", after="确认再入库（待重检）", operator=operator)
    session.commit()
    return row


def close(session: Session, repair_id: int, operator: str = "", note: str = "") -> WmsRepairOrder:
    """结案：重检完成（或已转拆件）后关单。"""
    row = session.get(WmsRepairOrder, repair_id)
    if not row:
        raise ValueError("报修工单不存在")
    item = session.get(WmsItem, row.item_id)
    if item and str(item.status or "").upper() == state.STATUS_OUT_REPAIR:
        raise ValueError("货还没确认回仓（在途），先「寄回 → 确认再入库」或撤单")
    row.status = REPAIR_CLOSED
    row.closed_at = _now()
    row.result_note = str(note or "")
    row.timeout_flag = False
    row.updated_at = _now()
    audit.log(session, "repair", row.id, "repair", after="结案", operator=operator, note=note)
    session.commit()
    return row


def open_order_of(session: Session, item_id) -> WmsRepairOrder | None:
    """这台货有没有还没收尾的报修单（待送出 / 送出 / 寄回 / 确认再入库）。

    一键入口靠它判两件事：重复的给「查看报修单」，没单的才给「开报修单」——
    同一台货不允许出现两张同时在走的单（钱垫两遍、回来对不上账）。
    """
    stmt = (
        select(WmsRepairOrder)
        .where(
            WmsRepairOrder.item_id == item_id,
            WmsRepairOrder.status.in_(
                (REPAIR_OPEN, REPAIR_SENT, REPAIR_RETURNED, REPAIR_RESTOCKED)
            ),
        )
        .order_by(WmsRepairOrder.id.desc())
    )
    return session.scalars(stmt).first()


def to_parts(session: Session, repair_id: int, operator: str = "",
             note: str = "") -> WmsRepairOrder:
    """报修收尾的另一条出口：官方判不可修 / 修回来还不如拆 —— 关单 + 判 D 档 + 转残值池。

    以前这步要两处跑：先在报修页结案，再跑去货品详情页点「转拆机件」，中间还夹一道
    「只有 D 档能转」的拦 —— C 档点一下就被拒，人不知道下一步干嘛。这里一次做完：

      ① 货还在外面（OUT_REPAIR）不让转 —— 手上有货才谈得上拆；
      ② 不是 D 档的先判成 D（不可修的货不能挂着 C 档继续算账），并写清理由；
      ③ 走 parts.to_parts 写 SCRAP 流水进拆机件仓；
      ④ 关单（「转拆机件」本身就是结案，别让人再点一次结案）。
    等级改过 → 全量重放流水把余额搬正（别指望增量补账，ledger.rebuild 是唯一准的）。
    """
    from . import parts as parts_service      # 延迟 import：parts 不依赖 repairs，防环

    row = session.get(WmsRepairOrder, repair_id)
    if not row:
        raise ValueError("报修工单不存在")
    if row.status in (REPAIR_CLOSED, REPAIR_CANCELLED):
        raise ValueError(f"这张单已经{REPAIR_STATUS_LABELS.get(row.status, row.status)}了，不能再转拆机件")
    item = session.get(WmsItem, row.item_id)
    if not item:
        raise ValueError("这台货已经不在库里了（可能已出库 / 核销）")
    if str(item.status or "").upper() == STATUS_OUT_REPAIR:
        raise ValueError("货还没确认回仓（在途），先「寄回 → 确认再入库」—— 手上没货拆不了件")

    reason = str(note or "").strip() or "官方判不可修 / 无维修价值"
    if str(item.grade or "").upper() != GRADE_D:
        item.grade = GRADE_D
        item.grade_reason = reason
        item.updated_at = _now()
        audit.log(session, "item", item.id, "grade", after="判 D 档（报修不可修）",
                  operator=operator, note=reason)

    parts_service.to_parts(session, item, operator=operator,
                           remark=f"报修转拆机件：{reason}")

    row.status = REPAIR_CLOSED
    row.closed_at = _now()
    row.result_note = f"转拆机件：{reason}"
    row.timeout_flag = False
    row.updated_at = _now()
    audit.log(session, "repair", row.id, "repair", after="结案转拆机件",
              operator=operator, note=f"{item.display_code} → 拆机件仓")
    session.commit()
    ledger.rebuild(session)                    # 等级变了，余额必须重放
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
