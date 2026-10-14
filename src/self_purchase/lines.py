"""把货源整理成「可采购的一行」—— 采购车 / 结算页 / 采购单共用一套口径。

单价：**该规格的成本价**（sku 级有价就用 sku 的，与详情页 SKU 表「成本」列同一个数），
没填就回落到商品级渔货价（`src.goods.hub_price.resolve_cost`，渔枢系统渔货价优先）。

库存：同样扣掉已预占（SkuMapping.reserved_stock），页面上显示多少就能买多少。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.goods.hub_price import resolve_cost
from src.models import SkuMapping

TWO = Decimal("0.01")


@dataclass
class BuyLine:
    """一条可采购的明细（结算页那一行）。"""

    item_id: str
    sku_id: str
    spec_text: str
    title: str
    image: str | None
    price: Decimal
    qty: int
    stock: int  # 可售（已扣预占）
    supplier_id: str
    supplier_name: str
    # 来自采购车时带上行 id（结算页改数量要回写采购车）；直接采购为 None
    line_id: int | None = None

    @property
    def amount(self) -> Decimal:
        """小计 = 单价 × 数量。"""
        return (Decimal(str(self.price)) * int(self.qty)).quantize(TWO)


def supplier_name_of(item) -> str:
    """结算页「店铺明细」那栏的归属：优先供应商，没有就写平台货源。"""
    name = str(getattr(item, "supplier_id", None) or "").strip()
    return f"供应商 {name}" if name else "渔货栈 · 平台货源"


def fish_price_of(item) -> Decimal:
    """商品级渔货价（向平台采购价）。"""
    return Decimal(str(resolve_cost(item).value or 0)).quantize(TWO)


def freight_of(item) -> Decimal:
    """整单运费口径：包邮（free）为 0，固定运费按商品上的运费，其余（距离/自提）也按 0 计。"""
    mode = str(getattr(item, "shipping_mode", None) or "free")
    if mode == "fixed":
        return Decimal(str(getattr(item, "shipping_fee", 0) or 0)).quantize(TWO)
    return Decimal("0.00")


def sku_rows_of(item) -> dict[str, dict]:
    """sku_id -> 规格原始行（sku_list.skus）。"""
    rows = (getattr(item, "sku_list", None) or {}).get("skus") or []
    return {
        str(r.get("sku_id") or ""): r
        for r in rows
        if isinstance(r, dict)
    }


def _reserved_of(db: Session, item_id: str, sku_id: str) -> int:
    if sku_id:
        row = (
            db.execute(
                select(SkuMapping).where(
                    SkuMapping.item_id == item_id, SkuMapping.ali_sku_id == sku_id
                )
            )
            .scalars()
            .first()
        )
        return int(row.reserved_stock or 0) if row else 0
    total = db.scalar(
        select(func.coalesce(func.sum(SkuMapping.reserved_stock), 0)).where(
            SkuMapping.item_id == item_id
        )
    )
    return int(total or 0)


def line_of(db: Session, item, *, sku_id: str = "", qty: int = 1) -> BuyLine:
    """把货源 + 规格 + 数量整理成一条可采购明细（数量会被夹到 [1, 可售]）。"""
    rows = sku_rows_of(item)
    raw = rows.get(str(sku_id or "")) or {}
    if raw:
        key = str(raw.get("sku_id") or "")
        spec_text = str(raw.get("spec_text") or raw.get("spec") or "默认规格")
        price = Decimal(str(raw.get("price") or 0)).quantize(TWO)
        if price <= 0:
            price = fish_price_of(item)
        stock = int(raw.get("stock") or 0)
    else:
        key = ""
        spec_text = "默认规格"
        price = fish_price_of(item)
        stock = int(getattr(item, "stock", 0) or 0)

    available = max(0, stock - _reserved_of(db, item.item_id, key))
    images = list(getattr(item, "images", None) or [])
    image = getattr(item, "main_image", None) or (images[0] if images else None)
    return BuyLine(
        item_id=item.item_id,
        sku_id=key,
        spec_text=spec_text,
        title=str(getattr(item, "title", "") or ""),
        image=str(image) if image else None,
        price=price,
        qty=max(1, min(int(qty or 1), max(1, available))),
        stock=available,
        supplier_id=str(getattr(item, "supplier_id", None) or ""),
        supplier_name=supplier_name_of(item),
    )
