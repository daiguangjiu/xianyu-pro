"""时间口径统一：**库里一律存 UTC naive，展示一律按服务端时区（默认北京时间）**。

2026-10-13 收口（用户拍板）：以前展示"按用户时区"—— 时区取自浏览器写进 cookie 的
``tz``，**同一条数据坐在不同时区的电脑上看是两个点**，这就是「上传商品的时间戳不是
北京时间」的根因。现在改成 **全站统一按 ``settings.display_tz``（默认 Asia/Shanghai）渲染**，
浏览器时区一律不认；真要按各自时区看，才把 ``TZ_FOLLOW_BROWSER`` 设成 1。

约定：
  - 所有「系统时间」字段（created_at / updated_at / pushed_at / notified_at …）
    写 UTC 的 naive 值（不带 tzinfo），统一用本模块的 ``now()``；
  - 业务代码不要再写 ``datetime.now()`` / ``datetime.utcnow()``
    （前者是服务器本地时间，容器设了 TZ=Asia/Shanghai，等于北京时间）；
  - **展示 / 传输一律按服务端时区**：``fmt()`` / ``to_local()`` / ``to_iso()``
    默认都走 ``tz_of()``（= ``display_tz``，不传就不按浏览器时区换算）；
  - 页面靠模板的 ``| dt`` 过滤器或 ``human_time()``，**不要在模板里直接 ``.strftime()``**
    （那样出来的是库里的 UTC 值，会比北京时间早 8 小时）。

历史数据里旧版本写进去的是**北京时间** naive 值，首次启动时由
``db._migrate_utc_time()`` 统一 −8 小时（存 UTC），展示再 +8 回来 —— 用户看到的时间不变。
"""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 东八区；名字用 ZoneInfo 一致的写法，方便日志里辨认
BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
# 同一个时区的 ZoneInfo 版本：按用户时区换算时用得上（支持夏令时等）
BEIJING_ZONE = "Asia/Shanghai"
UTC = timezone.utc

# 页面时间显示用的时区 cookie（前端按浏览器时区写进来）
TZ_COOKIE = "tz"

# ---------------------------------------------------------------- 当前请求时区
# 模板过滤器（| dt）能拿到 request，但 `human_time()` 这类全局函数拿不到。
# 由 `app/main.py` 的时区中间件在每次请求开始时写进来，渲染函数直接读它；
# 没有请求上下文（定时任务 / 飞书推送 / 脚本）时是 None → 按北京渲染。
current_tz: ContextVar[str | None] = ContextVar("current_tz", default=None)


def set_current_tz(tz_name: str | None):
    """设置当前上下文（一次请求）的时区，返回的 token 交给 ``reset_current_tz`` 复位。"""
    return current_tz.set(tz_name or None)


def reset_current_tz(token) -> None:
    """复位 ``set_current_tz`` 写进去的时区（中间件 finally 里调）。"""
    current_tz.reset(token)


def _settings():
    """延迟 import：timeutil 被很底层的模块用，别在导入期把配置链拉起来。"""
    from .config import settings

    return settings


def display_tz() -> str:
    """全站统一的显示时区（默认 ``Asia/Shanghai``）。"""
    try:
        return (getattr(_settings(), "display_tz", "") or "").strip() or BEIJING_ZONE
    except Exception:  # noqa: BLE001
        return BEIJING_ZONE


def follow_browser() -> bool:
    """是否跟浏览者的浏览器时区走（默认 False = 全站统一服务端时区）。"""
    try:
        return bool(getattr(_settings(), "tz_follow_browser", False))
    except Exception:  # noqa: BLE001
        return False


def tz_of(tz_name: str | None = None) -> str | None:
    """渲染用时区：**默认全站统一用服务端时区（``display_tz``）**。

    只有开了 ``TZ_FOLLOW_BROWSER`` 才认外面带进来的时区（显式传参 / 请求里的
    ``?tz=`` 或 cookie ``tz``）—— 这样渔枢、渔货栈、渔集三端与接口、CSV 导出
    出来的时间才会是同一个点。
    """
    if tz_name and follow_browser():
        return tz_name
    if follow_browser():
        return current_tz.get() or display_tz()
    return display_tz()


