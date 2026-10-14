"""把账号的端权限值对调：self（旧 = 渔枢系统）↔ hub（旧 = 渔货栈）。

v1.20 起端键按字面走：`hub` = 渔枢系统、`self` = 渔货栈 —— 以前这两个是反的
（见 `app/portals.py`）。库里 `users.portals` 存的是旧语义，不换的话老账号登录后
「能进哪个端」会整个反过来：原来只有渔枢系统的账号会变成只有渔货栈。

只跑一次：干完在 `system_flags` 里记一笔，重复执行会跳过（要重跑加 `--force`）。

用法（跑之前先备一份库）：

    python scripts/migrate_portal_keys.py            # 真跑
    python scripts/migrate_portal_keys.py --dry-run  # 只打印要改什么，不写库
    python scripts/migrate_portal_keys.py --force    # 忽略「已迁移」标记重跑
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import SystemFlag, User  # noqa: E402
from app.portals import DEFAULT_PORTAL, PORTAL_KEYS  # noqa: E402

# 一次性标记：跑过一次就记住，避免重复跑把值又换回去
FLAG_NAME = "portal_keys_v120"
# 旧值 → 新值（两个端键对调）
SWAP = {"self": "hub", "hub": "self"}


def _new_value(raw: str) -> str:
    """把旧语义的端权限字符串换成新语义（顺序也按 PORTAL_KEYS 排好）。"""
    tokens = [t.strip().lower() for t in str(raw or "").replace("，", ",").split(",")]
    swapped = [SWAP.get(t, t) for t in tokens if t]
    ordered = [k for k in PORTAL_KEYS if k in swapped]
    return ",".join(ordered) if ordered else DEFAULT_PORTAL


def main(dry_run: bool = False, force: bool = False) -> int:
    init_db()
    with SessionLocal() as s:
        if not force and s.get(SystemFlag, FLAG_NAME) is not None:
            print(f"已迁移过（system_flags.{FLAG_NAME}），跳过；要重跑加 --force")
            return 0

        users = s.scalars(select(User)).all()
        changed = 0
        for user in users:
            raw = str(user.portals or "").strip()
            if not raw:
                # 空值走默认端（渔枢系统），本来就没存旧语义
                continue
            value = _new_value(raw)
            if value == raw:
                continue
            print(f"  {user.username}: {raw} → {value}")
            changed += 1
            if not dry_run:
                user.portals = value

        if dry_run:
            print(f"[dry-run] 共 {changed} 个账号需要改，未写库")
            return 0

        s.add(SystemFlag(name=FLAG_NAME))
        s.commit()
    print(f"端权限值对调完成：共改 {changed} 个账号，已记 {FLAG_NAME}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="把账号的端权限值 self/hub 对调")
    parser.add_argument("--dry-run", action="store_true", help="只打印要改什么，不写库")
    parser.add_argument("--force", action="store_true", help="忽略「已迁移」标记重跑")
    args = parser.parse_args()
    raise SystemExit(main(dry_run=args.dry_run, force=args.force))
