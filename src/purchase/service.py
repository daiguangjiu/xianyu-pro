"""采购推单（草稿）服务。

幂等键 = (downstream_order_id, ali_sku_id)
重复调用：直接返回已存在的 PurchaseOrder，不重复推单。

口径（2026-10-13 收口）：1688 代发推单已整体下线——
- 真推能力（AliClient.push_dropshipping_order）从未申请下来，从未真调通过；
- 正向采购走订单页「推给渔枢系统采购」（src/order/flow.py::push_hub）；
- 本服务只保留「生成采购草稿 + 人工确认付款 / 标记失败」，
  历史采购单（purchase_orders）继续作为渔枢系统成本回溯的第一档来源
  （app/services/dso_sync.py::_cost_of）。

接口：
    push_order(db, downstream_order_id)               # 生成采购草稿（订单 → 所有 SKU）
    push_order_for_dso(db, dso)                       # 单 DSO 生成草稿
    confirm_paid(db, purchase_order_id, *, by='human') # 人工付款后，回写 status=paid
    mark_failed(db, purchase_order_id, *, reason)      # 标记推单失败
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Iterable

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models import (
    DownstreamOrder,
    ExceptionLog,
    PurchaseOrder,
    SkuMapping,
)

logger = logging.getLogger(__name__)


# =====================================================================
# 领域异常
# =====================================================================
class PurchasePushError(Exception):
    """推单过程失败。"""


class OrderNotFound(PurchasePushError):
    pass


class AutoPaymentNotAllowed(PurchasePushError):
    """当前被拒接了。默认所有推单都是"init"草稿，从不真支付。"""


# =====================================================================
# 主入口
# =====================================================================
async def push_order(
    db: Session,
    downstream_order_id: str,
    *,
    auto_create: bool = False,
) -> list[PurchaseOrder]:
    """为一笔 DSO 生成采购草稿。

    入参：
        db              SQLAlchemy Session
        downstream_order_id
                        单 DSO id （如 "ORDER_001" 或 "ORDER_001::sku#0"）
        auto_create     当 DSO 不存在时是否自动创建？默认 False（要求先 accept_order）

    返回：
        list[PurchaseOrder]
            若 DSO 有 N 个 SKU（默认 1）→ list 长度 = N

    行为：
        1. 查 DownstreamOrder；不存在 → 抛 OrderNotFound（除非 auto_create=True）
        2. 取 channel_sku_id → SkuMapping → ali_item_id + ali_sku_id + item_id
        3. 幂等键 (downstream_order_id, ali_sku_id) 已有 PO → 直接返回
        4. 新 PO.status = 'init'（草稿，等人工确认付款）
    """
    dso = (
        db.query(DownstreamOrder)
        .filter(DownstreamOrder.downstream_order_id == downstream_order_id)
        .first()
    )
    if dso is None:
        if not auto_create:
            raise OrderNotFound(f"downstream_order not found: {downstream_order_id}")
        # auto_create = True 是主动部署场景，service 本轮仅用作占位
        raise OrderNotFound(f"downstream_order not found: {downstream_order_id}")

    return await push_order_for_dso(db, dso)


async def push_order_for_dso(
    db: Session,
    dso: DownstreamOrder,
) -> list[PurchaseOrder]:
    """单 DSO 的推单流程（当前 DSO 只对应 1 个 SKU → list 长度 1）。"""
    # 0. 串行化同一 DSO 的并发推单
    # PG 下 SELECT ... FOR UPDATE 会阻塞后来者（SQLite 方言忽略该子句，本就单写者）；
    # 配合 #2 的 IntegrityError 兜底，双保险避免"两个事务都过 existing is None 检查
    # → 各建一个 PO"，也避免重推时 delete + insert 互相踩踏导致 PO 丢失。
    locked = (
        db.query(DownstreamOrder)
        .filter(DownstreamOrder.id == dso.id)
        .with_for_update()
        .first()
    )
    if locked is not None:
        dso = locked

    # 1. 映射反查
    mapping = (
        db.query(SkuMapping)
        .filter(
            SkuMapping.channel_sku_id == dso.channel_sku_id,
            SkuMapping.channel == dso.channel,
        )
        .first()
    )
    if mapping is None:
        # 缺映射 → 拒绝推单
        _log_exception(
            db,
            type_="purchase.mapping_missing",
            payload={
                "downstream_order_id": dso.downstream_order_id,
                "channel_sku_id": dso.channel_sku_id,
                "channel": dso.channel,
            },
        )
        dso.status = "exception"
        db.flush()
        return []

    # 2. 幂等检查（#4a 修复：failed / init 状态允许重推）
    existing = (
        db.query(PurchaseOrder)
        .filter(
            PurchaseOrder.downstream_order_id == dso.downstream_order_id,
            PurchaseOrder.ali_sku_id == mapping.ali_sku_id,
        )
        .first()
    )
    if existing is not None:
        # 已是"成功态" → 短路返回（同 #4a 之前的行为）
        if existing.status in ("pushed", "shipped", "paid", "confirmed", "completed"):
            logger.info(
                "push_order: idempotent hit downstream=%s sku=%s po_id=%s status=%s",
                dso.downstream_order_id,
                mapping.ali_sku_id,
                existing.id,
                existing.status,
            )
            return [existing]
        # 失败 / 草稿 → 删除旧 PO，让重推 trace 清晰；ExceptionLog 里仍保留历史
        logger.info(
            "push_order: prior po_id=%s status=%s; deleting to allow retry",
            existing.id, existing.status,
        )
        db.delete(existing)
        db.flush()

    # 3. 实时库存复核（推单阶段可选，但为了"及时发现问题"，仍重检）
    from src.order.service import _available_stock  # 避免循环 import
    from src.models import SupplierItem

    item = (
        db.query(SupplierItem)
        .filter(SupplierItem.item_id == mapping.item_id)
        .first()
    )
    # ===== #3 修复说明 =====
    # 推单阶段**不再**重复做 "qty > available" 复核：
    # - accept_order 阶段已经预占 reserved 并做过可用库存检查
    # - 重复复核会导致"自己刚刚 +reserved 又自己判定自己不够"的逻辑自相矛盾
    #
    # 仅保留 SupplierItem.is_oversold 同步期标记（更权威，跨 SKU 的全局判断）。
    if item is None or item.is_oversold:
        _log_exception(
            db,
            type_="purchase.item_oversold_or_missing",
            payload={
                "downstream_order_id": dso.downstream_order_id,
                "ali_item_id": mapping.item_id,
            },
        )
        dso.status = "exception"
        # 推单发现 SKU 不可用 → 释放 accept 阶段的预占（#3）
        failed_po = _make_po(
            dso, item_id=mapping.item_id, sku_id=mapping.ali_sku_id,
            qty=dso.qty, cost_price=Decimal("0"), status="failed",
            push_log={"reason": "item_missing_or_oversold"},
        )
        _release_reserved(db, dso, failed_po)
        db.add(failed_po)
        db.flush()
        return [failed_po]

    # 4. 生成采购草稿（1688 代发已下线：不再调外部接口，只生 init 草稿等人工确认）
    cost_price = _sku_cost(item, mapping.ali_sku_id)
    push_log: dict = {"attempts": 0}
    po_status = "init"  # 草稿（待人工确认付款）

    po = _make_po(
        dso,
        item_id=mapping.item_id,
        sku_id=mapping.ali_sku_id,
        qty=dso.qty,
        cost_price=cost_price,
        status=po_status,
        push_log=push_log,
    )
    db.add(po)
    try:
        db.flush()
    except IntegrityError:
        # 并发推单：另一个事务已抢先建了同 (downstream_order_id, ali_sku_id) 的 PO。
        # UNIQUE 约束替我们兜底 → 回滚本事务并返回"赢家"，而不是把 500 抛给上游。
        db.rollback()
        winner = (
            db.query(PurchaseOrder)
            .filter(
                PurchaseOrder.downstream_order_id == dso.downstream_order_id,
                PurchaseOrder.ali_sku_id == mapping.ali_sku_id,
            )
            .first()
        )
        if winner is not None:
            logger.info(
                "push_order: lost concurrent insert; return winner po_id=%s status=%s",
                winner.id,
                winner.status,
            )
            return [winner]
        raise

    # 5. DSO 状态不在草稿阶段推进：正向采购走订单页「推给渔枢系统采购」
    # （src/order/flow.py::push_hub，按状态机流转）；这里只留采购草稿，
    # 不再写状态机外的孤儿值 pushed_pending_1688_confirm。

    db.flush()
    logger.info(
        "push_order: draft dso=%s sku=%s po_status=%s",
        dso.downstream_order_id,
        mapping.ali_sku_id,
        po_status,
    )
    return [po]


# =====================================================================
# 人工确认付款 / 标记失败
# =====================================================================
def confirm_paid(
    db: Session,
    purchase_order_id: int,
    *,
    by: str = "human",
    note: str | None = None,
) -> PurchaseOrder:
    """人工付款后，运营调用这里把 status 推进到 'paid'。

    设计：MVP 不主动支付（没有自动支付通道）。
    """
    po = db.get(PurchaseOrder, purchase_order_id)
    if po is None:
        raise OrderNotFound(f"purchase_order {purchase_order_id} not found")
    if po.status in ("paid", "shipped", "completed"):
        return po  # 幂等返回
    if po.status in ("failed",):
        raise PurchasePushError(f"po {purchase_order_id} is already failed; cannot mark paid")
    po.status = "paid"
    po.push_log = dict(po.push_log or {})
    po.push_log["confirmed_by"] = by
    po.push_log["confirmed_note"] = note
    # DSO 同步状态
    dso = (
        db.query(DownstreamOrder)
        .filter(DownstreamOrder.downstream_order_id == po.downstream_order_id)
        .first()
    )
    if dso is not None:
        dso.status = "pushed"
    db.commit()
    return po


def mark_failed(
    db: Session,
    purchase_order_id: int,
    *,
    reason: str,
) -> PurchaseOrder:
    """标记推单失败，写 exception 表，**释放 SkuMapping.reserved_stock**（#3 修复）。

    幂等：重复调已 failed 的 PO 不会再次释放预占（避免负值）。
    """
    po = db.get(PurchaseOrder, purchase_order_id)
    if po is None:
        raise OrderNotFound(f"purchase_order {purchase_order_id} not found")

    # 幂等判断：只有从非 failed → failed 的跃迁才释放预占
    was_already_failed = po.status == "failed"

    po.status = "failed"
    po.push_log = dict(po.push_log or {})
    po.push_log["failure_reason"] = reason

    dso = (
        db.query(DownstreamOrder)
        .filter(DownstreamOrder.downstream_order_id == po.downstream_order_id)
        .first()
    )
    if dso is not None:
        dso.status = "exception"
        # 释放预占（仅当不是已被 mark_failed 过的 PO）
        if not was_already_failed:
            released = _release_reserved(db, dso, po)
            if released:
                logger.info(
                    "mark_failed: released reserved downstream=%s sku=%s qty=%d",
                    po.downstream_order_id, po.ali_sku_id, po.qty,
                )

    _log_exception(
        db,
        type_="purchase.failed",
        payload={
            "purchase_order_id": po.id,
            "downstream_order_id": po.downstream_order_id,
            "reason": reason,
        },
    )
    db.commit()
    return po


def _release_reserved(db: Session, dso: DownstreamOrder, po: PurchaseOrder) -> bool:
    """归还预占：根据 (ali_sku_id, channel) 反查 SkuMapping 并扣减 reserved_stock。

    返回是否真的扣减了（False = 幂等跳过 / 无映射 / 余额不足）。

    幂等标记挂在 ``dso.raw_json['reservation_released']``，**不能**挂在 PO 上：
    重推会先 delete 旧 failed PO 再 insert 新 PO，PO 身上的标记就丢了 →
    新 PO 再次 mark_failed 会第二次扣减，把别人的预占也扣掉。

    并发安全：用条件 UPDATE（``reserved_stock >= qty``）扣减，避免两个请求同时
    释放同一份预占把 reserved_stock 扣成负数。
    """
    from sqlalchemy.orm.attributes import flag_modified

    raw = dict(dso.raw_json or {})
    if raw.get("reservation_released"):
        logger.info(
            "_release_reserved: already released (idempotent) downstream=%s sku=%s",
            dso.downstream_order_id,
            po.ali_sku_id,
        )
        return False

    mapping = (
        db.query(SkuMapping)
        .filter(
            SkuMapping.ali_sku_id == po.ali_sku_id,
            SkuMapping.channel == dso.channel,
        )
        .first()
    )
    if mapping is None:
        return False

    qty = int(po.qty)
    stmt = (
        update(SkuMapping)
        .where(SkuMapping.id == mapping.id)
        .where(SkuMapping.reserved_stock >= qty)
        .values(reserved_stock=SkuMapping.reserved_stock - qty)
    )
    result = db.execute(stmt, execution_options={"synchronize_session": False})
    if (result.rowcount or 0) == 0:
        logger.warning(
            "_release_reserved: skip (reserved < qty) downstream=%s sku=%s qty=%d",
            po.downstream_order_id,
            po.ali_sku_id,
            qty,
        )
        return False

    # 打幂等标记（跨 PO 重建存活）
    raw["reservation_released"] = True
    dso.raw_json = raw
    flag_modified(dso, "raw_json")
    db.flush()
    return True


# =====================================================================
# 工具
# =====================================================================
def _make_po(
    dso: DownstreamOrder,
    *,
    item_id: str,
    sku_id: str,
    qty: int,
    cost_price: Decimal,
    status: str,
    push_log: dict,
    ali_order_id: str | None = None,
) -> PurchaseOrder:
    return PurchaseOrder(
        downstream_order_id=dso.downstream_order_id,
        ali_item_id=item_id,
        ali_sku_id=sku_id,
        qty=qty,
        cost_price=cost_price,
        status=status,
        push_log=push_log,
        ali_order_id=ali_order_id,
        # 采购成本归属商家（与 DSO 同源，结算时作为货款依据）
        merchant_id=dso.merchant_id,
    )


def _sku_cost(item, sku_id: str) -> Decimal:
    """从 item.sku_list 取对应 SKU 的成本价，回退到 item.cost_price。"""
    sku_payload = (item.sku_list or {}).get("skus") or []
    for raw in sku_payload:
        if str(raw.get("sku_id") or "") == sku_id:
            return Decimal(str(raw.get("price") or 0))
    return Decimal(str(item.cost_price or 0))


def _log_exception(
    db: Session,
    *,
    type_: str,
    payload: dict,
) -> ExceptionLog:
    row = ExceptionLog(
        type=type_,
        payload=payload,
        error=payload.get("error") or "",
        retry_count=0,
        resolved=False,
    )
    db.add(row)
    db.flush()
    logger.warning("exception_log type=%s payload=%s", type_, payload)
    return row


# =====================================================================
# 查询
# =====================================================================
def list_purchase_orders(
    db: Session,
    *,
    status: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> list[PurchaseOrder]:
    q = db.query(PurchaseOrder)
    if status:
        q = q.filter(PurchaseOrder.status == status)
    return q.order_by(PurchaseOrder.id.desc()).offset(skip).limit(limit).all()


__all__ = [
    # 入口
    "push_order",
    "push_order_for_dso",
    "confirm_paid",
    "mark_failed",
    "list_purchase_orders",
    # 异常
    "PurchasePushError",
    "OrderNotFound",
    "AutoPaymentNotAllowed",
]