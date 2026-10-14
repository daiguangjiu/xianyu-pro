"""渔货栈订单流转的执行层：按状态机改状态、写履约字段、记流水。

状态规则全在 `src/order/state.py`（那边只做判断不碰库），这里负责落库：
一次动作对**整单所有子行**（一笔单多个 SKU 时 downstream_order_id 带
`::sku#<idx>` 后缀）一起生效，并给每行记一条 OrderEvent。
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models import DownstreamOrder, OrderEvent
from src.order import state

logger = logging.getLogger(__name__)


def _rows(db: Session, dso_id: str) -> list[DownstreamOrder]:
    """取整单所有子行（dso_id 可以是主单号，也可以是带 ::sku# 后缀的子行号）。"""
    return list(
        db.execute(
            select(DownstreamOrder)
            .where(DownstreamOrder.downstream_order_id.like(f"{dso_id}%"))
            .order_by(DownstreamOrder.id)
        )
        .scalars()
        .all()
    )


def _log(
    db: Session,
    dso: DownstreamOrder,
    action: str,
    from_status: str,
    to_status: str,
    actor: str,
    note: str = "",
    payload: dict | None = None,
) -> None:
    db.add(
        OrderEvent(
            downstream_order_id=dso.downstream_order_id,
            action=action,
            from_status=from_status,
            to_status=to_status,
            actor=actor or "系统",
            note=note or "",
            payload=payload or {},
        )
    )


def _money(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def apply_action(
    db: Session,
    dso_id: str,
    action: str,
    actor: str = "",
    note: str = "",
    **data,
) -> tuple[bool, str]:
    """做一次状态动作。返回 `(ok, 提示语)`，失败不落库（调用方直接把提示给用户）。

    `data` 里可以带 shipping_no / shipping_company / refund_amount 等，
    缺必填字段由 state.plan 拦下（比如「标记发货」必须给物流单号）。
    """
    rows = _rows(db, dso_id)
    if not rows:
        return False, "订单不存在"

    head = rows[0]
    # 退款金额没填就按售价记（渔枢系统口径一致：退款金额按成交价）
    if action in ("refund_request", "refund_finish") and not str(data.get("refund_amount") or "").strip():
        data["refund_amount"] = head.sale_price

    plan = state.plan(head.status, action, **data)
    if not plan.ok:
        return False, plan.error

    # 推给渔枢系统采购：渔枢系统那边建档失败就不允许流转（否则渔货栈显示已推单、渔枢系统其实没有）
    if action == "push_hub":
        try:
            from src.web.hub_orders import push_to_hub

            order, sync_action = push_to_hub(db, head)
        except Exception as exc:  # noqa: BLE001  桥断了要能看清原因
            logger.warning("推给渔枢系统失败（订单 %s）：%s", dso_id, exc)
            return False, f"推给渔枢系统失败：{exc}"
        if order is None:
            return False, "推给渔枢系统失败：渔枢系统没返回订单"
        plan.fields["hub_order_id"] = getattr(order, "id", None)
        note = note or f"渔枢系统建档（{sync_action}）"

    payload = {k: str(v) for k, v in data.items() if v not in (None, "")}
    for d in rows:
        for key, value in plan.fields.items():
            setattr(d, key, value)
        d.status = plan.to_status
        _log(db, d, action, plan.from_status, plan.to_status, actor, note, payload)

    db.commit()
    return True, f"{state.status_label(plan.from_status)} → {state.status_label(plan.to_status)}"


def events_of(db: Session, dso_ids: list[str]) -> list[OrderEvent]:
    """取这些单号的流转流水（时间倒序 → 页面按时间正序画时间线）。"""
    if not dso_ids:
        return []
    return list(
        db.execute(
            select(OrderEvent)
            .where(OrderEvent.downstream_order_id.in_(dso_ids))
            .order_by(OrderEvent.id)
        )
        .scalars()
        .all()
    )


def summary(rows: list[DownstreamOrder]) -> dict:
    """整单的状态摘要：状态、可用动作、履约信息（详情页与列表页共用）。"""
    if not rows:
        return {}
    head = rows[0]
    return {
        "status": head.status,
        "status_label": state.status_label(head.status),
        "actions": state.available_actions(head.status),
        "shipping_no": head.shipping_no,
        "shipping_company": head.shipping_company,
        "refund_amount": head.refund_amount,
        "hub_order_id": head.hub_order_id,
        "is_terminal": head.status in state.TERMINAL_STATUSES,
        # 进度条：正向走到第几步（逆向/终态就按当前步显示）
        "step": (
            state.FORWARD_FLOW.index(head.status) + 1
            if head.status in state.FORWARD_FLOW
            else len(state.FORWARD_FLOW)
        ),
        "steps": [
            {"label": state.status_label(s), "done": s in state.FORWARD_FLOW[: _idx(head.status) + 1]}
            for s in state.FORWARD_FLOW
        ],
    }


def _idx(status: str) -> int:
    return state.FORWARD_FLOW.index(status) if status in state.FORWARD_FLOW else 0


def self_profit(rows: list[DownstreamOrder], hub: dict | None = None) -> dict:
    """渔货栈的毛利（2026-10-03 定稿口径）：

        毛利 = 销售价 − 运费 + 退款给客户 − 渔货价 + 平台退款 − 平台技术服务费

    · **渔货价** = 订单上落的 fish_price（收单时按商品渔货价记的单价 × 数量）；
      订单没记（老数据 / 异常单）才回落到渔枢系统那边的采购成本 cost_amount；
    · 退款给客户存负值、平台退款（渔枢系统的供应商退款）存正值，公式里直接相加；
    · 运费 / 平台技术服务费 / 平台退款只有渔枢系统建档了才有，没建档就按 0 算。
    """
    if not rows:
        return {}
    sale = sum(float(r.sale_price or 0) * int(r.qty or 1) for r in rows)
    fish = sum(float(getattr(r, "fish_price", None) or 0) * int(r.qty or 1) for r in rows)
    if fish <= 0:
        fish = float((hub or {}).get("cost_amount") or 0)
    # 退款给客户是按整单记的（不乘数量），库里存负值
    refund = sum(float(getattr(r, "refund_amount", None) or 0) for r in rows)
    shipping = float((hub or {}).get("shipping_fee") or 0)
    supplier_refund = float((hub or {}).get("supplier_refund_amount") or 0)
    tech_fee = float((hub or {}).get("platform_fee") or 0)
    profit = sale - shipping + refund - fish + supplier_refund - tech_fee
    return {
        "sale": round(sale, 2),
        "fish": round(fish, 2),
        "refund": round(refund, 2),
        "shipping": round(shipping, 2),
        "supplier_refund": round(supplier_refund, 2),
        "tech_fee": round(tech_fee, 2),
        "hub_profit": round(float((hub or {}).get("net_profit") or 0), 2),
        "fish_from_hub": fish > 0 and not any(
            getattr(r, "fish_price", None) for r in rows
        ),
        "profit": round(profit, 2),
    }
