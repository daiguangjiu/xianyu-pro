"""发货管理：渔货栈自营商品的发货单（渔枢中台 → 渔集 WMS，2026-10-15）。

供应链是渔货栈（YS001 / 名称带「渔货栈」）的订单视为渔货栈自营商品：
渔枢推单不走外部供应商，直接把**发货单**同步过来（主表字段见
``WmsDeliveryOrder``，用户定稿），仓库按发货单拣货出库；出库核销填了
运单号，物流单号自动回传渔枢发货页（bridge.apply_hub_tracking）。

* ``delivery_no`` 渔集生成、全局唯一，是幂等键；
* 同一单重推：按平台原始单号幂等，字段有变 ``version`` 递增（防乱序）；
* 状态 / 运单号跟随关联出库单（``sync_outbound_status``）。
"""
from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import audit
from .models import WmsDeliveryOrder
from .timeutil import now as _now

# 平台编码映射（设计口径：XY / XHS，认不出的原样大写）
PLATFORM_CODES = {"xianyu": "XY", "xy": "XY", "xiaohongshu": "XHS", "xhs": "XHS"}
DELIVERY_TYPES = ("快递", "自提", "同城配送")
ORDER_TYPES = ("普通", "预售", "换货", "赠品")
# 要求发货时限默认 48 小时（推单时间起算）：超时罚则的依据，WMS 处理
SHIP_DEADLINE_HOURS = 48

STATUS_LABELS = {"pending": "待拣货", "shipped": "已发货", "cancelled": "已取消"}
# 出库单状态 → 发货单状态
_OUT_STATUS_MAP = {
    "draft": "pending",
    "picking": "pending",
    "shipped": "shipped",
    "cancelled": "cancelled",
}


def next_delivery_no(session: Session, at=None) -> str:
    moment = at or _now()
    prefix = "DV" + moment.strftime("%y%m%d")
    cnt = int(
        session.scalar(
            select(func.count()).select_from(WmsDeliveryOrder).where(
                WmsDeliveryOrder.delivery_no.like(f"{prefix}%")
            )
        ) or 0
    )
    return f"{prefix}{cnt + 1:03d}"


def _split_receiver(text: str) -> dict:
    """四级地址：省 / 市 / 区县 / 街道 + 详细地址（不含省市区街道）。

    优先用渔货栈的地址解析（``parse_address_text``，region = 「省 市 区」空格分隔）；
    分端部署读不到 src 时用内置正则兜底 —— 拆不出的整段进详细地址，别丢信息。
    """
    text = str(text or "").strip()
    out = {"province": "", "city": "", "district": "", "town": "", "address": text}
    if not text:
        return out
    try:
        from src.self_purchase.address import parse_address_text  # noqa: PLC0415

        parsed = parse_address_text(text)
        parts = str(parsed.get("region") or "").split()
        if parts:
            out["province"] = parts[0][:30]
        if len(parts) > 1:
            out["city"] = parts[1][:30]
        if len(parts) > 2:
            out["district"] = parts[2][:30]
        if parsed.get("address"):
            out["address"] = str(parsed["address"])[:250]
        return out
    except Exception:  # noqa: BLE001  src 不可读 → 内置正则兜底
        pass
    m = re.match(
        r"^(.+?(?:省|自治区)|北京市|上海市|天津市|重庆市)\s*"
        r"(.+?市)?\s*(.+?(?:区|县|市))?\s*(.*)$",
        text,
    )
    if m and m.group(1):
        out["province"] = m.group(1)[:30]
        out["city"] = (m.group(2) or "")[:30]
        out["district"] = (m.group(3) or "")[:30]
        out["address"] = (m.group(4) or "").strip()[:250]
    return out


def build_payload(order, *, shop_code: str = "", deadline_hours: int | None = None) -> dict:
    """从渔枢订单提炼发货单主表字段（渔枢侧 sync 路由调用）。"""
    moment = _now()
    addr = _split_receiver(order.receiver_address)
    deal = float(getattr(order, "deal_amount", 0) or 0)
    sale = float(getattr(order, "sale_amount", 0) or 0)
    return {
        # 渔枢平台订单 ID（64 位长整型，最大 12 位数字）
        "order_no": str(order.id),
        "platform_code": PLATFORM_CODES.get(
            str(order.platform or "").lower(), str(order.platform or "XY").upper()
        )[:15],
        # 平台原始单号：回传平台发货要用，不能改（= 渔枢订单号）
        "platform_order_no": str(order.order_no or ""),
        "shop_code": str(shop_code or "")[:30],
        "create_time": moment,
        "pay_time": getattr(order, "transfer_paid_at", None),
        "ship_deadline": moment + timedelta(hours=int(deadline_hours or SHIP_DEADLINE_HOURS)),
        "receiver_name": str(order.receiver_name or "")[:60],
        "receiver_phone": str(order.receiver_phone or "")[:18],
        # 四级地址拆开存（province/city/district/town/address → receiver_* 列）
        **{f"receiver_{k}": v for k, v in addr.items()},
        "delivery_type": "快递",
        "order_type": "普通",
        "urgent_flag": 0,
        # 金额四项：对账参考，非结算依据
        "order_sales_price": sale,
        "order_cj_amount": deal or sale,
        "order_amount": deal or sale,
        "order_yunfei": float(getattr(order, "shipping_fee", 0) or 0),
        "invoice_flag": 0,
        "invoice_info": "",
        "remark": str(getattr(order, "note", "") or "")[:250],
    }


