"""闲鱼上架草稿生成（MVP：半自动）。"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from src.models import ChannelListing, SupplierItem
from src.pricing.engine import calculate_price, pick_rule

logger = logging.getLogger(__name__)


def generate_listing_draft(
    db: Session, item: SupplierItem, *, channel: str = "xianyu", owner: str = ""
) -> ChannelListing:
    """根据 SupplierItem 生成 ChannelListing(status=draft)。

    TODO:
    - 标题模板：{1688 标题} + 渠道标签
    - 价格：pricing.calculate_price
    - 库存 = SupplierItem.stock - SkuMapping.reserved_stock
    - 防重：UniqueConstraint(item_id, channel)
    - 改价阈值：与 last_published_price 比较，超阈值 → status=needs_review
    """
    rule = pick_rule(db, channel=channel, category_id=item.category_id, supplier_id=item.supplier_id)
    price = calculate_price(item, rule)

    listing = ChannelListing(
        item_id=item.item_id,
        channel=channel,
        owner=str(owner or ""),
        status="draft",
        suggested_price=price,
        suggested_title=item.title,
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return listing


__all__ = ["generate_listing_draft"]