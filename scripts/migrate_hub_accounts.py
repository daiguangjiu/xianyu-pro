"""把渔枢系统（渔货栈）的账号迁进统一用户表。

合并两端之后只维护一套账号：这里把渔枢系统users 表里的账号搬进渔枢系统的 users 表，
手机号当登录名，端权限按渔枢系统的角色给：

  · platform_admin 平台管理员 → 管理员（默认全端）
  · merchant       商家       → 子账号（渔枢系统 + 渔货栈：既铺货也管自己的商品）
  · reseller       分销（学员）→ 子账号（渔枢系统：渔货栈的选货页按 hub_bridge 放行规则单独开）

密码不搬明文也搬不了哈希（渔枢系统用 bcrypt，这套系统用 pbkdf2）：把老哈希存进
`users.legacy_password_hash`，账号第一次用老密码登录时自动换成新格式并清空，
**已经注册过的商家不用改密码**；新账号的密码字段先填一个随机值，只能靠老密码进。

用法（跑之前先备一份库，脚本只加不删）：

    DATABASE_URL=... HUB_DATABASE_URL=... python scripts/migrate_hub_accounts.py
    python scripts/migrate_hub_accounts.py --dry-run   只打印要做什么，不写库
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select, text  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import User  # noqa: E402
from app.portals import PORTAL_HUB, join_portals  # noqa: E402
from app.web.auth import ROLE_ADMIN, ROLE_USER, hash_password  # noqa: E402

# 渔枢系统角色 →（统一账号的角色，可用端）
ROLE_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    # 平台管理员：进来就是管理员，默认全端
    "platform_admin": (ROLE_ADMIN, ("self", "hub")),
    # 商家：在渔货栈管自己的商品，也能用渔枢系统铺货
    "merchant": (ROLE_USER, ("self", "hub")),
    # 分销（学员）：默认只给渔枢系统；渔货栈的选货页由 hub_bridge 的放行规则单独开
    "reseller": (ROLE_USER, (PORTAL_HUB,)),
}

# 从渔枢系统库里读账号：手机号即登录名
_HUB_SQL = text(
    "SELECT phone, password_hash, nickname, role, status FROM users ORDER BY id"
)


def hub_database_url() -> str:
    """渔枢系统的库地址：自己配了 HUB_DATABASE_URL 就用它，否则回落 DATABASE_URL。"""
    import os

    return os.environ.get("HUB_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


def fetch_hub_users(url: str) -> list[dict]:
    if not url:
        raise SystemExit("没找到渔枢系统的库地址：请设置 HUB_DATABASE_URL（或 DATABASE_URL）")
    engine = create_engine(url, future=True)
    with engine.connect() as conn:
        rows = conn.execute(_HUB_SQL).mappings().all()
    return [dict(r) for r in rows]


def migrate(dry_run: bool = False) -> dict[str, int]:
    init_db()
    rows = fetch_hub_users(hub_database_url())
    stats = {"total": len(rows), "created": 0, "updated": 0, "skipped": 0}
    with SessionLocal() as session:
        for row in rows:
            phone = str(row.get("phone") or "").strip()
            if not phone:
                stats["skipped"] += 1
                continue
            role, portals = ROLE_MAP.get(
                str(row.get("role") or "").strip(), (ROLE_USER, (PORTAL_HUB,))
            )
            nickname = str(row.get("nickname") or "").strip() or phone
            legacy = str(row.get("password_hash") or "").strip()
            status = "active" if str(row.get("status") or "active") == "active" else "disabled"

            user = session.scalar(select(User).where(User.username == phone))
            if user is None:
                stats["created"] += 1
                if dry_run:
                    print(f"  新建 {phone}（{nickname}）角色={role} 端={','.join(portals)}")
                    continue
                session.add(
                    User(
                        username=phone,
                        display_name=nickname,
                        # 随机密码：本人不知道，只能靠 legacy 的老密码进来
                        password_hash=hash_password(secrets.token_urlsafe(24)),
                        legacy_password_hash=legacy or None,
                        role=role,
                        status=status,
                        portals=join_portals(portals),
                    )
                )
            else:
                # 已经迁过：只补端权限和老哈希，不动显示名 / 角色，避免覆盖人工调整
                changed = False
                if legacy and not user.legacy_password_hash:
                    user.legacy_password_hash = legacy
                    changed = True
                if not user.portals:
                    user.portals = join_portals(portals)
                    changed = True
                if changed:
                    stats["updated"] += 1
                    if dry_run:
                        print(f"  补齐 {phone}")
                else:
                    stats["skipped"] += 1
        if not dry_run:
            session.commit()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="把渔枢系统的账号迁进统一用户表")
    parser.add_argument("--dry-run", action="store_true", help="只打印要做什么，不写库")
    args = parser.parse_args()

    stats = migrate(dry_run=args.dry_run)
    prefix = "[预演] " if args.dry_run else ""
    print(
        f"{prefix}渔枢系统账号共 {stats['total']} 个："
        f"新建 {stats['created']} · 补齐 {stats['updated']} · 跳过 {stats['skipped']}"
    )
    if args.dry_run:
        print("（加 --dry-run 之外的参数才会真正写库）")


if __name__ == "__main__":
    main()
