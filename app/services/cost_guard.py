"""成本写入的护栏：自动流程写进来的成本「看着就不对」时，不写、留痕、标出来等人工确认。

背景（2026-10-01 修）：售价 55 的充电宝，成本被写成 133 —— 群消息里明明白白写着
「成本价：45」，但建单后异步跑的截图 OCR 参与了字段解析，把截图上认到的某个数字
（商品页的划线价 / 原价这类）当成了成本价覆盖进去，毛利直接变负数，且没有任何留痕。

成本高于售价在业务上不成立（真亏损属于极少数，而且应该由人来确认），所以这里统一
拦一道：**自动流程**（解析 / 截图 OCR / 重新解析 / 去重合并）写成本前先过这里，
不对就直接不写，标「待核对」并留一条日志写明「拦下了什么、来源是哪」。

人工在页面上自己填的不受这条限制 —— 人已经看过数字了，填多少都按人的算。
"""

from __future__ import annotations

from sqlalchemy import select

from ..models import Order, not_recalled
from . import order_log as order_log_service

# 成本拦下时挂到「待核对原因」上的前缀：清理由解析产生的旧原因时会保留这一类
COST_REASON_PREFIX = "成本异常"


def cost_reason_for(sale, value) -> str:
    """纯函数版：拿一个售价数字当判据，看这个值当成本对不对（空串 = 没问题）。

    给「手上只有解析结果、还没落到订单上」的场景用（如纠错样本的采用与记录）。
    """
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "成本不是数字"
    if v <= 0:
        return "成本为 0（没取到值）"
    try:
        s = float(sale or 0)
    except (TypeError, ValueError):
        s = 0.0
    if s > 0 and v > s:
        return f"成本 ¥{v:.2f} 比售价 ¥{s:.2f} 还高"
    return ""


def cost_reason(order: Order, value, sale_hint=0) -> str:
    """这个值当成本看着对不对：空串 = 没问题，否则返回原因（人话）。

    sale_hint：订单自己的售价还没写进去时的备用判据（新单先算成本、后算售价的情形），
    不传就只看订单上的售价 —— 售价为 0 时无从判断，一律放行。
    """
    sale = float(order.sale_amount or 0) or float(sale_hint or 0)
    return cost_reason_for(sale, value)


def _add_review_reason(order: Order, reason: str) -> None:
    """把「成本异常」挂到订单的待核对原因上（不覆盖别的原因）。"""
    parts = [
        p
        for p in (order.review_reason or "").split("；")
        if p.strip() and not p.strip().startswith(COST_REASON_PREFIX)
    ]
    parts.append(f"{COST_REASON_PREFIX}：{reason}（自动写入已拦下，请人工核对成本）")
    order.review_reason = "；".join(parts)
    order.needs_review = True


def guard_cost(
    session, order: Order, value, source: str = "", sale_hint=0
) -> tuple[bool, str]:
    """自动流程写成本前的检查：不对就不写。

    返回 (能不能写, 原因)：能写时调用方照常赋值；不能写时已经标好待核对并留了痕。
    sale_hint 见 cost_reason。
    """
    reason = cost_reason(order, value, sale_hint=sale_hint)
    if not reason:
        return True, ""
    _add_review_reason(order, reason)
    if session is not None:
        order_log_service.log(
            session,
            order,
            order_log_service.ACTION_COST_BLOCKED,
            detail=(
                f"{reason} —— 已拦下未写入"
                f"（来源：{source or '自动流程'}）；请人工核对后在详情页手填成本"
            ),
            operator=source or "系统",
            commit=False,
        )
    return False, reason


def scan_cost_anomalies(session) -> list[dict]:
    """全库扫一遍成本异常（成本 > 售价 / 成本为 0），只报不改。"""
    rows: list[dict] = []
    for order in session.scalars(select(Order).where(not_recalled())).all():
        reason = cost_reason(order, order.cost_amount)
        if not reason:
            continue
        rows.append(
            {
                "order_id": order.id,
                "order_no": order.order_no or "",
                "product_name": order.product_name or "",
                "sale": float(order.sale_amount or 0),
                "cost": float(order.cost_amount or 0),
                "label": reason,
                "fix": "到订单详情把成本改成实际采购价（自动写入已被拦下，人工改的会保留）",
                "closed": bool(order.is_closed),
            }
        )
    rows.sort(key=lambda r: (r["cost"] - r["sale"]), reverse=True)
    return rows
