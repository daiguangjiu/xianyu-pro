"""检测仓的「操作员」：登录身份 + 检测仓自己的授权。

分层写清楚了才不会又把权限搞到一起去：

    「你是谁」      平台登录态说了算（``bridge.current_identity``）—— 账号、
                   密码、启用停用都在平台那边，检测仓不重复维护一份人；
    「你能干什么」  完全由检测仓自己的角色权限（``wms.permissions``）说了算，
                   平台的角色（管理员 / 子账号）**只对一件事有影响**：平台管理员
                   自动拥有检测仓全部权限，其它账号一概以这里的授权为准。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from . import bridge, permissions


@dataclass
class Operator:
    """一次请求里的检测仓操作员。"""

    id: int = 0
    username: str = ""
    display: str = ""
    is_admin: bool = False
    role_names: list[str] = field(default_factory=list)
    perms: set[str] = field(default_factory=set)

    @property
    def label(self) -> str:
        return self.display or self.username or "未登录"

    @property
    def can_anything(self) -> bool:
        return bool(self.perms)

    def has(self, code: str | None) -> bool:
        return permissions.has(self.perms, code)


def operator_of(request: Any, db: Session | None = None) -> Operator | None:
    """解析当前操作员；没登录返回 ``None``。"""
    identity = bridge.current_identity(request)
    if not identity:
        return None
    role_names: list[str] = []
    perms: set[str] = set()
    if db is not None and identity["id"]:
        try:
            perms = permissions.perms_of(db, identity["id"], identity["is_admin"])
            role_names = permissions.role_names_of(db, identity["id"])
        except Exception:  # noqa: BLE001  授权表还没建好时不该把页面打死
            perms = set(permissions.ALL_PERMS) if identity["is_admin"] else set()
    elif identity["is_admin"]:
        perms = set(permissions.ALL_PERMS)
    if identity["is_admin"]:
        role_names = ["平台管理员（检测仓全部权限）"] + role_names
    return Operator(
        id=int(identity["id"] or 0),
        username=identity["username"],
        display=identity["display"],
        is_admin=bool(identity["is_admin"]),
        role_names=role_names,
        perms=perms,
    )
