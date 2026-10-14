"""从渔枢系统（写入库）读渔货价（2026-09-30 起）。

渔货栈的商品来自渔枢系统的「自营品」与「供应链商品」—— 两边是同一份数据，
渔枢系统那侧是**写入库**、渔货栈这侧是**读取库**。所以渔货栈商品详情页的成本价，
应当直接取渔枢系统的渔货价（products.fish_price），利润也按它算。

怎么连到渔枢系统库（按顺序试，命中即停）：

  1. 配了 `PLATFORM_DATABASE_URL` —— 渔货栈独立部署时的正常姿势：**直连渔枢系统
     库**，只跑一句 SQL，不依赖渔枢系统那套代码（两个镜像里并没有彼此的代码）；
  2. 同一个进程里就装着渔枢系统那套（本地一体化部署 / 测试）—— 借它的会话查；
  3. 都不行 → 返回 None，上层按渔货栈自己的口径回落。

怎么找到对应的那条渔枢系统商品（按顺序试，命中即停）：

  1. **商家编码**：`supplier_items.item_code` ↔ `products.code`
     —— 两边用的是同一套编码规则（见 `src/goods/service.py::next_item_code`）；
  2. **商品名**：`title` ↔ `name`，去空格后精确匹配；
  3. 都取不到 → 回落渔货栈自己的 `fish_price`（再不行才是采购价），页面标注来源。

读不到一律 None 让上层回落 —— 绝不因为读渔枢系统失败就让商品详情页打不开。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

# 渔货栈的商品来源就是这两类渔枢系统商品：自营品 + 供应链商品
_HUB_SOURCE_TYPES = ("self_operated", "third_party")

# 来源标识：页面按它决定「成本价（xxx）」括号里写什么
SRC_HUB = "hub"
SRC_SELF = "self"
SRC_PURCHASE = "purchase"

_ENGINE = None


@dataclass
class CostInfo:
    """渔货栈商品的成本口径（值 + 来源，页面要显示来源）。"""

    value: Decimal
    source: str                 # hub | self | purchase
    label: str                  # 「成本价（{{ label }}）」
    note: str = ""              # 为什么是这个来源（页面提示 / 日志）
    matched: str = ""           # 匹配到的渔枢系统商品名（没有则空）


def _engine(dsn: str):
    """渔枢系统库的引擎（建一次就复用；断线自动重连）。"""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_engine(dsn, pool_pre_ping=True, pool_recycle=280)
    return _ENGINE


def _hub_orm():
    """同进程里有渔枢系统那套代码时借用它的会话；独立部署时返回 (None, None)。"""
    try:
        from app.db import SessionLocal  # noqa: PLC0415
        from app.models import Product  # noqa: PLC0415

        return SessionLocal, Product
    except Exception:  # noqa: BLE001
        return None, None


def _flat(text_value: str) -> str:
    """名称比对用：去掉所有空白、统一小写（录入常带多余空格）。"""
    return "".join((text_value or "").split()).lower()


def _positive(value) -> bool:
    """渔货价 > 0 才算定价了（渔枢系统未定价是 0，不能拿 0 当成本）。"""
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError):
        return False


def _load_rows() -> tuple[list[tuple[str, str, float]] | None, str]:
    """读渔枢系统的商品（code / name / fish_price）；读不到返回 (None, 原因)。"""
    dsn = _platform_dsn()
    if dsn:
        try:
            with _engine(dsn).connect() as conn:
                rows = conn.execute(_sql_with_source(conn)).fetchall()
            return [(r[0] or "", r[1] or "", float(r[2] or 0)) for r in rows], ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("直连渔枢系统库读渔货价失败（回落渔货栈口径）: %s", exc)
            return None, "连渔枢系统库失败"

    session_factory, product_model = _hub_orm()
    if session_factory is None:
        return None, "没配 PLATFORM_DATABASE_URL，也没装渔枢系统那套代码（独立部署）"
    try:
        with session_factory() as db:
            query = db.query(product_model)
            if hasattr(product_model, "source_type"):
                query = query.filter(product_model.source_type.in_(_HUB_SOURCE_TYPES))
            rows = query.all()
        return [(r.code or "", r.name or "", float(r.fish_price or 0)) for r in rows], ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("借渔枢系统会话读渔货价失败（回落渔货栈口径）: %s", exc)
        return None, "读渔枢系统库失败"


def _platform_dsn() -> str:
    """渔枢系统库地址。

    直接读环境变量，不走 Settings —— 渔货栈是独立部署的容器，里面的 config
    版本未必有 platform_database_url 这个字段（读不到就当没配，走回落）。
    """
    dsn = (os.environ.get("PLATFORM_DATABASE_URL") or "").strip()
    if dsn:
        return dsn
    # 容器里这个变量写在工作目录的 .env 里（启动参数是几年前定的，不带它），
    # 自己读一下；读不到就当没配。
    for path in (".env", "/app/.env"):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("PLATFORM_DATABASE_URL="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            continue
    try:
        from src.config import get_settings  # noqa: PLC0415

        return (getattr(get_settings(), "platform_database_url", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _sql_with_source(conn):
    """渔枢系统商品查询：老库可能没有 source_type 列，那就不过滤来源。

    值直接写在 SQL 里（不是参数绑定），因为带不带过滤是两条不同的语句。
    """
    try:
        has_column = conn.execute(
            text(
                "select 1 from information_schema.columns "
                "where table_name = 'products' and column_name = 'source_type' limit 1"
            )
        ).fetchone()
    except Exception:  # noqa: BLE001  连系统表都查不了，就按没有这列处理
        return text("select code, name, fish_price from products")
    if has_column:
        return text(
            "select code, name, fish_price from products "
            "where source_type in ('self_operated', 'third_party')"
        )
    return text("select code, name, fish_price from products")


def hub_fish_price(item) -> tuple[Decimal | None, str, str]:
    """渔枢系统对应商品的渔货价。

    返回 `(价格, 说明, 匹配到的商品名)`；取不到时价格是 None，说明里写原因。
    """
    rows, reason = _load_rows()
    if rows is None:
        return None, reason, ""

    code = (getattr(item, "item_code", "") or "").strip()
    title = (getattr(item, "title", "") or "").strip()
    if not code and not title:
        return None, "这个商品既没有商家编码也没有名称，没法在渔枢系统定位", ""

    # 1) 商家编码：两边同一套编码规则，最可靠
    if code:
        for row_code, row_name, fish in rows:
            if (row_code or "").strip() == code and _positive(fish):
                return Decimal(str(fish)), f"按商家编码 {code} 匹配到渔枢系统商品", row_name
    # 2) 商品名精确匹配（编码还没同步上时靠它）
    if title:
        flat = _flat(title)
        for row_code, row_name, fish in rows:
            if _flat(row_name) == flat and _positive(fish):
                return Decimal(str(fish)), "按商品名匹配到渔枢系统商品", row_name

    return None, "渔枢系统没有对应商品，或那边还没填渔货价", ""


def resolve_cost(item) -> CostInfo:
    """渔货栈商品的成本价：渔枢系统渔货价优先，取不到按渔货栈自己的口径回落。"""
    hub_value, note, matched = hub_fish_price(item)
    if hub_value is not None:
        return CostInfo(hub_value, SRC_HUB, "渔枢系统渔货价", note, matched)

    own = getattr(item, "fish_price", None)
    if own is not None:
        return CostInfo(Decimal(str(own)), SRC_SELF, "渔货栈渔货价", note)
    return CostInfo(
        Decimal(str(getattr(item, "cost_price", 0) or 0)),
        SRC_PURCHASE,
        "采购价（还没定渔货价）",
        note,
    )
