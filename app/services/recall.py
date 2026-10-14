"""消息撤回：群里的模板消息 / 订单截图被撤回后，对应订单打上「撤回」标记。

业务口径（2026-09）：
  1 群里撤回一条订单模板消息或订单截图 -> 找到它对应的订单，打上「撤回」标记
    （模板消息：订单的 source_message_id / 消息上挂的 order_id；
      订单截图：截图挂靠的模板消息，或截图直接建的单）；
  2 撤回的订单只留档：不参与任何数据统计（首页看板、供应商、日报汇总），
    也不参与后续流程（待推单、推单批次、缺截图提醒）；
  3 页面一眼可辨：置灰 + 文字删除线 + 「撤回订单」徽标 / 水印；
  4 误撤回（或群里重发后）可撤销：撤销后订单重新参与统计与流程。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import RECALL_LABEL, Order, OrderImage, PushBatch, PushBatchItem, RawMessage
from ..timeutil import now
from . import order_state as order_state_service

logger = logging.getLogger(__name__)

# 撤回的订单要从「还没推送」的批次里撤出来；已推送的批次保留历史，不改写
_OPEN_BATCH_STATUS = "draft"


def orders_of_message(session: Session, message_id: str) -> list[Order]:
    """按某条群消息的 id 找出相关联的订单（模板消息 / 订单截图 / 挂靠的截图都算）。

    撤回事件只带一个 message_id，这里把能对上的关系都找一遍：
      1 消息自己挂的订单（模板消息解析后写的 order_id）；
      2 订单的来源消息就是这条（或它的父消息）；
      3 这条消息下的订单截图（截图行上挂的订单）；
      4 挂靠在这条消息下的子消息（模板消息 + 订单截图是一组）对应的订单。
    即使本地没存过这条消息（系统上线前发的 / 事件先到），也能按订单表反查出来。
    """
    message_id = str(message_id or "").strip()
    if not message_id:
        return []
    found: dict[int, Order] = {}

    def _add(order: Order | None) -> None:
        if order is not None:
            found[order.id] = order

    def _add_message(mid: str) -> None:
        """把这条消息自己 / 它挂靠的模板消息对应的订单加进来。"""
        if not mid:
            return
        msg = session.scalar(select(RawMessage).where(RawMessage.message_id == mid))
        if msg is not None and msg.order_id:
            _add(session.get(Order, msg.order_id))
        for row in session.scalars(select(Order).where(Order.source_message_id == mid)).all():
            _add(row)

    msg = session.scalar(select(RawMessage).where(RawMessage.message_id == message_id))
    # 1) 消息自己挂的订单 + 订单的来源消息就是这条
    _add_message(message_id)
    # 2) 截图消息：它挂靠的模板消息对应的订单
    if msg is not None and msg.parent_message_id:
        _add_message(msg.parent_message_id)
    # 3) 订单截图消息：截图行上挂的订单
    for img in session.scalars(
        select(OrderImage).where(OrderImage.message_id == message_id)
    ).all():
        if img.order_id:
            _add(session.get(Order, img.order_id))
    # 4) 挂在这条消息下的子消息（撤回模板消息时，配套的订单截图一起算）
    children = session.scalars(
        select(RawMessage).where(RawMessage.parent_message_id == message_id)
    ).all()
    for child in children:
        _add_message(child.message_id)
        for img in session.scalars(
            select(OrderImage).where(OrderImage.message_id == child.message_id)
        ).all():
            if img.order_id:
                _add(session.get(Order, img.order_id))
    return list(found.values())


def find_related_orders(session: Session, message_id: str) -> list[Order]:
    """（兼容老接口）按消息 id 找关联订单。"""
    return orders_of_message(session, message_id)


def drop_from_open_batches(session: Session, order: Order) -> int:
    """撤回订单从「未推送」的批次里撤出来：不参与后续推单流程。"""
    removed = 0
    for item in session.scalars(
        select(PushBatchItem).where(PushBatchItem.order_id == order.id)
    ).all():
        batch = session.get(PushBatch, item.batch_id)
        if batch is None or batch.status != _OPEN_BATCH_STATUS:
            continue
        session.delete(item)
        removed += 1
    if removed:
        session.flush()
    return removed


def mark_recalled(
    session: Session,
    order: Order,
    msg: RawMessage | None = None,
    recaller: str = "",
    reason: str = "",
    commit: bool = True,
) -> Order:
    """给订单打上撤单标记（重复撤单只留第一次的时间与来源）。

    reason：撤单来源说明（群里撤回消息 / 群里发消息撤单 / 运营手工标记），页面上可查。
    """
    # 已发货（物流已出库）的订单不允许撤单：只能让用户收到货后走退货退款。
    # 群里撤回消息常常是「规格写错后重发」，这时候订单还在正常履约，不能打撤单标记
    if order_state_service.is_shipped(order):
        logger.warning(
            "订单 %s 已发货（物流单号 %s）：撤回只作留档，不置为已撤单",
            order.order_no,
            (order.tracking_no or "").strip(),
        )
        return order
    ts = now()
    first_time = not order.is_recalled
    if not order.is_recalled:
        order.is_recalled = True
        order.recalled_at = ts
    if msg is not None and not order.recalled_message_id:
        order.recalled_message_id = msg.message_id
    if recaller and not order.recalled_by:
        order.recalled_by = recaller
    if reason and not order.recall_reason:
        order.recall_reason = reason
    order.updated_at = ts
    # 订单状态 -> 已撤单：口径是「还没发货前撤回」；已发货的只留档，不改状态
    if order_state_service.is_shipped(order):
        logger.warning(
            "订单 %s 已发货（物流单号 %s）：撤回只作留档，不置为已撤单",
            order.order_no,
            (order.tracking_no or "").strip(),
        )
    else:
        order_state_service.set_state(
            session,
            order,
            order_state_service.ORDER_STATE_CANCELED,
            actor=recaller,
            commit=False,
        )
    drop_from_open_batches(session, order)
    # 撤单的订单不再结算：对应结算流水置为「已作废」，只留档
    try:
        from . import finance as finance_service  # 局部导入，避免循环依赖

        finance_service.void_order(session, order)
    except Exception as exc:  # noqa: BLE001  结算流水失败不影响撤单
        logger.warning("撤单后作废结算流水失败 %s: %s", order.order_no, exc)
    if commit:
        session.commit()
    if first_time:
        # 订单处理日志：首次撤单留一条（谁撤的、什么来源），重复撤单不刷屏
        from . import order_log as order_log_service  # 局部导入，避免循环依赖

        order_log_service.log(
            session,
            order,
            order_log_service.ACTION_RECALL,
            detail=f"撤单来源：{reason or '群里撤回 / 撤单'}",
            operator=recaller,
            commit=commit,
        )
    logger.info(
        "订单 %s 已标记%s（消息 %s）",
        order.order_no,
        RECALL_LABEL,
        order.recalled_message_id or "-",
    )
    return order


def undo_recalled(session: Session, order: Order, commit: bool = True) -> Order:
    """撤销撤单标记：订单重新参与统计与后续流程。"""
    order.is_recalled = False
    order.recalled_at = None
    order.recalled_message_id = ""
    order.recalled_by = ""
    order.recall_reason = ""
    order.updated_at = now()
    # 撤销撤回后订单重新参与流程：状态按当前事实重算（该推单的还是待推单）
    order_state_service.refresh(session, order, commit=False)
    # 结算流水跟着恢复（从「已作废」退回待结算）
    try:
        from . import finance as finance_service

        finance_service.sync_order(session, order)
    except Exception as exc:  # noqa: BLE001
        logger.warning("撤销撤单后恢复结算流水失败 %s: %s", order.order_no, exc)
    if commit:
        session.commit()
    logger.info("订单 %s 已撤销%s标记", order.order_no, RECALL_LABEL)
    return order


def mark_message_recalled(
    session: Session, msg: RawMessage, recaller: str = "", commit: bool = False
) -> RawMessage:
    """标记该条群消息已被撤回（撤回人只作记录）。"""
    _ = recaller
    if not msg.is_recalled:
        msg.is_recalled = True
        msg.recalled_at = now()
    if commit:
        session.commit()
    return msg


def recaller_label(recaller: str, client=None) -> str:
    """撤回人展示名：飞书撤回事件给的是 open_id，能查通讯录就换成真名。"""
    value = str(recaller or "").strip()
    if not value:
        return ""
    if not value.startswith(("ou_", "on_")):
        return value
    try:
        from .ingest import resolve_sender_name  # 局部导入，避免循环依赖

        name = resolve_sender_name(value, client)
        return name or value
    except Exception as exc:  # noqa: BLE001  查名字失败不影响撤单
        logger.debug("撤回人姓名解析失败 %s: %s", value, exc)
        return value


def handle_message_recalled(
    session: Session,
    message_id: str,
    chat_id: str = "",
    recaller: str = "",
    commit: bool = True,
) -> dict:
    """处理一条被撤回的消息：定位关联订单 -> 置为已撤单（只留档），返回处理结果。

    撤回事件只带 message_id：即使这条消息本地没存过（系统上线前发的 / 事件先到），
    也会按订单表反查（来源消息 / 订单截图 / 挂靠关系）把订单找出来。
    """
    message_id = str(message_id or "").strip()
    result = {
        "message_id": message_id,
        "chat_id": chat_id,
        "found_message": False,
        "orders": [],
        "marked": 0,
        # 已发货 / 已退单的订单不再撤单（群里撤回常常是规格写错后重发），只留档记录
        "skipped": [],
    }
    if not message_id:
        return result

    who = recaller_label(recaller)
    msg = session.scalar(select(RawMessage).where(RawMessage.message_id == message_id))
    if msg is not None:
        result["found_message"] = True
        mark_message_recalled(session, msg, who)

    orders = orders_of_message(session, message_id)
    if msg is None and not orders:
        logger.info("收到消息撤回通知，本地既没有这条消息也没有关联订单：%s", message_id)
        return result

    for order in orders:
        if not order.is_recalled:
            # 已退单的订单不再因消息撤回而撤单：退单已有自己的处理流程（拦截 / 退款）
            if order.is_refund:
                result["skipped"].append(order.id)
                logger.info(
                    "消息 %s 已撤回，但订单 %s 已走退单流程：只留档记录，不置为撤单",
                    message_id,
                    order.order_no,
                )
            else:
                mark_recalled(session, order, msg, who, reason="群里撤回消息", commit=False)
                if order.is_recalled:
                    result["marked"] += 1
                else:
                    result["skipped"].append(order.id)
        result["orders"].append(order.id)
    if commit:
        session.commit()
    if result["marked"]:
        # 撤回的订单不占「今日第 x 单」的号：立刻重排一次（后台定时任务也会兜底修复）
        from . import push as push_service

        push_service.repair_daily_indexes(session)
    logger.info(
        "消息 %s 已撤回（撤回人：%s）：关联订单 %s（新标记 %s 笔，跳过 %s 笔）",
        message_id,
        who or "-",
        result["orders"] or "无",
        result["marked"],
        len(result["skipped"]),
    )
    return result
