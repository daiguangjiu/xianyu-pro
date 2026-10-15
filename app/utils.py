"""通用小工具。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import timeutil
from .config import settings


def media_url(local_path: str) -> str:
    """把图片地址统一成 /media/... 访问地址。

    带上文件修改时间做版本号：商品图重裁后文件名不变，浏览器会按新地址重新拉图，
    不会继续显示缓存的旧图。

    **库里三种写法都得吃**（2026-10-15 修线上「商品详情页的图全没了」）：
      · 站内地址 `/media/products/xxx.jpg` —— 渔枢商品档案一直是这么存的；
      · 相对路径 `products/xxx.jpg` —— 导入 / 同步时拼出来的；
      · 绝对路径 `/app/data/media/products/xxx.jpg` —— 早期同步脚本写的。
    以前只认最后一种，前两种会被 `relative_to` 抛错后退化成「只剩文件名」，
    于是 `/thumb/12889ec1eff5487a.jpg?w=800` 一律 404，图整片消失。
    """
    if not local_path:
        return ""
    s = str(local_path).strip()
    # 外链图不在我们服务器手里，原样放行（裁不动）
    if s.startswith(("http://", "https://", "//", "data:")):
        return s
    media_root = Path(settings.media_path)
    if s.startswith("/media/"):
        rel = s[len("/media/"):]
        p = media_root / rel
    else:
        p = Path(s)
        if p.is_absolute():
            try:
                rel = p.relative_to(media_root).as_posix()
            except ValueError:
                p = Path(p.name)
                rel = p.as_posix()
        else:
            rel = p.as_posix()
    url = "/media/" + rel
    try:
        stamp = int(p.stat().st_mtime)
    except OSError:
        return url
    return f"{url}?v={stamp}"


def thumb_url(local_path: str, width: int = 320) -> str:
    """把 media 图片换成**按需生成的缩略图**地址（2026-10-15）。

    列表 / 购物车 / 结算这些地方图只占几十像素，原来直接拉 2MB 原图；这里把地址
    换成 /thumb/<相对路径>?w=<宽度>，由 app.thumbs 裁一次并落盘缓存，第二次起
    只有几十 KB。相较直接写 media_url：宽度不同 = 不同 URL，天然不会串味。

    外链（http(s)://、//、data:）原样返回 —— 那些图不在我们服务器手里，裁不动。
    """
    if not local_path:
        return ""
    s = str(local_path)
    if s.startswith(("http://", "https://", "//", "data:")):
        return s
    url = media_url(s)
    if not url.startswith("/media/"):
        return url
    rest = url[len("/media/"):]
    joiner = "&" if "?" in rest else "?"
    return f"/thumb/{rest}{joiner}w={int(width)}"


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
