"""订单合并：把一笔订单并到另一笔订单上（典型场景：退单消息被误建成独立订单）。

业务口径（2026-09）：
  · 群里「此单退」这类消息早期会被误建成一笔新订单（成本为 0、挂在临时单号下），
    真正的订单是另一笔（有成本、有供应商）—— 两笔其实是同一单的正向与退单；
  · 合并 = 以正向订单为主（dst），把退单订单（src）上的信息并过来后删掉 src，
    避免同一单在系统里留两条、统计里重复算一次；
  · 并过来的内容：截图 / 商品图、推单批次、群消息挂靠，以及主订单缺的字段
    （收货人、手机号、地址、出货闲鱼号、发送人…）与缺的金额；
  · **退单标记跟着走**：src 带退单信息的，合并后主订单同样标记退单（时间沿用原退单时间），
    状态按流转规则走到「已退单」（已发货的不受理，只并字段不改状态并提示）；
  · **撤回标记不带过去**：src 上「误建成新订单」的撤回标记是纠错用的，不是真实撤单；
  · 主订单已有的字段（成本、售价、供应商…）以主订单为准，不被 src 的 0 / 空值覆盖。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ORDER_STATE_REFUNDED,
    Order,
    OrderImage,
    ParseCorrection,
    PushBatchItem,
    RawMessage,
    Settlement,
    VoiceEvent,
)
from ..services import finance as finance_service
from ..services import order_log as order_log_service
from ..services import order_state as order_state_service
from ..timeutil import now

logger = logging.getLogger(__name__)

# 主订单缺什么就补什么（不覆盖主订单已有的值）
_FIELD_ATTRS = (
    "buyer_nick", "receiver_name", "receiver_phone", "receiver_address",
    "product_name", "spec", "supplier_name", "xianyu_account", "payee_name",
    "chat_id", "chat_name", "raw_text", "note", "product_image_path",
    "sender_id", "sender_name", "tracking_no", "temp_order_no",
)
_AMOUNT_ATTRS = (
    "sale_amount", "deal_amount", "total_amount", "cost_amount",
    "shipping_fee", "platform_fee",
)
# 退单信息：src 有的直接搬到主订单（退单是对这一单的处理，不能因为合并丢掉）
_REFUND_ATTRS = (
    "refund_status", "refund_keyword", "refund_at", "refund_note",
    "refund_source_message_id", "refund_handled_at", "refund_by",
)


# 订单 id 最多这么多位：再长的纯数字是闲鱼订单号，不能拿去 int()（会越 64 位整型上限）
_MAX_ID_DIGITS = 9


def find_order(session: Session, key: str) -> Order | None:
    """按订单号 / 数字 id 找订单（页面输入框里两种都能填）。

    **别再无条件 int(纯数字)**：闲鱼订单号是 16~24 位纯数字，SQLite 绑参 /
    PostgreSQL bigint 都装不下，会抛 OverflowError / bigint out of range，
    而路由没兜异常 → 页面直接 500（2026-10-05 运营点「合并」报错就是这个）。
    现在只有位数像订单 id 的才当 id 查，其余一律按订单号字符串匹配。
    """
    text = (key or "").strip()
    if not text:
        return None
    if text.isdigit() and len(text) <= _MAX_ID_DIGITS:
        row = session.get(Order, int(text))
        if row is not None:
            return row
    row = session.scalar(select(Order).where(Order.order_no == text))
    if row is not None:
        return row
    # 合并不改单号、只把被并掉的单号留在 temp_order_no 里：填那个号也能追到本单
    return session.scalar(select(Order).where(Order.temp_order_no == text))


def _migrate_assets(session: Session, src: Order, dst: Order) -> None:
    """截图 / 商品图、推单批次、群消息挂靠、语音播报、纠错样本全部改挂到主订单。"""
    for img in session.scalars(
        select(OrderImage).where(OrderImage.order_id == src.id)
    ).all():
        img.order_id = dst.id
    for item in session.scalars(
        select(PushBatchItem).where(PushBatchItem.order_id == src.id)
    ).all():
        dup = session.scalar(
            select(PushBatchItem).where(
                PushBatchItem.batch_id == item.batch_id,
                PushBatchItem.order_id == dst.id,
            )
        )
        if dup is not None:
            session.delete(item)
        else:
            item.order_id = dst.id
    for msg in session.scalars(
        select(RawMessage).where(RawMessage.order_id == src.id)
    ).all():
        msg.order_id = dst.id
    # 这两张表的 order_id 是裸整数（没外键），不搬就会留下指向已删订单的悬空引用
    for row in session.scalars(
        select(VoiceEvent).where(VoiceEvent.order_id == src.id)
    ).all():
        row.order_id = dst.id
    for row in session.scalars(
        select(ParseCorrection).where(ParseCorrection.order_id == src.id)
    ).all():
        row.order_id = dst.id
    # 上面改的是外键列、没走关系属性：必须先落库再删源单。
    # 否则 session.delete(src) 会按「库里还写着 src.id」把这些子行加载成 src 的孤儿，
    # 被 delete-orphan 一起删掉（截图丢失）或在 UPDATE / DELETE 交叉时抛 StaleDataError。
    session.flush()


def merge_orders(
    session: Session,
    src: Order,
    dst: Order,
    actor: str = "",
    commit: bool = False,
) -> tuple[bool, str]:
    """把 src 并入 dst（src 会被删除），返回 (是否成功, 说明)。"""
    if src is None or dst is None:
        return False, "订单不存在"
    if src.id == dst.id:
        return False, "不能和自己合并"
    if (src.platform or "xianyu") != (dst.platform or "xianyu"):
        return False, f"两笔订单平台不同（{src.platform} / {dst.platform}），不能合并"

    src_no = src.order_no or f"#{src.id}"
    src_refund = src.is_refund
    moved: list[str] = []

    for attr in _FIELD_ATTRS:
        if not getattr(dst, attr, "") and getattr(src, attr, ""):
            setattr(dst, attr, getattr(src, attr))
            moved.append(attr)
    for attr in _AMOUNT_ATTRS:
        if not getattr(dst, attr, 0) and getattr(src, attr, 0):
            value = getattr(src, attr)
            if attr == "cost_amount":
                # 成本从另一单搬过来也要过护栏：比售价还高就留空等人工，见 services/cost_guard
                from .cost_guard import guard_cost

                ok, _reason = guard_cost(session, dst, value, source="订单合并")
                if not ok:
                    continue
            setattr(dst, attr, value)
            moved.append(attr)
    if dst.ordered_at is None and src.ordered_at is not None:
        dst.ordered_at = src.ordered_at
    if dst.msg_time is None and src.msg_time is not None:
        dst.msg_time = src.msg_time
    if not dst.source_message_id and src.source_message_id:
        dst.source_message_id = src.source_message_id
    # 被合并订单的单号留档备查（临时单号被并掉后还能追到来源）
    if not dst.temp_order_no and src.order_no:
        dst.temp_order_no = src.order_no

    _migrate_assets(session, src, dst)

    # 被合并的那笔带退单信息：主订单同样标记退单（时间沿用原退单时间）
    state_note = ""
    if src_refund:
        for attr in _REFUND_ATTRS:
            value = getattr(src, attr, None)
            if value:
                setattr(dst, attr, value)
        ok, reason = order_state_service.set_state(
            session, dst, ORDER_STATE_REFUNDED, actor=actor, commit=False
        )
        if ok:
            state_note = "，订单状态 → 已退单"
        else:
            # 已发货的退单按口径不受理：字段已经并过来，状态不动，提示原因让运营知道
            state_note = f"（状态未改：{reason}）"
            logger.info("合并退单但状态未流转 %s -> %s：%s", src_no, dst.order_no, reason)
        moved.append("退单标记")

    # 被合并订单的结算流水不再单独存在：主订单按合并后的事实重算
    for row in session.scalars(
        select(Settlement).where(Settlement.order_id == src.id)
    ).all():
        session.delete(row)

    detail = f"合并订单：{src_no} 并入本单"
    if moved:
        detail += "（" + "、".join(_labels(moved)) + "）"
    src_id, src_no_saved = src.id, src_no
    session.delete(src)
    session.flush()
    finance_service.sync_order(session, dst)

    dst.updated_at = now()
    order_log_service.log(
        session,
        dst,
        order_log_service.ACTION_MERGE,
        detail=detail,
        operator=actor,
    )
    logger.info("订单合并：%s(#%s) -> %s(#%s)", src_no_saved, src_id, dst.order_no, dst.id)
    if commit:
        session.commit()
    return True, f"已把 {src_no} 合并到本单{state_note}"


def _labels(attrs: list[str]) -> list[str]:
    """字段 -> 中文说明（日志里给运营看）。"""
    text = {
        "buyer_nick": "买家",
        "receiver_name": "收货人",
        "receiver_phone": "手机号",
        "receiver_address": "地址",
        "product_name": "产品",
        "spec": "规格",
        "supplier_name": "供应商",
        "xianyu_account": "出货闲鱼号",
        "payee_name": "回款归属人",
        "sender_id": "发送人",
        "sender_name": "发送人",
        "tracking_no": "物流单号",
        "temp_order_no": "原单号",
        "sale_amount": "售价",
        "deal_amount": "成交价",
        "total_amount": "商品总价",
        "cost_amount": "成本",
        "shipping_fee": "运费",
        "platform_fee": "平台技术服务费",
        "note": "备注",
        "product_image_path": "商品图",
        "chat_name": "所属群",
        "raw_text": "原始文本",
    }
    out: list[str] = []
    for attr in attrs:
        label = text.get(attr)
        if label and label not in out:
            out.append(label)
    return out
