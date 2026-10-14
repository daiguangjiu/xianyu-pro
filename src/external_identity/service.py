"""External identity synchronization service."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User
from src.models.external_identity import ExternalIdentity


class IdentityConflict(Exception):
    """The external identity or matching platform user is already claimed."""


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

    if identity.platform_user_id is None and matched_user:
        identity.platform_user_id = matched_user.id
    identity.phone = phone
    identity.display_name = display_name
    identity.source_role = source_role
    db.flush()
    return identity
