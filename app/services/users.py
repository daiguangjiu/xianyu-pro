"""后台账号管理：格式校验、建号、改密、停用、删除。"""
from __future__ import annotations

import re

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..models import IDENTITY_CHOICES, IDENTITY_INTERNAL, User
from ..portals import ALL_PORTALS, PORTAL_HUB, join_portals  # noqa: F401  PORTAL_HUB 用于降权回落
from ..timeutil import now
from ..web.auth import ROLE_ADMIN, ROLE_USER, hash_password

# 账号：3-20 位，字母开头，只允许字母/数字/下划线
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,19}$")
USERNAME_RULE = "账号 3-20 位，字母开头，只能用字母、数字、下划线"
PASSWORD_RULE = "密码 8-32 位，且必须同时包含字母和数字"
# 手机号：11 位大陆号码；可留空（不填 = 不用手机号登录）
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
PHONE_RULE = "手机号请填 11 位大陆号码（留空 = 不用手机号登录）"


def validate_username(username: str) -> str:
    name = str(username or "").strip()
    if not USERNAME_RE.match(name):
        raise ValueError(USERNAME_RULE)
    return name


def validate_password(password: str, username: str = "") -> str:
    pwd = str(password or "")
    if not (8 <= len(pwd) <= 32):
        raise ValueError(PASSWORD_RULE)
    if not re.search(r"[A-Za-z]", pwd) or not re.search(r"\d", pwd):
        raise ValueError(PASSWORD_RULE)
    if username and pwd.lower() == str(username).strip().lower():
        raise ValueError("密码不能与账号相同")
    return pwd


def list_users(session: Session) -> list[User]:
    return list(session.scalars(select(User).order_by(User.id.asc())).all())


def get_user(session: Session, user_id: int) -> User | None:
    return session.get(User, user_id)


def count_active_admin(session: Session) -> int:
    return (
        session.scalar(
            select(func.count(User.id)).where(User.role == ROLE_ADMIN, User.status == "active")
        )
        or 0
    )


def validate_identity(value: str) -> str:
    """身份：只认 internal / student；空值或不认识的一律按内部人员。"""
    text = str(value or "").strip()
    return text if text in dict(IDENTITY_CHOICES) else IDENTITY_INTERNAL


def set_identity_kind(session: Session, user_id: int, identity: str) -> User:
    """改账号身份（内部人员 / 外部学员）—— 外部学员进渔货栈会先走一遍开店引导。"""
    user = session.get(User, user_id)
    if not user:
        raise ValueError("账号不存在")
    user.identity = validate_identity(identity)
    user.updated_at = now()
    session.commit()
    return user


def create_user(
    session: Session,
    username: str,
    password: str,
    role: str = ROLE_USER,
    display_name: str = "",
    portals: str = "",
    identity: str = IDENTITY_INTERNAL,
) -> User:
    name = validate_username(username)
    pwd = validate_password(password, name)
    if session.scalar(select(User).where(User.username == name)):
        raise ValueError(f"账号「{name}」已存在")
    role = ROLE_ADMIN if role == ROLE_ADMIN else ROLE_USER
    user = User(
        username=name,
        display_name=str(display_name or "").strip() or name,
        password_hash=hash_password(pwd),
        role=role,
        status="active",
        # 管理员一律全端；子账号按勾选的值，一个都没勾就只给渔枢系统
        portals=ALL_PORTALS if role == ROLE_ADMIN else join_portals(portals),
        identity=validate_identity(identity),
    )
    session.add(user)
    session.commit()
    return user


def set_password(session: Session, user_id: int, password: str) -> User:
    user = session.get(User, user_id)
    if not user:
        raise ValueError("账号不存在")
    validate_password(password, user.username)
    user.password_hash = hash_password(password)
    user.updated_at = now()
    session.commit()
    return user


