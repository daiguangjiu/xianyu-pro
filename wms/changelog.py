"""渔集自己的更新日志（2026-10-08）。

记录**不另维护一份**：平台同一份 ``config/changelog.json`` 里，标了 ``"system": "wms"``
的条目就是渔集的更新。本模块只把它挑出来给 ``/wms/changelog`` 用 ——
渔枢系统 / 渔货栈的更新不在渔集页面露面（用户要求「划分清楚」：
以前渔集的更新写在同一个文件里、页面上被标成「渔枢系统」）。

老记录没写 ``system`` 时退回看 ``tags``（历史上渔集的 tag 就是 ``wms``），
两边都不认的就不算渔集的更新。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from . import config as cfg

logger = logging.getLogger(__name__)

# 平台那份日志的位置：<项目>/config/changelog.json（本文件在 <项目>/wms/ 下）
CHANGELOG_PATH = cfg.BASE_DIR.parent / "config" / "changelog.json"
SYSTEM_KEY = "wms"

_cache: dict[str, Any] = {"mtime": 0.0, "rows": []}


def path() -> Path:
    return CHANGELOG_PATH


def version_key(version: str) -> tuple[int, ...]:
    """``v1.20`` → ``(1, 20)``：按数值比大小，避免字符串排序把 v1.9 排到 v1.20 前面。"""
    nums = re.findall(r"\d+", str(version or ""))
    return tuple(int(n) for n in nums) if nums else (0,)


def entries(force: bool = False) -> list[dict]:
    """渔集的更新记录（最新在前）；文件缺失 / 格式错时给上次结果或空列表，不炸页面。"""
    try:
        mtime = CHANGELOG_PATH.stat().st_mtime
    except OSError:
        return []
    if not force and _cache["mtime"] == mtime and _cache["rows"]:
        return list(_cache["rows"])
    rows: list[dict] = []
    try:
        data = json.loads(CHANGELOG_PATH.read_text(encoding="utf-8"))
        raw = data.get("entries") if isinstance(data, dict) else data
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            system = str(item.get("system") or "").strip().lower()
            tags = {str(x).strip().lower() for x in (item.get("tags") or []) if str(x).strip()}
            if system != SYSTEM_KEY and SYSTEM_KEY not in tags:
                continue
            rows.append(
                {
                    "version": str(item.get("version") or "").strip(),
                    "date": str(item.get("date") or "").strip(),
                    "title": str(item.get("title") or "").strip(),
                    "tags": sorted(tags - {SYSTEM_KEY}),
                    "items": [str(x).strip() for x in (item.get("items") or []) if str(x).strip()],
                }
            )
    except (OSError, ValueError) as exc:
        logger.warning("渔集更新日志读取失败: %s", exc)
        return list(_cache["rows"])
    rows.sort(key=lambda e: (e["date"], version_key(e["version"])), reverse=True)
    _cache["mtime"] = mtime
    _cache["rows"] = rows
    return list(rows)


def current_version() -> str:
    rows = entries()
    return rows[0]["version"] if rows else ""