# 重推时参与比对的字段（有变才 version+1，别让人手滑多推两次把版本刷上去）
_TRACKED_FIELDS = (
    "shop_code", "receiver_name", "receiver_phone",
    "receiver_province", "receiver_city", "receiver_district", "receiver_town",
    "receiver_address", "delivery_type", "order_type", "urgent_flag",
    "order_sales_price", "order_cj_amount", "order_amount", "order_yunfei",
    "invoice_flag", "invoice_info", "remark",
)


def upsert_from_hub(session: Session, payload: dict, operator: str = "") -> tuple[WmsDeliveryOrder, bool]:
    """按平台原始单号幂等：没有就建（version=1）；重推字段有变 version+1 更新。

    返回 ``(发货单, 是否新建或更新)``。
    """
    pno = str(payload.get("platform_order_no") or "").strip()
    if not pno:
        raise ValueError("发货单缺平台原始单号")
    row = session.scalar(
        select(WmsDeliveryOrder)
        .where(WmsDeliveryOrder.platform_order_no == pno)
        .order_by(WmsDeliveryOrder.id.desc())
    )
    if row is None:
        row = WmsDeliveryOrder(
            delivery_no=next_delivery_no(session),
            version=1,
            status="pending",
            operator=str(operator or ""),
            **payload,
        )
        session.add(row)
        session.flush()
        audit.log(session, "delivery", row.id, "create",
                  after=f"{row.delivery_no} v1", operator=operator)
        session.commit()
        return row, True

    # 重推：对比关键字段，有变 version+1（已取消的单重推恢复成待拣货）
    changed = False
    if str(row.status or "") == "cancelled":
        row.status = "pending"
        changed = True
    for key in _TRACKED_FIELDS:
        new = payload.get(key)
        old = getattr(row, key)
        if key in ("urgent_flag", "invoice_flag"):
            new, old = int(new or 0), int(old or 0)
        elif key.startswith("order_") and key != "order_type":
            try:
                new = round(float(new or 0), 2)
            except (TypeError, ValueError):
                continue
            old = round(float(old or 0), 2)
        if new in (None, ""):
            continue
        if new != old:
            setattr(row, key, new)
            changed = True
    if changed:
        row.version = (row.version or 1) + 1
        row.updated_at = _now()
        audit.log(session, "delivery", row.id, "update",
                  after=f"v{row.version}", operator=operator, note="重推更新")
        session.commit()
    return row, changed


def attach_outbound(session: Session, delivery: WmsDeliveryOrder, outbound) -> None:
    """发货单挂上出库单（同步发货 = 建一张销售出库单让仓库拣）。"""
    delivery.outbound_id = int(outbound.id)
    delivery.updated_at = _now()
    audit.log(session, "delivery", delivery.id, "outbound",
              after=f"出库单 {outbound.order_no}", operator=delivery.operator)
    session.commit()


def sync_outbound_status(session: Session, outbound) -> None:
    """出库单拣货 / 核销 / 取消后把状态与运单号同步到发货单。"""
    row = session.scalar(
        select(WmsDeliveryOrder).where(WmsDeliveryOrder.outbound_id == outbound.id)
    )
    if not row:
        return
    row.status = _OUT_STATUS_MAP.get(str(outbound.status or "").lower(), row.status)
    row.tracking_no = str(outbound.waybill_no or "")
    row.express_company = str(outbound.express_company or "")
    row.updated_at = _now()
    session.commit()


def _filtered_stmt(q: str = "", status: str = ""):
    stmt = select(WmsDeliveryOrder)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                WmsDeliveryOrder.delivery_no.like(like),
                WmsDeliveryOrder.platform_order_no.like(like),
                WmsDeliveryOrder.order_no.like(like),
                WmsDeliveryOrder.receiver_name.like(like),
                WmsDeliveryOrder.receiver_phone.like(like),
                WmsDeliveryOrder.tracking_no.like(like),
            )
        )
    if status:
        stmt = stmt.where(WmsDeliveryOrder.status == status)
    return stmt


def list_orders(
    session: Session, q: str = "", status: str = "",
    limit: int | None = None, offset: int = 0,
) -> list[WmsDeliveryOrder]:
    stmt = _filtered_stmt(q, status)
    stmt = stmt.order_by(WmsDeliveryOrder.id.desc())
    if offset:
        stmt = stmt.offset(int(offset))
    if limit is not None:
        stmt = stmt.limit(int(limit))
    return list(session.scalars(stmt).all())


def count_orders(session: Session, q: str = "", status: str = "") -> int:
    stmt = _filtered_stmt(q, status)
    return int(session.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0)
