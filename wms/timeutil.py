"""检测仓自己的时间口径：**库里存 UTC naive，页面按用户时区展示**。

口径与平台保持一致（同一套规则），但实现是自己的一份 —— 子系统不 import 渔枢系统的工具库。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
DEFAULT_TZ = "Asia/Shanghai"

# 页面时区 cookie（前端按浏览器时区写进来，取不到就按北京）
TZ_COOKIE = "tz"


def now() -> datetime:
    """当前时刻（UTC naive）—— 库里所有时间字段都用它。"""
    return datetime.now(UTC).replace(tzinfo=None)


def utc_now() -> datetime:
    return now()


def beijing_now() -> datetime:
    """当前北京时间（naive）：只用于「按自然日算账」的地方，存库仍用 ``now()``。"""
    return datetime.now(BEIJING).replace(tzinfo=None)


def _zone(name: str | None):
    if not name:
        return BEIJING
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return BEIJING


def to_local(value: datetime | None, tz_name: str | None = None) -> datetime | None:
    """UTC naive -> 用户时区 naive（页面展示用）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_zone(tz_name)).replace(tzinfo=None)


def fmt(
    value: datetime | None,
    pattern: str = "%Y-%m-%d %H:%M",
    tz_name: str | None = None,
) -> str:
    if value is None:
        return ""
    return to_local(value, tz_name).strftime(pattern)


def to_iso(value: datetime | None, tz_name: str | None = None) -> str | None:
    """ISO8601（带偏移），给 JSON / 导出用。"""
    if value is None:
        return None
    local = to_local(value, tz_name)
    if local is None:
        return None
    return local.isoformat()
