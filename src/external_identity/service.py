"""External identity synchronization service."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User
from src.models.external_identity import ExternalIdentity


class IdentityConflict(Exception):
    """The external identity or matching platform user is already claimed."""


class PlatformAccountNotFound(Exception):
    """No platform account exists; manual purchase-access approval is required."""


def sync_xianmou_identity(
    db: Session,
    *,
    external_user_id: str,
    phone: str | None,
    display_name: str,
    source_role: str,
) -> ExternalIdentity | None:
    """关联闲参谋身份到已存在的平台账号；不自动创建平台账号。

    返回 None 表示平台不存在该手机号，调用方应提示联系专员人工申请渔货栈采购权限。
    已关联的账号继续沿用；关联账号已被删除时，仅当手机号重新命中现存账号才可重绑。
    """
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

    if identity and identity.platform_user_id:
        linked_user = db.get(User, identity.platform_user_id)
        if linked_user is not None:
            if matched_user is not None and matched_user.id != linked_user.id:
                raise IdentityConflict("外部身份已关联其他平台账号")
            platform_user = linked_user
        elif matched_user is not None:
            platform_user = matched_user
            identity.platform_user_id = matched_user.id
        else:
            # 原关联账号已删除且没有手机号命中的新账号，不重建账号/关联。
            identity.phone = phone
            identity.display_name = display_name
            identity.source_role = source_role
            db.flush()
            raise PlatformAccountNotFound("未找到对应的平台账号，请联系客服专员人工申请渔货栈采购权限")
    elif matched_user is not None:
        platform_user = matched_user
        if identity is None:
            identity = ExternalIdentity(
                source_system=source_system,
                external_user_id=external_user_id,
            )
            db.add(identity)
        identity.platform_user_id = matched_user.id
    else:
        # 不产生未审批的平台用户，也不创建未绑定的 external identity 记录。
        raise PlatformAccountNotFound("未找到对应的平台账号")

    if phone and not platform_user.phone:
        platform_user.phone = phone
    if display_name and not platform_user.display_name:
        platform_user.display_name = display_name
    identity.phone = phone
    identity.display_name = display_name
    identity.source_role = source_role
    db.flush()
    return identity
