"""每条消息「发群里还是发私信」的判定。

.env 里没为某条消息配过 MSG_ROUTE_<KEY> 时，一律按 messages_registry 里的默认方式走；
默认值记的就是这套功能上线前的老行为，所以不配置 = 和以前一模一样。
"""

from __future__ import annotations

from ..config import settings
from ..messages_registry import (
    CH_GROUP,
    CH_PM_SENDER,
    CH_PM_SYNC,
    KEYS,
)

_FALLBACK = "receipt"  # 没登记过的消息：按订单归档回执的默认方式发


def _default_channels(key: str) -> tuple[str, ...]:
    """这条消息的默认发送方式 = 上线前的老行为（全局「私信同步给本人」关掉就不再私信）。"""
    from ..messages_registry import DEFAULT_ROUTES

    raw = str(DEFAULT_ROUTES.get(key, DEFAULT_ROUTES.get(_FALLBACK, "")) or "")
    channels = [c.strip() for c in raw.split(",") if c.strip()]
    if not settings.notify_pm_sync and CH_PM_SENDER in channels:
        channels.remove(CH_PM_SENDER)
    return tuple(channels)


def channels_of(key: str) -> tuple[str, ...]:
    """这条消息当前开启的发送渠道：消息中心里勾过就以勾的为准，否则用默认值。"""
    if key not in KEYS:
        key = _FALLBACK
    explicit = str(settings.message_routes.get(key, "") or "").strip()
    if explicit:
        return tuple(c.strip() for c in explicit.split(",") if c.strip())
    return _default_channels(key)


def wants(key: str, channel: str) -> bool:
    """这条消息这会儿要不要走这个渠道？

    「关闭下发」压倒一切：群、私信都不发。
    「只私信」模式下：群里一律不发，私信仍然发给发送人和通知同步人（老行为如此）。
    """
    mode = str(settings.notify_mode or "reply").lower()
    if mode == "off":
        return False
    if mode == "direct":
        return channel != CH_GROUP
    return channel in channels_of(key)


def group_only_anyway(key: str) -> bool:
    """每日汇总这类「固定发群」的消息：只看自己在消息中心里勾没勾群聊。

    它一向不受上方「群内回复 / 只私信 / 关闭下发」影响（汇总要 @ 所有人），
    要停请在汇总设置里关「每日汇总」开关。
    """
    return CH_GROUP in channels_of(key)


def set_route(key: str, channels: list[str] | tuple[str, ...]) -> str:
    """保存某条消息的发送方式（立即生效，返回写进 .env 的值）。"""
    value = ",".join(c.strip() for c in channels if c in (CH_GROUP, CH_PM_SENDER, CH_PM_SYNC))
    settings.message_routes[key] = value
    return value
