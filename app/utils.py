"""通用小工具。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import timeutil
from .config import settings


def media_url(local_path: str) -> str:
    """把本地绝对路径转换成 /media/... 访问地址。

    带上文件修改时间做版本号：商品图重裁后文件名不变，浏览器会按新地址重新拉图，
    不会继续显示缓存的旧图。
    """
    if not local_path:
        return ""
    p = Path(local_path)
    try:
        rel = p.relative_to(settings.media_path)
    except ValueError:
        rel = Path(p.name)
    url = "/media/" + rel.as_posix()
    try:
        stamp = int(p.stat().st_mtime)
    except OSError:
        return url
    return f"{url}?v={stamp}"


def money(value: float | None) -> str:
    try:
        return f"{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def human_time(value, tz_name: str | None = None) -> str:
    """页面统一的时间文案：**库里存 UTC，这里按用户时区渲染**（2026-10-03 定稿）。

    时区取 `?tz=` 或 cookie `tz`（由 `main.TimezoneMiddleware` 写进 contextvar，
    模板里 `human_time(x)` 拿不到 request 也能跟着走）；拿不到就按北京。
    传进来的不是 datetime（date / 字符串）时按原样格式化，不报错。
    """
    if not value:
        return "-"
    if isinstance(value, datetime):
        return timeutil.fmt(value, "%Y-%m-%d %H:%M", tz_name)
    if hasattr(value, "strftime"):  # date 之类：没有时区概念，按原样出
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)
