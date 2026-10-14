"""买家标识：订单的「买家」一栏 = 收货人 + 手机号（2026-09-29 口径）。

群里同步的订单模板里没有买家昵称，闲鱼的昵称也常常读不到；但每笔订单都有
收货人和手机号 —— 这两项拼起来就是「这个人」的稳定标识，跨单能认出同一个买家，
以后的用户管理（复购、退单率、异常买家）就按这一栏来做。

口径：
  · 买家 = 「收货人 手机号」，例如「王小萌 18561274331」；
  · 只有其中一项时就只写那一项（不硬拼空格）；
  · 运营在订单详情手改过的买家名不覆盖 —— 只补空白的和解析噪声（「已付款」「买家」这类）。
"""
from __future__ import annotations

import logging

from sqlalchemy import or_, select

from ..models import Order

logger = logging.getLogger(__name__)

# 解析时被当成「买家昵称」抓进来的噪声：这些不是人名，遇到就当空处理
NOISE_VALUES = frozenset(
    {"已付款", "了昵称", "买家", "昵称", "待发货", "已下单", "付款"}
)


def buyer_label(receiver_name: str = "", receiver_phone: str = "") -> str:
    """买家的展示 / 归档文本：收货人 + 手机号。"""
    name = str(receiver_name or "").strip()
    phone = str(receiver_phone or "").strip()
    if name and phone:
        return f"{name} {phone}"
    return name or phone


def is_noise(value: str) -> bool:
    """这个值是不是解析噪声（「已付款」这类被误当成昵称的）。"""
    return str(value or "").strip() in NOISE_VALUES


def fill_buyer(order: Order) -> bool:
    """给单笔订单补买家标识：空的 / 噪声的才补，运营手改过的不动。"""
    label = buyer_label(order.receiver_name, order.receiver_phone)
    if not label:
        return False
    current = str(order.buyer_nick or "").strip()
    if current and not is_noise(current):
        return False
    order.buyer_nick = label
    return True


def ensure_buyer_labels(session, batch: int = 2000) -> int:
    """把历史订单里「买家一栏还是空的 / 噪声」的都补上（幂等，可每次启动调）。"""
    stmt = (
        select(Order)
        .where(
            or_(
                Order.buyer_nick.is_(None),
                Order.buyer_nick == "",
                Order.buyer_nick.in_(sorted(NOISE_VALUES)),
            ),
            or_(Order.receiver_name != "", Order.receiver_phone != ""),
        )
        .limit(batch)
    )
    filled = 0
    for order in session.scalars(stmt).all():
        if fill_buyer(order):
            filled += 1
    if filled:
        session.commit()
        logger.info("买家标识补全：%s 个订单（收货人 + 手机号）", filled)
    return filled
