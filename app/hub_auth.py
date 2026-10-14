"""渔货栈与渔枢系统的登录打通（两端同一个进程、同一个端口）。

登录只在渔枢系统做（`oh_session` Cookie，见 `app/web/auth.py`）：

- 进 `/self` 下的页面先过渔枢系统的登录中间件；
- 渔枢系统侧按「统一账号的登录名 = 商家账号手机号」在渔枢系统库里找到自己的账号，
  角色（platform_admin / merchant / reseller）和商家归属一律沿用渔枢系统库里的记录，
  渔枢系统的业务代码不用改；
- 能不能进渔枢系统，看统一账号的子系统权限（见 `app/portals.py`），管理员默认全子系统。
"""
from __future__ import annotations

from fastapi import Request

from .db import SessionLocal
from .models import User
from .portals import PORTAL_SELF
from .web.auth import COOKIE_NAME, parse_session


def unified_user(request: Request) -> User | None:
    """当前登录的统一账号；没登录 / 会话失效时返回 None。"""
    user_id = parse_session(request.cookies.get(COOKIE_NAME, ""))
    if not user_id:
        return None
    with SessionLocal() as db:
        return db.get(User, user_id)


def hub_login_name(request: Request) -> str:
    """渔枢系统侧用来查自己账号的登录名（统一账号名即商家手机号）。"""
    user = unified_user(request)
    return user.username if user else ""


def may_enter_hub(request: Request) -> bool:
    """有没有渔货栈的权限（已登录 + 端授权）。"""
    user = unified_user(request)
    return bool(user and user.status == "active" and user.has_portal(PORTAL_SELF))
