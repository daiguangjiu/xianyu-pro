"""系统更新日志：渔枢系统 + 渔货栈 + 渔集，一个页面按子系统看。

- 渔枢系统的记录维护在 `config/changelog.json`（改完刷新页面就生效，不用重启）；
- 渔货栈的记录维护在 `src/version.py` 的 `CHANGELOG`（渔枢系统自己发版时往上加）；
- 渔集的记录**也写在 `config/changelog.json` 里**，靠条目上的 `system: "wms"` 区分
  （2026-10-08 用户要求「划分清楚」：以前渔集的更新被当成渔枢系统的，页面上标错端）。

三个子系统各记各的，页面按端合在一起展示，也可以只看某一个端
（`?portal=hub|self|wms`）。**没写 `system` 的老记录一律当渔枢系统**。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..config import BASE_DIR
from ..portals import PORTAL_HUB, PORTAL_SELF, PORTAL_WMS, portal

logger = logging.getLogger(__name__)

# 条目上的 `system` 字段 → 子系统键。认不出来的（含老记录的缺失值）归渔枢系统。
_SYSTEMS: tuple[str, ...] = (PORTAL_HUB, PORTAL_SELF, PORTAL_WMS)


def _portal_of(system: str) -> str:
    key = str(system or "").strip().lower()
    return key if key in _SYSTEMS else PORTAL_HUB

CHANGELOG_PATH = BASE_DIR / "config" / "changelog.json"

_cache: dict[str, Any] = {"mtime": 0.0, "entries": []}


def path() -> Path:
    return CHANGELOG_PATH


def version_key(version: str) -> tuple[int, ...]:
    """v1.20 → (1, 20)：按数值比大小，避免字符串排序把 v1.9 排到 v1.20 前面。"""
    nums = re.findall(r"\d+", str(version or ""))
    return tuple(int(n) for n in nums) if nums else (0,)


def load(force: bool = False) -> list[dict]:
    """读取更新日志；文件缺失或格式错误时返回空列表（不影响页面）。"""
    try:
        mtime = CHANGELOG_PATH.stat().st_mtime
    except OSError:
        return []
    if not force and _cache["mtime"] == mtime and _cache["entries"]:
        return list(_cache["entries"])
    entries: list[dict] = []
    try:
        data = json.loads(CHANGELOG_PATH.read_text(encoding="utf-8"))
        raw = data.get("entries") if isinstance(data, dict) else data
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            entries.append(
                {
                    "version": str(item.get("version") or "").strip(),
                    "date": str(item.get("date") or "").strip(),
                    "title": str(item.get("title") or "").strip(),
                    # system 决定这条记录归哪个子系统（渔集写在同一个文件里就靠它区分）
                    "system": str(item.get("system") or "").strip().lower(),
                    "tags": [str(x).strip() for x in (item.get("tags") or []) if str(x).strip()],
                    "items": [str(x).strip() for x in (item.get("items") or []) if str(x).strip()],
                }
            )
    except (OSError, ValueError) as exc:
        logger.warning("更新日志读取失败: %s", exc)
        return list(_cache["entries"])
    entries.sort(key=lambda e: (e["date"], version_key(e["version"])), reverse=True)
    _cache["mtime"] = mtime
    _cache["entries"] = entries
    return list(entries)


def hub_entries() -> list[dict]:
    """渔货栈的更新日志（维护在 `src/version.py` 的 CHANGELOG）。

    渔枢系统没接上（依赖没装、代码没并入）时返回空，页面照常显示渔枢系统的记录。
    函数名是历史遗留（早年 self / hub 两个键是反的），内容一直是渔货栈。
    """
    try:
        from src.version import CHANGELOG as raw_rows
    except Exception:  # noqa: BLE001
        return []
    rows: list[dict] = []
    for item in raw_rows or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "version": str(item.get("version") or "").strip(),
                "date": str(item.get("date") or "").strip(),
                "title": str(item.get("title") or "").strip(),
                "system": PORTAL_SELF,  # 这一份记录本来就属于渔货栈
                "tags": [str(x).strip() for x in (item.get("tags") or []) if str(x).strip()],
                "items": [str(x).strip() for x in (item.get("items") or []) if str(x).strip()],
            }
        )
    return rows


def load_all(portal_key: str = "") -> list[dict]:
    """三个子系统的更新日志合在一起（带 `portal` / `portal_name`），按日期倒序。

    `portal_key` 为空表示全部；传 `hub` / `self` / `wms` 只看某一个子系统。
    **渔集的记录也在 config/changelog.json 里，按条目上的 `system` 分流** ——
    没写 system 的老记录当渔枢系统（升级不动老数据）。
    """
    merged: list[dict] = []
    # load() = config/changelog.json（渔枢系统 + 渔集的记录），hub_entries() = 渔货栈的记录
    for row in load():
        key = _portal_of(row.get("system"))
        merged.append({**row, "portal": key, "portal_name": portal(key)["name"]})
    for row in hub_entries():
        merged.append({**row, "portal": PORTAL_SELF, "portal_name": portal(PORTAL_SELF)["name"]})
    if portal_key:
        merged = [r for r in merged if r["portal"] == portal_key]
    merged.sort(key=lambda e: (e["date"], version_key(e["version"])), reverse=True)
    return merged


def latest(count: int = 1) -> list[dict]:
    return load()[: max(int(count), 1)]


def current_version() -> str:
    rows = load()
    return rows[0]["version"] if rows else ""
