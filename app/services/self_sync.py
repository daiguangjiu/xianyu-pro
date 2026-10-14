"""渔枢系统 → 渔货栈订单状态回写（合并部署时同进程直调）。

链路（2026-10-02 口径）：
    渔货栈确认接单 → **推给渔枢系统采购** → 渔枢系统采购并发货（回填物流单号）
    → **这里回写给渔货栈** → 渔货栈流转到「已发货」→ 买家签收 → 已完成
    → 退单时渔枢系统退款收尾 → 回写给渔货栈 → 渔货栈流转到「已退款」

只有渔货栈出的单（渔枢系统订单 origin='self'）才回写；群消息来的单在渔货栈没有
对应的下游订单，`sync_from_hub` 会自然查不到、直接跳过。

回写失败一律吞掉：渔枢系统是订单中台，不能因为回写渔货栈失败就编辑不了订单。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _self_enabled() -> bool:
    """当前部署有没有挂渔货栈（见 config.hub_enabled / app.hub_bridge）。"""
    try:
        from ..config import settings

        return bool(getattr(settings, "hub_enabled", False))
    except Exception:  # noqa: BLE001
        return False


def notify_self_fulfillment(order_no: str | None) -> list[str]:
    """把这笔渔枢系统订单的履约事实（物流单号 / 状态 / 退款）推给渔货栈。

    返回渔货栈做了哪些动作（没挂渔货栈 / 不是渔货栈的单 / 已经是目标状态 → 空列表）。
    调用方在渔枢系统订单 commit 之后再调，这里另开一个 session 重读渔枢系统订单取最新事实。
    """
    order_no = (order_no or "").strip()
    if not order_no or not _self_enabled():
        return []
    try:
        from sqlalchemy import select

        from ..db import SessionLocal
        from ..models import ORDER_ORIGIN_SELF, Order
    except Exception as exc:  # noqa: BLE001  渔货栈那套没装起来
        logger.debug("渔货栈回写不可用：%s", exc)
        return []

    try:
        with SessionLocal() as db:
            order = db.scalars(select(Order).where(Order.order_no == order_no)).first()
            if order is None or (order.origin or "") != ORDER_ORIGIN_SELF:
                return []
            snap = {
                "order_state": order.order_state,
                "tracking_no": order.tracking_no,
                "refund_amount": float(order.refund_amount or 0),
            }
            from src.order import hub_sync

            return hub_sync.sync_from_hub(db, order_no, snap, actor="渔枢系统")
    except Exception as exc:  # noqa: BLE001  回写失败不能影响渔枢系统自己
        logger.warning("回写渔货栈订单状态失败（%s）：%s", order_no, exc)
        return []
