"""订单业务逻辑。

主流程：
  accept_order(payload)
    └─ 1. 查/写 DownstreamOrder（幂等）
    └─ 2. items[] 展开成 N 行（每 SKU 一行；downstream_order_id 后面追加 ::sku#idx）
    └─ 3. 实时库存复核：Σ(reserved_stock + qty) <= sku.stock
    └─ 4. SkuMapping.reserved_stock += qty（事务内）
    └─ 5. sale_price = cost × (1+rule.margin) + rule.shipping
    └─ 6. 失败 DSO 写 failed PurchaseOrder + ExceptionLog
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from sqlalchemy import update
from sqlalchemy.orm import Session

from src.models import (
    DownstreamOrder,
    ExceptionLog,
    PriceRule,
    PurchaseOrder,
    SkuMapping,
    SupplierItem,
)
from src.pricing.engine import pick_rule
from src.schemas import OrderCreateIn

logger = logging.getLogger(__name__)


# =====================================================================
# Exceptions（领域异常，便于事务正确回滚 + ExceptionLog 写库）
# =====================================================================
class OrderAcceptError(Exception):
    """accept_order 失败。"""


class SkuMappingMissing(OrderAcceptError):
    """OrderItemIn.channel_sku_id 找不到对应的 SkuMapping。"""


class InsufficientStock(OrderAcceptError):
    """实时库存复核失败：可用库存 < 下单数。"""

    def __init__(self, *, channel_sku_id: str, requested: int, available: int):
        self.channel_sku_id = channel_sku_id
        self.requested = requested
        self.available = available
        super().__init__(
            f"insufficient stock for {channel_sku_id}: requested={requested}, "
            f"available={available}"
        )


class ItemOversold(OrderAcceptError):
    """SupplierItem.is_oversold=True → 拒绝接单。"""


# =====================================================================
# DTO（内部传递，不是 Pydantic output）
# =====================================================================
@dataclass
class AcceptedOrderResult:
    """accept_order 的返回值：成功落库的 DSO + 生成的 PO + 已写 ExceptionLog。"""

    downstream_orders: list[DownstreamOrder]
    purchase_orders: list[PurchaseOrder]
    exceptions: list[ExceptionLog]


# =====================================================================
# 入口
# =====================================================================
def accept_order(
    db: Session,
    payload: OrderCreateIn,
) -> AcceptedOrderResult:
    """接收下游订单，写库并同步渔枢系统。

    （自动推单已下线：1688 代发 2026-10-13 收口，正向采购由运营在
    订单页点「推给渔枢系统采购」（src/order/flow.py::push_hub）。）

    幂等性：
    - 同一 (downstream_order_id, channel) 重复请求：保留旧 DSO，**不再**重复预占库存
      （不会创建新的 DSO；只返回已有 DSO + 已生成的 PO）
    - OrderCreateIn.items 多 SKU 时：每个 item 都创建独立 DSO，suffix `::sku#<idx>`
    """
    if not payload.items:
        raise OrderAcceptError("items is empty")

    channel = payload.channel or "xianyu"
    base_id = payload.downstream_order_id

    # ---- 1. 幂等：已有 DSO 直接返回 ----
    existing_dso = (
        db.query(DownstreamOrder)
        .filter(
            DownstreamOrder.downstream_order_id == base_id,
            DownstreamOrder.channel == channel,
        )
        .all()
    )
    if existing_dso:
        logger.info(
            "accept_order: idempotent hit downstream_order_id=%s rows=%d",
            base_id,
            len(existing_dso),
        )
        return _build_existing_result(db, existing_dso)

    # ---- 2. 展开 SKU：每个 item 一个 DSO ----
    dso_rows: list[DownstreamOrder] = []
    po_rows: list[PurchaseOrder] = []
    exceptions: list[ExceptionLog] = []

    for idx, item in enumerate(payload.items):
        per_dso_id = f"{base_id}::sku#{idx}" if len(payload.items) > 1 else base_id
        try:
            dso = _accept_one_item(
                db,
                per_dso_id=per_dso_id,
                channel=channel,
                payload=payload,
                item=item,
            )
            dso_rows.append(dso)
        except OrderAcceptError as exc:
            # 某个 SKU 失败 → 写 DSO.exception + (若有映射) failed PO + ExceptionLog
            dso, po = _write_failed_dso(
                db,
                per_dso_id=per_dso_id,
                channel=channel,
                payload=payload,
                item=item,
                exc=exc,
            )
            dso_rows.append(dso)
            if po is not None:
                po_rows.append(po)
            exceptions.append(
                _log_exception(
                    db,
                    type_="order.accept_failed",
                    payload={
                        "downstream_order_id": base_id,
                        "channel_sku_id": item.channel_sku_id,
                        "qty": item.qty,
                        "error": str(exc),
                        "reason": exc.__class__.__name__,
                    },
                )
            )
            continue

    db.flush()  # 确保 DSO 全部取得 PK
    db.commit()  # Celery worker 在另一 session 读 DSO，必须先 commit 才能可见

    # ---- 3. 同步到渔枢系统：渔货栈出的单自动在那边建档 ----
    # 以前只等飞书群里那条订单消息，群里漏发 / 没解析，渔枢系统就永远没有这笔单
    # （不会推单、不会发货，运营也看不见）。现在收单即建档，并把采购单上定下来的成本带过去。
    for dso in dso_rows:
        if dso.status == "exception":
            continue
        _sync_to_hub(db, dso)

    db.commit()

    return AcceptedOrderResult(
        downstream_orders=dso_rows,
        purchase_orders=po_rows,
        exceptions=exceptions,
    )


def _sync_to_hub(db: Session, dso: DownstreamOrder) -> None:
    """把一笔下游订单同步成渔枢系统的订单（失败不影响渔货栈接单）。"""
    try:
        from app.services.dso_sync import sync_downstream_order
    except Exception:  # noqa: BLE001  渔货栈单独部署时没有渔枢系统那套，同步通路整体不生效
        logger.info("未加载渔枢系统，跳过订单同步（downstream_order_id=%s）", dso.downstream_order_id)
        return
    try:
        order, action = sync_downstream_order(db, dso)
    except Exception as exc:  # noqa: BLE001
        # 同步失败绝不能把渔货栈的单弄丢：记下来，渔枢系统那边后面可以重跑补上
        logger.exception(
            "同步下游订单到渔枢系统失败（不影响渔货栈接单）downstream_order_id=%s: %s",
            dso.downstream_order_id,
            exc,
        )
        return
    logger.info(
        "下游订单 %s 同步到渔枢系统：action=%s order_id=%s",
        dso.downstream_order_id,
        action,
        getattr(order, "id", None),
    )


def _build_existing_result(
    db: Session, dsos: list[DownstreamOrder]
) -> AcceptedOrderResult:
    """幂等返回时，把已存在的 PO 与 ExceptionLog 一起带出。"""
    dso_ids = [d.id for d in dsos]
    pos = (
        db.query(PurchaseOrder)
        .filter(PurchaseOrder.downstream_order_id.in_([d.downstream_order_id for d in dsos]))
        .all()
    )
    return AcceptedOrderResult(
        downstream_orders=dsos,
        purchase_orders=pos,
        exceptions=[],
    )


# =====================================================================
# 失败留痕：DSO.exception + failed PO + ExceptionLog
# =====================================================================
def _write_failed_dso(
    db: Session,
    *,
    per_dso_id: str,
    channel: str,
    payload: OrderCreateIn,
    item,
    exc: OrderAcceptError,
) -> tuple[DownstreamOrder, PurchaseOrder | None]:
    """接受失败时也落 DSO.exception + (若有映射) PurchaseOrder.status=failed。"""
    # 尝试反查映射（可能本身就没映射 → 不写 PO）
    mapping = (
        db.query(SkuMapping)
        .filter(
            SkuMapping.channel_sku_id == item.channel_sku_id,
            SkuMapping.channel == channel,
        )
        .first()
    )

    raw_json = {
        "reject_reason": str(exc),
        "reason": exc.__class__.__name__,
        **payload.raw_json,
    }
    if mapping is not None:
        raw_json["ali_item_id"] = mapping.item_id
        raw_json["ali_sku_id"] = mapping.ali_sku_id

    dso = DownstreamOrder(
        downstream_order_id=per_dso_id,
        channel=channel,
        channel_sku_id=item.channel_sku_id,
        qty=item.qty,
        sale_price=Decimal("0"),
        # 异常单也把渔货价记下来：以后补资料 / 算毛利不用再回查商品
        fish_price=_fish_price_of(item_row),
        buyer_name=payload.buyer_name,
        buyer_phone=payload.buyer_phone,
        address=payload.address,
        raw_json=raw_json,
        status="exception",
        merchant_id=(item_row.merchant_id if item_row is not None else None),
    )
    db.add(dso)
    db.flush()

    po: PurchaseOrder | None = None
    if mapping is not None:
        # 用 supplier_item / sku_list 取成本
        item_row = (
            db.query(SupplierItem)
            .filter(SupplierItem.item_id == mapping.item_id)
            .first()
        )
        cost_price = Decimal("0")
        if item_row is not None:
            sku_payload = (item_row.sku_list or {}).get("skus") or []
            for raw in sku_payload:
                if str(raw.get("sku_id") or "") == mapping.ali_sku_id:
                    cost_price = Decimal(str(raw.get("price") or 0))
                    break
            if cost_price == 0:
                cost_price = Decimal(str(item_row.cost_price or 0))

        po = PurchaseOrder(
            downstream_order_id=per_dso_id,
            ali_item_id=mapping.item_id,
            ali_sku_id=mapping.ali_sku_id,
            qty=item.qty,
            cost_price=cost_price,
            status="failed",
            push_log={
                "reason": _reason_code(exc),
                "error": str(exc),
                "stage": "accept",
            },
            merchant_id=(item_row.merchant_id if item_row is not None else None),
        )
        db.add(po)
        db.flush()

    return dso, po


# =====================================================================
# 单个 SKU 的 accept（事务单元）
# =====================================================================
def _accept_one_item(
    db: Session,
    *,
    per_dso_id: str,
    channel: str,
    payload: OrderCreateIn,
    item,
) -> DownstreamOrder:
    """处理单个 OrderItemIn，落一条 DSO + 预占库存。"""
    # 1. 查 SkuMapping（一定要查 channel —— 不同渠道 channel_sku_id 不通用）
    mapping = (
        db.query(SkuMapping)
        .filter(
            SkuMapping.channel_sku_id == item.channel_sku_id,
            SkuMapping.channel == channel,
        )
        .first()
    )
    if mapping is None:
        raise SkuMappingMissing(
            f"channel_sku_id={item.channel_sku_id} channel={channel} not mapped"
        )

    # 2. 查 SupplierItem 实时复核
    supplier_item = (
        db.query(SupplierItem)
        .filter(SupplierItem.item_id == mapping.item_id)
        .first()
    )
    if supplier_item is None:
        raise OrderAcceptError(f"supplier_item {mapping.item_id} missing")
    if supplier_item.is_oversold:
        raise ItemOversold(f"item {supplier_item.item_id} is_oversold=True")

    sku_stock = _sku_stock(supplier_item, mapping.ali_sku_id)
    available = max(0, sku_stock - int(mapping.reserved_stock or 0))
    if item.qty > available:
        raise InsufficientStock(
            channel_sku_id=item.channel_sku_id,
            requested=item.qty,
            available=available,
        )

    # 3. 预占（原子条件 UPDATE）
    # 不用 "读 → 改 → 写"：并发 accept 同一 SKU 时两个事务都会读到同样的
    # reserved_stock，各自 +qty 后互相覆盖 → 只预占一份却接了两单（软超卖）。
    # 单条 UPDATE ... WHERE reserved + qty <= stock 在 SQLite / PG 下都由数据库
    # 串行化，只有一方 rowcount=1，另一方 rowcount=0 → 明确报 InsufficientStock。
    if not _reserve_stock(
        db, mapping_id=mapping.id, qty=int(item.qty), sku_stock=sku_stock
    ):
        db.expire(mapping)
        raise InsufficientStock(
            channel_sku_id=item.channel_sku_id,
            requested=item.qty,
            available=max(0, sku_stock - int(mapping.reserved_stock or 0)),
        )
    # 让后续读取（sale_price / 返回对象）拿到 DB 最新值
    db.expire(mapping)

    # 4. sale_price = cost × (1+margin) + shipping
    sale_price = _sku_sale_price(db, supplier_item, mapping, channel=channel)

    # 5. 落 DSO
    dso = DownstreamOrder(
        downstream_order_id=per_dso_id,
        channel=channel,
        channel_sku_id=item.channel_sku_id,
        qty=item.qty,
        sale_price=sale_price,
        # 渔货价（渔货栈毛利口径里的成本）：收单那一刻商品在渔货栈的拿货价，
        # 落库不回查 —— 以后改价不该改写历史单的毛利。
        fish_price=_fish_price_of(supplier_item),
        buyer_name=payload.buyer_name,
        buyer_phone=payload.buyer_phone,
        address=payload.address,
        raw_json={
            "ali_item_id": mapping.item_id,
            "ali_sku_id": mapping.ali_sku_id,
            **payload.raw_json,
        },
        status="pending",  # 推单阶段会推进到 pushed / failed / exception
        # 归属商家：决定这笔订单进谁的商家后台、结算给谁
        merchant_id=supplier_item.merchant_id,
    )
    db.add(dso)
    db.flush()
    return dso


# =====================================================================
# 工具函数
# =====================================================================
def _fish_price_of(item: SupplierItem | None) -> Decimal:
    """这一单的渔货价（渔货栈向渔货栈拿货的成本价）。

    商品没单独设渔货价就退回采购价（cost_price）—— 两个都没有才是 0（页面会显示「—」）。
    """
    if item is None:
        return Decimal("0")
    try:
        price = Decimal(str(item.fish_price or 0))
        if price <= 0:
            price = Decimal(str(item.cost_price or 0))
        return price
    except Exception:  # noqa: BLE001  脏数据不该让收单失败
        return Decimal("0")


def _sku_stock(item: SupplierItem, ali_sku_id: str) -> int:
    """SKU 的**实物库存**（权威来源：item.sku_list[i].stock）。

    取不到 SKU 粒度时退化为 item.stock（无 SKU 列表的商品）。
    """
    sku_payload = (item.sku_list or {}).get("skus") or []
    for raw in sku_payload:
        if str(raw.get("sku_id") or "") == ali_sku_id:
            return int(raw.get("stock") or 0)
    if not sku_payload:
        return int(item.stock or 0)
    return 0


def _available_stock(
    db: Session, item: SupplierItem, ali_sku_id: str
) -> int:
    """实时复核可用库存 = max(0, sku.stock - sku_mapping.reserved_stock)。

    仅用于"读"场景（如展示 / 预检）；真正的预占必须走 :func:`_reserve_stock`
    的原子条件 UPDATE，否则并发下会超卖。
    """
    sku_stock = _sku_stock(item, ali_sku_id)
    mapping = (
        db.query(SkuMapping)
        .filter(SkuMapping.item_id == item.item_id, SkuMapping.ali_sku_id == ali_sku_id)
        .first()
    )
    reserved = int(mapping.reserved_stock) if mapping else 0
    return max(0, sku_stock - reserved)


def _reserve_stock(
    db: Session, *, mapping_id: int, qty: int, sku_stock: int
) -> bool:
    """原子条件预占（并发安全）。

    执行单条::

        UPDATE sku_mappings
           SET reserved_stock = reserved_stock + :qty
         WHERE id = :mapping_id
           AND reserved_stock + :qty <= :sku_stock

    "读-判-写"由数据库一条语句完成，并发事务会被串行化：
    - 成功方 `rowcount == 1`；
    - 落败方 `rowcount == 0`（说明库存已被别人抢空）。

    注意：**不要**退化成先 `SELECT reserved_stock` 再赋值 —— 那是竞态源头。
    """
    stmt = (
        update(SkuMapping)
        .where(SkuMapping.id == mapping_id)
        .where(SkuMapping.reserved_stock + qty <= sku_stock)
        .values(reserved_stock=SkuMapping.reserved_stock + qty)
    )
    result = db.execute(stmt, execution_options={"synchronize_session": False})
    return (result.rowcount or 0) > 0


def _sku_sale_price(
    db: Session,
    item: SupplierItem,
    mapping: SkuMapping,
    *,
    channel: str,
) -> Decimal:
    """计算 SKU 的展示售价：PriceRule.margin + PriceRule.fixed_shipping。

    底价 = sku 自己的 cost (item.sku_list[i].price) ，回退到 item.cost_price。
    """
    sku_payload = (item.sku_list or {}).get("skus") or []
    sku_cost = item.cost_price
    for raw in sku_payload:
        if str(raw.get("sku_id") or "") == mapping.ali_sku_id:
            sku_cost = Decimal(str(raw.get("price") or 0))
            break

    rule = pick_rule(
        db,
        channel=channel,
        category_id=item.category_id,
        supplier_id=item.supplier_id,
    )
    if rule is None:
        margin = Decimal("0.20")
        shipping = Decimal("0")
        price_min = None
        price_max = None
    else:
        margin = Decimal(str(rule.margin_rate))
        shipping = Decimal(str(rule.fixed_shipping))
        price_min = rule.price_min
        price_max = rule.price_max

    price = sku_cost * (Decimal("1") + margin) + shipping
    if price_min is not None:
        price = max(price, price_min)
    if price_max is not None:
        price = min(price, price_max)
    return price.quantize(Decimal("0.01"))


def _log_exception(
    db: Session,
    *,
    type_: str,
    payload: dict,
) -> ExceptionLog:
    """领域错误写库（事务内禁用，需业务层 commit）。"""
    err = payload.get("error") or ""
    row = ExceptionLog(type=type_, payload=payload, error=err, retry_count=0, resolved=False)
    db.add(row)
    db.flush()
    logger.warning("exception_log type=%s err=%s", type_, err)
    return row


# =====================================================================
# 错误码 → 字符串（与 push_order 阶段一致）
# =====================================================================
_REASON_MAP = {
    "SkuMappingMissing": "sku_mapping_missing",
    "InsufficientStock": "insufficient_stock",
    "ItemOversold": "item_oversold",
}


def _reason_code(exc: OrderAcceptError) -> str:
    return _REASON_MAP.get(exc.__class__.__name__, exc.__class__.__name__)


# =====================================================================
# 查询
# =====================================================================
def get_order_with_pos(
    db: Session, downstream_order_id: str
) -> tuple[list[DownstreamOrder], list[PurchaseOrder]]:
    """查询一个聚合（group_id）下所有 DSO + PO。

    支持两种下游订单号：
    - group_id（如 "ORDER_001"）—— 返回所有 ::sku# 后缀的行
    - per-SKU id（如 "ORDER_001::sku#1"）—— 只返回那一行
    """
    dsos = (
        db.query(DownstreamOrder)
        .filter(DownstreamOrder.downstream_order_id.like(f"{downstream_order_id}%"))
        .all()
    )
    # 过滤：只取前缀匹配的（同 group_id 多 SKU 都收下）
    ids = [d.id for d in dsos]
    dso_ids_str = [d.downstream_order_id for d in dsos]
    pos = (
        db.query(PurchaseOrder)
        .filter(PurchaseOrder.downstream_order_id.in_(dso_ids_str))
        .all()
    )
    return dsos, pos


__all__ = [
    # 入口
    "accept_order",
    "AcceptedOrderResult",
    "get_order_with_pos",
    # 异常
    "OrderAcceptError",
    "SkuMappingMissing",
    "InsufficientStock",
    "ItemOversold",
]