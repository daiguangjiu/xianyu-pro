"""渔枢系统履约回写：渔枢系统采购发货之后，把结果同步回渔货栈订单并往下流转。

链路（2026-10-02 定的口径）：
    渔货栈确认接单 → **推给渔枢系统采购** → 渔枢系统采购并回物流单号
    → 回写给渔货栈（已发货）→ 渔货栈确认收货 → 已完成
    → 退单时渔枢系统退款完成 → 回写（退款完成）

回写是**幂等**的：已经在目标状态就跳过；渔枢系统那边的状态比渔货栈靠后多少就补多少步。
桥不可用（渔货栈单独部署）时全部静默返回空，不影响渔货栈自己的订单。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models import DownstreamOrder
from src.order import flow, state

logger = logging.getLogger(__name__)

# 渔枢系统订单状态（app/models.py 的 ORDER_STATE_*）→ 渔货栈该做什么
_HUB_ORDER_STATE = {
    "new": "新建",
    "pushed": "已推单",
    "shipped": "已发货",
    "refund_request": "退单申请",
    "refund_synced": "已同步供应商退单",
    "refunded": "已退款",
    "closed": "关闭完成",
    "canceled": "已撤单",
}


def sync_from_hub(db: Session, dso_id: str, hub: dict | None, actor: str = "渔枢系统") -> list[str]:
    """按渔枢系统这笔单的事实推进渔货栈订单，返回做了哪些动作（没有就空列表）。

    `hub` 是 `src/web/hub_orders.one()` 给的只读快照；没有（未建档 / 桥断）就什么都不做。
    """
    if not hub:
        return []

    rows = list(
        db.execute(
            select(DownstreamOrder)
            .where(DownstreamOrder.downstream_order_id.like(f"{dso_id}%"))
            .order_by(DownstreamOrder.id)
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    done: list[str] = []
    tracking = (hub.get("tracking_no") or "").strip()
    hub_state = (hub.get("order_state") or "").strip()
    refund = hub.get("refund_amount") or 0

    # ① 渔枢系统回写了物流单号 → 已发货（渔货栈这边还停在「已推单」才推）
    if tracking and state.can(rows[0].status, "ship"):
        ok, msg = flow.apply_action(
            db, dso_id, "ship", actor=actor,
            note=f"渔枢系统回传物流单号 {tracking}",
            shipping_no=tracking,
        )
        done.append("ship" if ok else f"ship 失败：{msg}")

    # ② 渔枢系统已退款 / 已关闭完成 → 渔货栈走退款收尾
    if hub_state in ("refunded", "closed") and refund:
        if state.can(rows[0].status, "refund_request"):
            ok, _ = flow.apply_action(
                db, dso_id, "refund_request", actor=actor,
                note=_HUB_ORDER_STATE.get(hub_state, hub_state),
                refund_amount=refund,
            )
            done.append("refund_request" if ok else "refund_request 失败")
        rows = _reload(db, dso_id)
        if state.can(rows[0].status, "refund_finish"):
            ok, _ = flow.apply_action(
                db, dso_id, "refund_finish", actor=actor,
                note="渔枢系统退款完成", refund_amount=refund,
            )
            done.append("refund_finish" if ok else "refund_finish 失败")

    # ③ 渔枢系统撤单 → 渔货栈取消（只在还没发货时）
    if hub_state == "canceled" and state.can(rows[0].status, "cancel"):
        ok, _ = flow.apply_action(db, dso_id, "cancel", actor=actor, note="渔枢系统撤单")
        done.append("cancel" if ok else "cancel 失败")

    return done


def _reload(db: Session, dso_id: str) -> list[DownstreamOrder]:
    return list(
        db.execute(
            select(DownstreamOrder)
            .where(DownstreamOrder.downstream_order_id.like(f"{dso_id}%"))
            .order_by(DownstreamOrder.id)
        )
        .scalars()
        .all()
    )