def tz_from_scope(scope) -> str | None:
    """从 ASGI scope 里取用户时区：``?tz=`` 优先，其次 cookie ``tz``，都没有返回 None。

    只取名字不做校验 —— 脏值由 ``_zone()`` 回落到北京，别让坏 cookie 把页面打挂。
    """
    try:
        query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
        tz = (query.get(TZ_COOKIE) or [None])[0]
        if tz:
            return tz
        for raw_key, raw_value in scope.get("headers") or []:
            if raw_key.lower() != b"cookie":
                continue
            jar = SimpleCookie()
            jar.load(raw_value.decode("latin-1"))
            morsel = jar.get(TZ_COOKIE)
            if morsel:
                return morsel.value
    except Exception:  # noqa: BLE001  解析不出来就按默认时区渲染
        return None
    return None


def now() -> datetime:
    """当前时刻（UTC naive），**库里所有时间字段都用它**。"""
    return datetime.now(UTC).replace(tzinfo=None)


def utc_now() -> datetime:
    """``now()`` 的同义函数：老代码里写 utc_now() 的地方继续能用。"""
    return now()


def beijing_now() -> datetime:
    """当前北京时间（naive）。

    只在「要按北京时间的自然日算账」的地方用（日报 / 账单日 / 超时边界），
    存库仍然用 ``now()``。
    """
    return datetime.now(BEIJING).replace(tzinfo=None)


def from_epoch_ms(value: int | float | None) -> datetime | None:
    """毫秒时间戳 -> UTC naive（飞书消息时间戳走这里）。"""
    if not value:
        return None
    return datetime.fromtimestamp(float(value) / 1000, UTC).replace(tzinfo=None)


def to_beijing(value: datetime | None) -> datetime | None:
    """把带时区的时间转成北京时间；naive 值按原样返回（老函数，保留兼容）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(BEIJING).replace(tzinfo=None)


def _zone(name: str | None):
    """时区名 -> tzinfo；不认识的名字回落到北京（别让脏 cookie 把页面打挂）。"""
    if not name:
        return BEIJING
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return BEIJING


def to_local(value: datetime | None, tz_name: str | None = None) -> datetime | None:
    """UTC naive（库里存的值）-> 用户时区的 naive 时间，供页面展示。

    ``tz_name`` 取不到就按北京（``Asia/Shanghai``）。库里如果混进了带 tzinfo 的值，
    也一并转过去，不会算错两次。
    """
    if value is None:
        return None
    zone = _zone(tz_of(tz_name))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(zone).replace(tzinfo=None)


def to_iso(value: datetime | None, tz_name: str | None = None) -> str | None:
    """UTC naive -> ISO8601 字符串（传输用）。

    **默认按服务端统一时区出**（如 ``2026-10-03T20:15:00+08:00``），不再是 UTC 的
    ``...Z`` —— 接口、导出、回调里的时间跟页面显示的是同一个点。
    """
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(_zone(tz_of(tz_name))).isoformat()


def fmt(value: datetime | None, pattern: str = "%Y-%m-%d %H:%M", tz_name: str | None = None) -> str:
    """按服务端统一时区格式化（页面里用 ``| dt`` 过滤器，别在模板里直接 strftime）。

    时区由 ``tz_of()`` 决定（默认北京，不跟浏览器）。已经是字符串的值（后端
    格式化过的）原样返回 —— 以前那会拿字符串当时间对象去换算，直接把页面打挂。
    """
    if value is None:
        return ""
    if not isinstance(value, datetime):
        return str(value)
    local = to_local(value, tz_of(tz_name))
    if local is None:
        return ""
    return local.strftime(pattern)


def from_local(value: datetime | None, tz_name: str | None = None) -> datetime | None:
    """表单里填的时间（**用户时区**）-> UTC naive（存库）。

    ``| dt`` 渲染出来的是用户时区的值，用户改完再提交时反向换算一次 ——
    直接把表单值当 UTC 存，页面会显示成另一个点（差整个时区偏移）。
    """
    if value is None:
        return None
    zone = _zone(tz_of(tz_name))
    return value.replace(tzinfo=zone).astimezone(UTC).replace(tzinfo=None)


def start_of_local_day(tz_name: str | None = None, at: datetime | None = None) -> datetime:
    """「用户时区的今天 00:00」对应的 UTC naive（按天统计 / 日报的边界）。

    按天分组必须用这个，别拿 UTC 的零点当边界 —— 否则北京用户早上 8 点前的订单
    会被算到前一天。
    """
    zone = _zone(tz_of(tz_name))
    local = (at.replace(tzinfo=UTC) if at and at.tzinfo is None else at) or datetime.now(UTC)
    local = local.astimezone(zone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC).replace(tzinfo=None)
