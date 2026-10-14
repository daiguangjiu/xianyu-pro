"""渔货栈出单 → 渔枢系统自动建档（把闭环里缺的那一段接上）。

为什么需要它：以前这条链路是**断**的。渔货栈店铺出了单，只落在
`downstream_orders` / `purchase_orders` 里；渔枢系统要等飞书群里那条订单模板消息
被解析出来，才会有这笔订单。群里漏发、或者那条消息没解析成订单，渔枢系统这边就没有
这笔单 —— 看不见、不会推单、不会发货。

现在渔货栈 `accept_order()` 收单成功后直接在这儿建渔枢系统订单，**幂等**：

  · 渔枢系统里已有同单号的订单（群消息先建档了）→ 不重复建，只把**空着的**字段补上，
    已经有人审过 / 填过的值一律不动（与订单合并同一个道理：以先填的为准）；
  · 没有 → 按下游订单的信息建一条「新建」订单，等运营审单后走正常的推单 / 发货流程。

调用方（src/order/service.py）包了 try/except：渔枢系统这套没部署在一起时，
同步失败只记日志，绝不影响渔货栈接单。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ORDER_ORIGIN_SELF, ORDER_STATE_NEW, Order
from ..timeutil import now
from . import order_log as order_log_service
from . import product as product_service

logger = logging.getLogger(__name__)

# sync_downstream_order() 的返回动作码，调用方 / 测试按它判断发生了什么
ACTION_CREATED = "created"  # 渔枢系统里没有这笔单，新建了一条
ACTION_FILLED = "filled"  # 渔枢系统里已有这笔单，补齐了空字段（已有值没动）
ACTION_UNCHANGED = "unchanged"  # 渔枢系统里已有这笔单且字段都齐，什么都没做
ACTION_SKIPPED = "skipped"  # 下游订单缺单号，没法建档


def _is_blank(value: Any) -> bool:
    """字段算不算「空」：空串 / None / 0 都算（金额 0 也当作没填，避免用 0 盖掉真值）。"""
    return value is None or value == "" or value == 0


def _fill(order: Order, values: dict[str, Any], *, only_fill_empty: bool) -> list[str]:
    """把值写进订单，返回发生变化的字段名。

    only_fill_empty=True 时只补空字段 —— 已有的值（人工审过的、群消息解析出来的）不动。
    """
    changed: list[str] = []
    for field, value in values.items():
        if _is_blank(value):
            continue
        current = getattr(order, field, None)
        if current == value:
            continue
        if only_fill_empty and not _is_blank(current):
            continue
        setattr(order, field, value)
        changed.append(field)
    return changed


def _item_and_spec(session: Session, dso) -> tuple[Any, str]:
    """下游订单卖了哪个货源、什么规格：channel_sku_id → SKU 映射 → 货源商品。"""
    from src.models import SkuMapping, SupplierItem

    channel_sku_id = (dso.channel_sku_id or "").strip()
    if not channel_sku_id:
        return None, ""
    mapping = session.scalar(
        select(SkuMapping).where(SkuMapping.channel_sku_id == channel_sku_id)
    )
    if mapping is None:
        return None, ""
    item = session.scalar(
        select(SupplierItem).where(SupplierItem.item_id == mapping.item_id)
    )
    if item is None:
        return None, ""
    # 规格写在货源的 SKU 列表里（同一个货源多规格时，按 SKU 找对应那条）
    spec = ""
    for row in (item.sku_list or {}).get("skus", []) or []:
        if isinstance(row, dict) and str(row.get("sku_id") or "") == str(mapping.ali_sku_id):
            spec = str(row.get("spec") or "").strip()
            break
    return item, spec


def _cost_of(session: Session, dso, item) -> float:
    """成本，按「证据越强越优先」取：

    ① 1688 采购单（推单时定下来的成本，最准）；
    ② 渔货栈新采购域的采购价（stock_orders 里这件货最近一次已付款的进价）；
    ③ 货源的拿货价（兜底）。

    ② 是 2026-10-03 补的：以前只认老的 PurchaseOrder，渔货栈自己下采购单进的货
    在渔枢系统侧成本永远是兜底价（甚至 0），毛利跟着虚高。
    """
    from src.models import PurchaseOrder

    order_no = (dso.downstream_order_id or "").strip()
    po = (
        session.scalars(
            select(PurchaseOrder)
            .where(PurchaseOrder.downstream_order_id == order_no)
            .order_by(PurchaseOrder.id.desc())
        ).first()
        if order_no
        else None
    )
    if po is not None and not _is_blank(float(po.cost_price or 0)):
        return float(po.cost_price)

    # ② 渔货栈采购单：同一件货最近一次已付款的进价
    item_id = (getattr(item, "item_id", "") or "").strip()
    if item_id:
        try:
            from src.models import StockOrder, StockOrderItem

            row = session.execute(
                select(StockOrderItem.price)
                .join(StockOrder, StockOrder.id == StockOrderItem.order_id)
                .where(
                    StockOrderItem.item_id == item_id,
                    StockOrder.status.in_(("paid", "shipped", "completed")),
                )
                .order_by(StockOrder.paid_at.desc(), StockOrderItem.id.desc())
                .limit(1)
            ).first()
            if row is not None and not _is_blank(float(row[0] or 0)):
                return float(row[0])
        except Exception as exc:  # noqa: BLE001  分端部署时没有这张表，退回兜底
            logger.warning("按渔货栈采购单取成本失败（单号 %s）：%s", order_no, exc)

    if item is not None and not _is_blank(float(item.cost_price or 0)):
        return float(item.cost_price)
    return 0.0


def sync_downstream_order(
    session: Session, dso, *, commit: bool = True
) -> tuple[Order | None, str]:
    """一笔渔货栈下游订单同步成渔枢系统订单（幂等）。

    返回 `(order, action)`，action 见模块顶部的 ACTION_* 常量。
    """
    order_no = (dso.downstream_order_id or "").strip()
    if not order_no:
        logger.warning("下游订单 id=%s 没有单号，跳过同步到渔枢系统", getattr(dso, "id", "-"))
        return None, ACTION_SKIPPED

    item, spec = _item_and_spec(session, dso)
    product_name = (item.title or "").strip() if item is not None else ""
    cost = _cost_of(session, dso, item)

    existing = session.scalar(select(Order).where(Order.order_no == order_no))
    if existing is not None:
        # 群里那条消息先建了这笔单：只补空字段，运营已经审过的一律不动
        changed = _fill(
            existing,
            {
                "product_name": product_name,
                "spec": spec,
                "receiver_name": (dso.buyer_name or "").strip(),
                "receiver_phone": (dso.buyer_phone or "").strip(),
                "receiver_address": (dso.address or "").strip(),
                "quantity": int(dso.qty or 1),
                "sale_amount": float(dso.sale_price or 0),
                "cost_amount": cost,
            },
            only_fill_empty=True,
        )
        if changed:
            existing.updated_at = now()
            order_log_service.log(
                session,
                existing,
                action="补齐字段",
                detail=f"渔货栈同一单号（{order_no}）出单，补上：{'、'.join(changed)}",
                operator="渔货栈",
                commit=False,
            )
            logger.info("下游订单 %s 已同步到渔枢系统订单 %s（补齐 %s）", order_no, existing.id, changed)
        if commit:
            session.commit()
        return existing, (ACTION_FILLED if changed else ACTION_UNCHANGED)

    order = Order(
        platform=(dso.channel or "xianyu").strip() or "xianyu",
        order_no=order_no,
        order_state=ORDER_STATE_NEW,
        origin=ORDER_ORIGIN_SELF,
    )
    session.add(order)
    _fill(
        order,
        {
            "product_name": product_name,
            "spec": spec,
            "receiver_name": (dso.buyer_name or "").strip(),
            "receiver_phone": (dso.buyer_phone or "").strip(),
            "receiver_address": (dso.address or "").strip(),
            "quantity": int(dso.qty or 1),
            "sale_amount": float(dso.sale_price or 0),
            "cost_amount": cost,
            "msg_time": getattr(dso, "created_at", None) or now(),
        },
        only_fill_empty=False,
    )
    # 商品归一：名字对得上就挂到渔枢系统的商品上，后面统计 / 详情页能直接跳过去
    if product_name:
        product = product_service.find_by_name(session, product_name)
        if product is not None:
            order.product_id = product.id
    session.flush()  # 拿到 order.id，日志才能挂上去

    order_log_service.log(
        session,
        order,
        action="自动建档",
        detail=f"渔货栈出单（{order_no}）自动同步到渔枢系统，状态新建，等审单推单",
        operator="渔货栈",
        commit=False,
    )
    if commit:
        session.commit()
    logger.info("下游订单 %s 已在渔枢系统自动建档：订单 id=%s", order_no, order.id)
    return order, ACTION_CREATED
