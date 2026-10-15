"""Signed read-only category API for xh-fish.

提供渔货栈侧的三级类目，供闲参谋在货源检索页快速过滤商品。

数据来源（与 `src.goods.catalog.sourcing_tree` 一致）：
  · 优先读渔枢系统 `product_categories` 表（运营在「货源 · 类目管理」维护的那份）；
  · 表为空时回落 `SOURCING_CATEGORY_TREE` + `SOURCING_SUB_LEAVES` 内置常量，
    保证独立部署 / 表还没播种也能返回 1688 那套类目。

返回形态：扁平列表 + 父级 id 引用，由闲参谋前端自行拼树。
- `id` / `parentId` 用渔货栈原始 1688 id（`source_id`），与 `SupplierItem.category_id`
  直接对应，闲参谋可以原样回填到商品检索的 `category` 查询串。
- 三级没有独立 1688 id，约定为 `{二级id}_l3_{N}`，并用 `name` 字段做实际过滤值；
  `value` 字段就是闲参谋回填时该用的字符串（L1/L2 = id、L3 = name）。
- 60 秒缓存与 `hub_category_rows` 对齐，类目改完最多等一分钟生效。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import get_settings
from src.deps import get_db
from src.goods.catalog import SOURCING_CATEGORY_TREE, SOURCING_SUB_LEAVES
from app.models import ProductCategory

from .products import _authorized

router = APIRouter(prefix="/openapi/v1/xh-fish/categories", tags=["openapi"])

_CACHE_TTL = 60.0
_category_cache: tuple[float, list[dict[str, Any]], str] | None = None


def _path_of(node: ProductCategory, by_id: dict[int, ProductCategory]) -> str:
    names: list[str] = []
    cur: ProductCategory | None = node
    guard = 0
    while cur is not None and guard < 4:
        names.append(cur.name)
        cur = by_id.get(cur.parent_id) if cur.parent_id else None
        guard += 1
    return " / ".join(reversed(names))


def _flat_from_hub(db: Session) -> list[dict[str, Any]]:
    rows = list(db.scalars(
        select(ProductCategory)
        .where(ProductCategory.enabled.is_(True), ProductCategory.name != "")
        .order_by(ProductCategory.sort_order, ProductCategory.id)
    ))
    if not rows:
        return []
    by_id = {row.id: row for row in rows}
    items: list[dict[str, Any]] = []
    for row in rows:
        parent = by_id.get(row.parent_id) if row.parent_id else None
        if parent is not None and not parent.enabled:
            # 父级已停用，子级也不应该出现在公开接口里
            continue
        node_id = row.source_id or f"hub_{row.id}"
        parent_id = parent.source_id if parent and parent.source_id else (
            f"hub_{parent.id}" if parent else ""
        )
        items.append({
            "id": node_id,
            "name": row.name,
            "level": int(row.level),
            "parentId": parent_id,
            "path": _path_of(row, by_id),
            # L1/L2 用 1688 id 回填（命中 SupplierItem.category_id），L3 用 name 回填
            # （命中 SupplierItem.category_l3）。前端无需判断层级，统一用 value。
            "value": node_id if int(row.level) < 3 else row.name,
        })
    return items


def _flat_from_builtin() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for top in SOURCING_CATEGORY_TREE:
        top_id = top["id"]
        items.append({
            "id": top_id,
            "name": top["name"],
            "level": 1,
            "parentId": "",
            "path": top["name"],
            "value": top_id,
        })
        for sub in top.get("children") or []:
            sub_id = sub["id"]
            items.append({
                "id": sub_id,
                "name": sub["name"],
                "level": 2,
                "parentId": top_id,
                "path": f"{top['name']} / {sub['name']}",
                "value": sub_id,
            })
            for index, leaf_name in enumerate(SOURCING_SUB_LEAVES.get(sub_id, ())):
                leaf_id = f"{sub_id}_l3_{index}"
                items.append({
                    "id": leaf_id,
                    "name": leaf_name,
                    "level": 3,
                    "parentId": sub_id,
                    "path": f"{top['name']} / {sub['name']} / {leaf_name}",
                    "value": leaf_name,
                })
    return items


def _load_categories(db: Session) -> tuple[list[dict[str, Any]], str]:
    """读 + 缓存；表为空时回落内置常量。返回 (items, source) 。"""
    global _category_cache
    now = time.time()
    if _category_cache and now - _category_cache[0] < _CACHE_TTL:
        return _category_cache[1], _category_cache[2]

    items = _flat_from_hub(db)
    if items:
        source = "hub"
    else:
        items = _flat_from_builtin()
        source = "builtin"
    _category_cache = (now, items, source)
    return items, source


def invalidate_cache() -> None:
    """类目表被改时（管理页 / 同步脚本）主动清缓存。"""
    global _category_cache
    _category_cache = None


@router.get("")
async def list_categories(
    request: Request,
    q: str = Query(""),
    level: int | None = Query(None, ge=1, le=3),
    parent_id: str = Query("", alias="parentId"),
    db: Session = Depends(get_db),
    x_client_id: str | None = Header(default=None),
    x_timestamp: str | None = Header(default=None),
    x_nonce: str | None = Header(default=None),
    x_signature: str | None = Header(default=None),
):
    _authorized(request, x_client_id, x_timestamp, x_nonce, x_signature)
    items, source = _load_categories(db)

    needle = q.strip().lower()
    if needle:
        items = [
            it for it in items
            if needle in it["name"].lower() or needle in it["path"].lower() or needle == it["id"].lower()
        ]
    if level is not None:
        items = [it for it in items if it["level"] == level]
    if parent_id:
        items = [it for it in items if it["parentId"] == parent_id]

    return {
        "items": items,
        "total": len(items),
        "source": source,
        "updatedAt": int(time.time()),
    }
