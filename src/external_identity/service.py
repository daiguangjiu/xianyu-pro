"""External identity synchronization service."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IDENTITY_STUDENT, User
from app.portals import PORTAL_SELF, join_portals
from src.models.external_identity import ExternalIdentity

_USERNAME_MAX_LEN = 32
_USERNAME_RESERVED = 4  # 留给 "_NNN" 唯一后缀


class IdentityConflict(Exception):
    """The external identity or matching platform user is already claimed."""


def _candidate_username(external_user_id: str, phone: str | None) -> str:
    """派生一个默认用户名，确保能放进 User.username (32) 长度限制里。"""
    if phone:
        seed = f"xhm_{phone}"
    else:
        seed = f"xhm_{external_user_id}"
    # 留出 4 个字符给 _NNN 后缀以避免唯一约束冲突
    return seed[: _USERNAME_MAX_LEN - _USERNAME_RESERVED]


def _ensure_unique_username(db: Session, base: str) -> str:
    """用户名基底撞名时按 _2/_3/... 追加；超过 999 次则报错让人工介入。"""
    candidate = base
    suffix_width = _USERNAME_RESERVED - 1  # "_" + 最多 3 位
    for counter in range(1, 1000):
        if not db.scalar(select(User.id).where(User.username == candidate)):
            return candidate
        suffix = f"_{counter}"
        candidate = f"{base[: _USERNAME_MAX_LEN - len(suffix)]}{suffix}"
        if counter > 999 or len(suffix) > suffix_width:
            break
    raise IdentityConflict("无法为闲参谋用户生成唯一用户名，请联系平台管理员补登")


def _map_source_role(source_role: str) -> str:
    """把 xh-fish 的角色映射到平台 role：admin 不变，其余按子账号处理。"""
    return "admin" if source_role == "admin" else "user"


def _create_platform_user(
    db: Session,
    *,
    external_user_id: str,
    phone: str | None,
    display_name: str,
    source_role: str,
) -> User:
    """为首次到达的闲参谋用户开平台账号。

    - 用户名按 phone / external_user_id 派生，撞名自动加 _2/_3 后缀；
    - 不设置密码（密码字段保持空），所有调用走服务间 HMAC 鉴权；
    - portals 仅开通 self（渔货栈），与 xh-fish 入口一致；
    - identity 设为 student，首次进渔货栈走引导流程。
    """
    username = _ensure_unique_username(db, _candidate_username(external_user_id, phone))
    user = User(
        username=username,
        phone=phone,
        display_name=display_name,
        password_hash="",
        role=_map_source_role(source_role),
        status="active",
        portals=join_portals([PORTAL_SELF]),
        identity=IDENTITY_STUDENT,
    )
    db.add(user)
    db.flush()
    return user


def _sync_platform_user_fields(
    user: User, *, phone: str | None, display_name: str
) -> None:
    """把首次未填、后续同步才补齐的资料回填到平台账号。

    注意：只补空字段，不动 role/status/portals/identity，避免反复同步触发降权或端切换。
    """
    if phone and not user.phone:
        user.phone = phone
    if display_name and not user.display_name:
        user.display_name = display_name


def sync_xianmou_identity(
    db: Session,
    *,
    external_user_id: str,
    phone: str | None,
    display_name: str,
    source_role: str,
) -> ExternalIdentity:
    source_system = "xh-fish"
    identity = db.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.source_system == source_system,
            ExternalIdentity.external_user_id == external_user_id,
        )
    )
    matched_user = None
    if phone:
        matched_users = db.scalars(select(User).where(User.phone == phone)).all()
        if len(matched_users) > 1:
            raise IdentityConflict("手机号匹配到多个平台账号")
        matched_user = matched_users[0] if matched_users else None

    if identity is None:
        identity = ExternalIdentity(
            source_system=source_system,
            external_user_id=external_user_id,
        )
        db.add(identity)
    elif identity.platform_user_id and matched_user and identity.platform_user_id != matched_user.id:
        raise IdentityConflict("外部身份已关联其他平台账号")

    # 确定要绑定的平台账号：已绑 → 沿用；按手机号命中 → 复用；都未命中 → 自动开新号
    if identity.platform_user_id is not None:
        platform_user = db.get(User, identity.platform_user_id)
        if platform_user is None:
            # 关联的平台账号被删了，按"未关联"重新走自动开号流程
            platform_user = _create_platform_user(
                db,
                external_user_id=external_user_id,
                phone=phone,
                display_name=display_name,
                source_role=source_role,
            )
            identity.platform_user_id = platform_user.id
    elif matched_user is not None:
        platform_user = matched_user
        identity.platform_user_id = matched_user.id
    else:
        platform_user = _create_platform_user(
            db,
            external_user_id=external_user_id,
            phone=phone,
            display_name=display_name,
            source_role=source_role,
        )
        identity.platform_user_id = platform_user.id

    _sync_platform_user_fields(platform_user, phone=phone, display_name=display_name)
    identity.phone = phone
    identity.display_name = display_name
    identity.source_role = source_role
    db.flush()
    return identity
