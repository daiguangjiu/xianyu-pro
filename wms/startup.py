"""检测仓的启动动作（幂等，随时可以重跑）。

由宿主决定什么时候调：合并部署时挂在渔枢系统的 lifespan 里（``app/main.py``），
独立部署时由 ``wms.main`` 自己调。**不依赖平台_Base 的初始化** —— 表是自己的。
"""
from __future__ import annotations

import logging
from typing import Any

from . import permissions, print_settings, repairs, seed
from .db import SessionLocal, init_tables

logger = logging.getLogger("wms.startup")


def startup() -> dict[str, Any]:
    """建自己的表 → 兜底内置角色 → 内置仓库 / 规则播种 → 报修超时打标。

    任何一步炸了都不拖累宿主：这就是「独立系统」该有的样子。
    """
    out: dict[str, Any] = {}
    try:
        init_tables()
    except Exception as exc:  # noqa: BLE001
        logger.warning("检测仓建表失败：%s", exc)
        return {"ok": False, "error": f"init_tables: {exc}"}

    db = SessionLocal()
    try:
        try:
            out["roles"] = len(permissions.ensure_default_roles(db))
        except Exception as exc:  # noqa: BLE001
            logger.warning("检测仓内置角色兜底失败：%s", exc)
            out.setdefault("errors", []).append(f"roles: {exc}")
        try:
            out["seed"] = seed.ensure_defaults(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("检测仓规则播种失败：%s", exc)
            out.setdefault("errors", []).append(f"seed: {exc}")
        try:
            out["repair_timeouts"] = repairs.mark_timeouts(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("报修超时打标失败：%s", exc)
            out.setdefault("errors", []).append(f"repairs: {exc}")
        try:
            # 标签模板升级：老模板里中文用的是英数字体（f=1~6）→ 打出来是拉丁乱码，
            # 用户没自己改过就自动换成新版（2026-10-10）
            out["label_template"] = print_settings.ensure_template(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("标签模板升级失败：%s", exc)
            out.setdefault("errors", []).append(f"template: {exc}")
    finally:
        db.close()
    out["ok"] = not out.get("errors")
    return out