def set_status(session: Session, user_id: int, status: str) -> User:
    user = session.get(User, user_id)
    if not user:
        raise ValueError("账号不存在")
    if status not in ("active", "disabled"):
        raise ValueError("状态不合法")
    if user.role == ROLE_ADMIN and status == "disabled" and count_active_admin(session) <= 1:
        raise ValueError("至少保留一个可用的管理员账号")
    user.status = status
    user.updated_at = now()
    session.commit()
    return user


def set_role(session: Session, user_id: int, role: str) -> User:
    user = session.get(User, user_id)
    if not user:
        raise ValueError("账号不存在")
    role = ROLE_ADMIN if role == ROLE_ADMIN else ROLE_USER
    if user.role == ROLE_ADMIN and role != ROLE_ADMIN and count_active_admin(session) <= 1:
        raise ValueError("至少保留一个管理员账号")
    # 端跟着角色走：管理员一律全端；降为子账号时收回，只留渔枢系统（之后再单独勾）
    if role == ROLE_ADMIN:
        user.portals = ALL_PORTALS
    elif user.role == ROLE_ADMIN:
        user.portals = PORTAL_HUB
    user.role = role
    user.updated_at = now()
    session.commit()
    return user


def set_identity(
    session: Session,
    user_id: int,
    username: str | None = None,
    phone: str | None = None,
) -> User:
    """改登录账号 / 手机号：两个都能用来登录，改完原来那个就登不进去了。

    传 None 表示这一项不动 —— 表单里没带这个字段时不能把它清掉。
    """
    user = session.get(User, user_id)
    if not user:
        raise ValueError("账号不存在")
    if username is not None:
        name = validate_username(username)
        if session.scalar(select(User).where(User.username == name, User.id != user_id)):
            raise ValueError(f"账号「{name}」已被占用")
        user.username = name
    if phone is not None:
        value = str(phone or "").strip()
        if value:
            if not PHONE_RE.match(value):
                raise ValueError(PHONE_RULE)
            if session.scalar(select(User).where(User.phone == value, User.id != user_id)):
                raise ValueError(f"手机号「{value}」已被其它账号占用")
        user.phone = value or None
    user.updated_at = now()
    session.commit()
    return user


def set_portals(session: Session, user_id: int, portals) -> User:
    """调整子账号能进哪些子系统（管理员默认全子系统，不用也改不动）。"""
    user = session.get(User, user_id)
    if not user:
        raise ValueError("账号不存在")
    if user.is_admin:
        raise ValueError("管理员默认拥有全部子系统，无需单独配置")
    picked = [str(p).strip() for p in (portals.split(",") if isinstance(portals, str) else portals)]
    picked = [p for p in picked if p]
    if not picked:
        raise ValueError("至少勾选一个端，否则账号登录后没有可进的端")
    user.portals = join_portals(picked)
    user.updated_at = now()
    session.commit()
    return user


def delete_user(session: Session, user_id: int, operator_id: int | None = None) -> None:
    user = session.get(User, user_id)
    if not user:
        return
    if operator_id and user.id == operator_id:
        raise ValueError("不能删除当前登录的账号")
    if user.role == ROLE_ADMIN and count_active_admin(session) <= 1:
        raise ValueError("至少保留一个管理员账号")
    # 渔货栈（同一个库）有三张表引用 users.id，直接删会被外键挡住 —— Postgres 报
    # ForeignKeyViolation，页面上看着就是「点了删除没反应」。先解绑再删：
    #   merchant_users / merchant_applies：账号在商家下的身份与入驻申请，跟着账号一起清掉
    #   merchants.owner_user_id：负责人置空，商家本身保留（要转给别人去渔货栈操作）
    session.execute(
        text("DELETE FROM merchant_users WHERE user_id = :uid"), {"uid": user_id}
    )
    session.execute(
        text("DELETE FROM merchant_applies WHERE user_id = :uid"), {"uid": user_id}
    )
    session.execute(
        text("UPDATE merchants SET owner_user_id = NULL WHERE owner_user_id = :uid"),
        {"uid": user_id},
    )
    session.delete(user)
    session.commit()
