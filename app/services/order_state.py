"""订单状态（生命周期）：新建 / 已推单 / 已发货 / 已撤单 / 退单流程 / 关闭完成。

业务口径（2026-09-29 逆向订单口径）：
  · 新建：飞书群同步订单、归档出订单记录即为「新建」；
  · 已推单：运营处理完转账等事项、把复制出来的订单信息同步给上游（供应商）之后；
  · 已发货：上游返回物流单号，运营把物流信息更新到订单之后；
  · 已撤单：还没推单前用户撤单（只留档，不参与统计与后续流程）；
  · 退单流程：买家申请退单 -> 已同步供应商退单 -> 已退款；
    **已发货也受理退单**（不再「不同意退单」）：
      - 已发货、用户还没收到：同步物流拦截（把快递拦下来）；
      - 用户已收到货：收货退单，运营维护退单物流单号（退回的快递）；
  · 关闭完成：逆向流程终态 —— 该退的钱退完（已付款给供应商的：供应商已退款；
    未付款的：不再付款），订单不再参与统计、结算与后续流程。

状态由订单上的事实推导（derive），改动入口统一走 set_state 做校验，避免乱跳状态。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ORDER_STATE_CANCELED,
    ORDER_STATE_CHOICES,
    ORDER_STATE_CLOSED,
    ORDER_STATE_COMPLETED,
    ORDER_STATE_LABELS,
    ORDER_STATE_NEW,
    ORDER_STATE_PUSHED,
    ORDER_STATE_REFUNDED,
    ORDER_STATE_REFUND_REQUEST,
    ORDER_STATE_REFUND_SYNCED,
    ORDER_STATE_SHIPPED,
    REFUND_ORDER_STATES,
    Order,
)
from ..timeutil import now

logger = logging.getLogger(__name__)

# 终态：撤单 / 退单 / 关闭完成之后不再自动往前推进
TERMINAL_STATES = (ORDER_STATE_CANCELED, ORDER_STATE_REFUNDED, ORDER_STATE_CLOSED)
# 正向终态（2026-10-03）：已完成 = 已发货 + 买家签收 / 超时自动确认收货。
# 与上面的逆向终态分开：已完成之后还能走售后退单，但不该再被自动推回「已发货」。
FORWARD_TERMINAL_STATES = (ORDER_STATE_COMPLETED,)
# 发货后多少天自动确认收货（超时兜底）：平台口径 10 天，见 auto_complete_transit()
AUTO_COMPLETE_DAYS = 10


def sync_legacy_status(order: Order) -> None:
    """把老字段 order_status 单向镜像成「退单 / 正常」。

    2026-09 起订单状态只有一套口径：order_state（生命周期）+ refund_status（退单处理）。
    老字段 order_status 全系统不再读，只在这里按 is_refund 同步一次，
    让老备份、老脚本看到的还是「这单是不是退单」，不会再出现两套判断打架。
    """
    order.order_status = "refund" if order.is_refund else "new"


def label(value: str) -> str:
    """状态展示文案（未知值原样返回，便于排查脏数据）。"""
    return ORDER_STATE_LABELS.get(value or "", value or ORDER_STATE_LABELS[ORDER_STATE_NEW])


def choices() -> tuple[str, ...]:
    """下拉 / 筛选用的状态顺序。"""
    return ORDER_STATE_CHOICES


def has_tracking(order: Order) -> bool:
    """是否已回填物流单号（已发货的判定依据）。"""
    return bool((order.tracking_no or "").strip())


def is_shipped(order: Order) -> bool:
    """是否已发货：状态是已发货，或已经回填了物流单号。"""
    return order.order_state == ORDER_STATE_SHIPPED or has_tracking(order)


def derive(order: Order) -> str:
    """按订单上的事实推导状态（关闭完成 > 撤单 / 退单优先，其次发货，再次推单，最后新建）。

    已发货也受理退单（2026-09-29 口径）：已发货没收到货的走物流拦截、已收到货的走退货退款，
    都由「退单处理状态」refund_status 记录，状态跟到退单流程的对应一步。

    注意：这里只认「退单处理状态」这个事实（refund_status），不看 order_state 里已有的
    退单状态 —— 否则取消退单标记 / 恢复正常订单时会绕回来（状态说自己是退单，就永远退不出去）。
    """
    if order.is_recalled:
        # 已撤单：撤单后收尾完成（关闭完成）就停在关闭完成，不再回退成「已撤单」
        return ORDER_STATE_CLOSED if order.order_state == ORDER_STATE_CLOSED else ORDER_STATE_CANCELED
    if (order.refund_status or "").strip():
        # 关闭完成是终态：退单走完（钱该退的退完 / 该不付的没付）之后停在这里
        if order.order_state == ORDER_STATE_CLOSED:
            return ORDER_STATE_CLOSED
        # 老数据：已发货无法拦截（旧口径「不同意退单」，现已不受理这种写法）
        if order.refund_status == "shipped":
            return ORDER_STATE_SHIPPED
        if order.refund_status == "refunded":
            return ORDER_STATE_REFUNDED
        # 退单流程走到哪一步就停在哪一步（买家申请 -> 已同步供应商），不倒退
        if order.order_state in REFUND_ORDER_STATES:
            return order.order_state
        return ORDER_STATE_REFUND_REQUEST
    # 正向终态：签收 / 确认收货过了（completed_at 有值）就是已完成，不再退回「已发货」
    if order.completed_at is not None:
        return ORDER_STATE_COMPLETED
    # 已发货看物流单号：单号被清空就退回上一步（已推单 / 新建），状态始终跟着事实走
    if has_tracking(order):
        return ORDER_STATE_SHIPPED
    if order.pushed_at is not None or order.supplier_status in ("pushed", "done"):
        return ORDER_STATE_PUSHED
    return ORDER_STATE_NEW


def check(order: Order, target: str) -> tuple[bool, str]:
    """能否流转到 target，返回 (是否允许, 不允许的原因)。

    已经处于该状态时视为允许（幂等，不报错）。
    """
    target = (target or "").strip()
    if target not in ORDER_STATE_CHOICES:
        return False, f"未知的订单状态：{target or '（空）'}"
    current = order.order_state or ORDER_STATE_NEW
    if target == current:
        return True, ""
    # 关闭完成是终态：想改先点「恢复正常订单」，避免终态被随手改回中途状态
    if current == ORDER_STATE_CLOSED:
        return False, "该订单已关闭完成（逆向流程走完了）：如需继续处理请先点「恢复正常订单」"

    if target == ORDER_STATE_NEW:
        return False, "「新建」是订单归档时的初始状态，不能手工改回"

    if target == ORDER_STATE_PUSHED:
        if current == ORDER_STATE_CANCELED:
            return False, "已撤单的订单不能再推单"
        if current in REFUND_ORDER_STATES:
            return False, "退单的订单不能再推单（推单状态：用户退单，无需推单）"
        if current == ORDER_STATE_SHIPPED or is_shipped(order):
            return False, "已发货的订单不能退回「已推单」"
        return True, ""

    if target == ORDER_STATE_SHIPPED:
        if current == ORDER_STATE_CANCELED:
            return False, "已撤单的订单不能标记发货"
        if current in REFUND_ORDER_STATES:
            return False, "退单的订单不能标记发货"
        if not (order.tracking_no or "").strip():
            return False, "请先把上游返回的物流单号填进订单，再标记已发货"
        return True, ""

    if target == ORDER_STATE_CANCELED:
        if current in REFUND_ORDER_STATES:
            return False, "该订单已在退单流程里：请走退单收尾（关闭完成），不用再撤单"
        if current == ORDER_STATE_SHIPPED or is_shipped(order):
            return (
                False,
                f"订单已发货（物流单号 {(order.tracking_no or '').strip()}），不能撤单："
                "用户要退就走退单流程（已发货的同步物流拦截 / 已收货的维护退单物流单号）",
            )
        return True, ""

    if target == ORDER_STATE_COMPLETED:
        # 正向终态：已发货 + 签收（或超时自动确认收货）。没发货的单不能跳到已完成。
        if current == ORDER_STATE_CANCELED or order.is_recalled:
            return False, "已撤单的订单不能标记完成"
        if current in REFUND_ORDER_STATES:
            return False, "该订单在退单流程里：请走退单收尾（关闭完成），不用再标完成"
        if current not in (ORDER_STATE_SHIPPED,) and not is_shipped(order):
            return False, "请先填物流单号并标记已发货，签收后才能标记完成"
        return True, ""

    if target in (ORDER_STATE_REFUND_REQUEST, ORDER_STATE_REFUND_SYNCED, ORDER_STATE_REFUNDED):
        # 逆向退单三步：买家申请 -> 运营同步供应商 -> 已退款。
        # **已发货也受理**：已发货没收到货的同步物流拦截、已收到货的走退货退款（都要受理）。
        # 只有已撤单（只留档）的订单不用再退。
        if current == ORDER_STATE_CANCELED or order.is_recalled:
            return False, "已撤单的订单无需再退单"
        return True, ""

    if target == ORDER_STATE_CLOSED:
        # 关闭完成：逆向流程的收尾。只有已经在退单流程里 / 已撤单的订单能关。
        if current in REFUND_ORDER_STATES or current == ORDER_STATE_CANCELED:
            return True, ""
        if order.is_recalled:
            return True, ""
        return False, "正常订单不能「关闭完成」：只有退单 / 撤单的订单才走这个收尾动作"

    return False, f"不支持的订单状态流转：{label(current)} → {label(target)}"


def set_state(
    session: Session,
    order: Order,
    target: str,
    actor: str = "",
    note: str = "",
    commit: bool = True,
) -> tuple[bool, str]:
    """把订单状态流转到 target（带校验），返回 (是否成功, 说明)。

    会同步维护对应字段（推单时间、发货时间、撤回标记、退单标记），
    保证状态与订单上的事实始终一致（否则下次刷新会被推导结果改回去）。
    """
    ok, reason = check(order, target)
    if not ok:
        return False, reason
    target = (target or "").strip()
    current = order.order_state or ORDER_STATE_NEW
    if target == current:
        return True, ""

    ts = now()
    if target == ORDER_STATE_PUSHED:
        if order.supplier_status not in ("done", "cancelled"):
            order.supplier_status = "pushed"
        if order.pushed_at is None:
            order.pushed_at = ts
    elif target == ORDER_STATE_SHIPPED:
        if order.shipped_at is None:
            order.shipped_at = ts
    elif target == ORDER_STATE_COMPLETED:
        # 已完成：记确认收货时间（derive 靠它把状态稳定在终态，不会被打回已发货）
        if order.completed_at is None:
            order.completed_at = ts
        if order.shipped_at is None:
            order.shipped_at = ts
    elif target == ORDER_STATE_CANCELED:
        if not order.is_recalled:
            order.is_recalled = True
            order.recalled_at = ts
        if actor and not order.recalled_by:
            order.recalled_by = actor
    elif target == ORDER_STATE_REFUND_REQUEST:
        # 买家申请退单：退单理由必填（没有就留在「待补充理由」，页面会提醒）
        if not order.refund_at:
            order.refund_at = ts
        if not order.refund_status:
            order.refund_status = "pending"
    elif target == ORDER_STATE_REFUND_SYNCED:
        # 运营已把退单同步给供应商：记同步时间，等退款结果
        if order.refund_supplier_synced_at is None:
            order.refund_supplier_synced_at = ts
        if not order.refund_status:
            order.refund_status = "pending"
    elif target == ORDER_STATE_REFUNDED:
        if not order.refund_at:
            order.refund_at = ts
        if not order.refund_status:
            order.refund_status = "pending"
        if not order.refund_keyword:
            order.refund_keyword = "手工标记"
    elif target == ORDER_STATE_CLOSED:
        # 关闭完成：记收尾时间（钱该退的退完 / 该不付的没付），之后这单不再参与后续流程
        if order.closed_at is None:
            order.closed_at = ts
        if not order.refund_handled_at:
            order.refund_handled_at = ts

    order.order_state = target
    sync_legacy_status(order)
    order.updated_at = ts
    if note:
        order.note = (f"{order.note}\n{note}" if order.note else note)[:2000]
    if commit:
        session.commit()
    logger.info(
        "订单 %s 状态：%s → %s%s",
        order.order_no,
        label(current),
        label(target),
        f"（{actor}）" if actor else "",
    )
    return True, f"{label(current)} → {label(target)}"


def refresh(session: Session, order: Order, commit: bool = True) -> str:
    """按订单当前事实刷新状态（推单 / 回填物流 / 撤销撤回等场景调用）。"""
    current = order.order_state or ORDER_STATE_NEW
    target = derive(order)
    if target == current:
        return current
    order.order_state = target
    if target == ORDER_STATE_SHIPPED and order.shipped_at is None:
        order.shipped_at = now()
    sync_legacy_status(order)
    order.updated_at = now()
    if commit:
        session.commit()
    return target


def advance(session: Session, order: Order, target: str, actor: str = "") -> tuple[bool, str]:
    """事件驱动的自动流转：不合法就跳过（不抛错、不打断主流程）。"""
    ok, reason = check(order, target)
    if not ok:
        if (order.order_state or ORDER_STATE_NEW) != target:
            logger.info("订单 %s 未流转到「%s」：%s", order.order_no, label(target), reason)
        return False, reason
    return set_state(session, order, target, actor=actor)


def auto_complete_transit(
    session: Session,
    days: int = AUTO_COMPLETE_DAYS,
    *,
    actor: str = "超时自动确认收货",
    commit: bool = True,
) -> int:
    """超时兜底：已发货超过 N 天还没签收的，自动落到「已完成」。

    正向流程以前到「已发货」就停了，没人点完成就永远在途、结算也分不出哪些可以结。
    这里按 shipped_at（没记就退回 updated_at）算天数，超过阈值就自动确认收货。
    退单流程里的单不动（交给退单收尾）。
    """
    from ..timeutil import now as _now

    deadline = _now() - timedelta(days=max(int(days or 0), 1))
    rows = list(
        session.scalars(
            select(Order).where(
                Order.order_state == ORDER_STATE_SHIPPED,
                Order.completed_at.is_(None),
            )
        ).all()
    )
    changed = 0
    for order in rows:
        base = order.shipped_at or order.updated_at
        if base is None or base > deadline:
            continue
        if (order.refund_status or "").strip() or order.is_recalled:
            continue  # 退单 / 撤单里的单交给各自的收尾动作
        ok, _ = set_state(session, order, ORDER_STATE_COMPLETED, actor=actor, commit=False)
        if ok:
            changed += 1
    if changed:
        if commit:
            session.commit()
        logger.info("超时自动确认收货：%s 笔订单（超过 %s 天）落到「已完成」", changed, days)
    return changed


def backfill(session: Session, orders: Iterable[Order] | None = None) -> int:
    """老订单补状态：按订单上的事实重算（幂等，可反复执行）。

    老库新增 order_state 列后默认值是「新建」，这里按 撤单 / 退单 / 物流 / 推单
    的顺序重算一遍，历史订单也能落到正确的状态上。
    """
    rows = list(orders) if orders is not None else list(session.scalars(select(Order)).all())
    changed = 0
    for order in rows:
        target = derive(order)
        if target == (order.order_state or ORDER_STATE_NEW):
            continue
        order.order_state = target
        sync_legacy_status(order)
        order.updated_at = now()
        changed += 1
    if changed:
        session.commit()
        logger.info("订单状态回填：%s 笔订单按当前事实重算状态", changed)
    return changed
