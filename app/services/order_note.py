"""订单备注：支持多次累计追加（2026-10-06 口径）。

群里不会一次把话说完 —— 常见的情形是先发「原单号 + 退回单号」，过一会儿又补一句
「备注：客户承担运费退回」，改天再来一条「备注：已收到退回件」。以前 order.note 是
单值字段，后面发的内容会把前面的覆盖掉，前面说过的话就再也找不回来了。

现在的口径：**备注是累计的**
  · 每次群里发来新的备注，追加成一行（前面带时间），历史备注一条不动；
  · 同一条内容重复推送（群里重发 / 消息重放）不再重复追加；
  · 每追加一行就在订单处理日志里留一条「追加订单备注」，页面上看得到来源与时间。

人工在订单详情页「编辑订单」里改备注仍然是**覆盖**（那是人工整理后的最终结果），
只有群里自动登记的走这里的累加。
"""
from __future__ import annotations

import logging

from ..models import Order
from ..timeutil import now
from . import order_log as order_log_service

logger = logging.getLogger(__name__)

# 每行备注前的时间戳（北京时间，只到分钟：太长占地方，订单页一行放得下）
STAMP_FORMAT = "%m-%d %H:%M"


def stamp_line(text: str, when=None) -> str:
    """给一段备注加上时间前缀：[10-06 14:01] 内容。"""
    moment = when or now()
    return f"[{moment.strftime(STAMP_FORMAT)}] {(text or '').strip()}"


def has_note(order: Order, text: str) -> bool:
    """这条备注已经记在订单上了吗（按内容比，忽略前面的时间戳）。"""
    body = (text or "").strip()
    if not body:
        return False
    for line in (order.note or "").splitlines():
        # 去掉可能的 "[MM-DD HH:MM] " 前缀再比，只关心内容是否重复
        content = line.split("]", 1)[-1].strip() if line.startswith("[") else line.strip()
        if content == body:
            return True
    return False


def append_note(
    session,
    order: Order,
    text: str,
    actor: str = "",
    source: str = "",
    commit: bool = True,
) -> tuple[bool, str]:
    """往订单上追加一条备注，返回 (是否写入, 说明)。

    历史备注保留，新备注追加到最后一行；内容重复时不重复追加（群里重发同一条
    消息很常见，不然订单页会被同一句话刷屏）。
    """
    body = (text or "").strip()
    if not body:
        return False, "备注为空：没有写入"
    if has_note(order, body):
        return False, "这条备注已经在订单上了：重复推送，未重复追加"

    line = stamp_line(body)
    old = (order.note or "").strip()
    order.note = f"{old}\n{line}" if old else line
    order.updated_at = now()

    detail = f"备注累计：{line}"
    if source:
        detail += f"（来源：{source}）"
    order_log_service.log(
        session,
        order,
        order_log_service.ACTION_NOTE_APPEND,
        detail=detail,
        operator=actor or order_log_service.SYSTEM_OPERATOR,
        commit=False,
    )
    if commit:
        session.commit()
    logger.info("订单 %s 追加备注：%s", order.order_no, line)
    return True, f"备注已累计：{body}"
