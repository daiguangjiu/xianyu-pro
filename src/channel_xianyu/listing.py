"""铺货记录落库（ChannelListing）：让「上架到店铺 / 仅生成草稿」在自己系统里留痕。

背景：以前详情页点「上架到店铺」只是生成一份预填包 + 打开闲鱼发布页，**不写库** ——
「我的上架」里看不到任何记录，运营也不知道这款货带出去过几次。

现在统一走 `save_listing()`：

- `source="handoff"`：从「上架到店铺」带入闲鱼发布页（同时记 `handoff_at`）
- `source="draft"`：只在自己系统存一份草稿（「仅生成草稿」）
- `source="selection"`：货源广场按选品规则批量跑出来的（老行为，见 `src/selection/engine.py`）

约定（不要回退）：

1. 幂等：按 (item_id, channel) upsert，同一款货只有一条记录；
2. **已 published 的记录不动状态** —— 否则点一次「上架到店铺」会把已上架的商品
   拉回「待发布」，运营在「我的上架」里就看不到真相了；
3. 只补事实（来源 / 带入发布时间 / 建议价 / 建议标题），不做价格决策 ——
   价格口径仍由 `publish.build_publish_package` 与 `pricing` 说了算。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.timeutil import utc_now
from src.models import ChannelListing, SupplierItem

CHANNEL = "xianyu"

# 铺货记录状态中文化（与 src/web/router.py 的 STATUS_TEXT 同一套叫法）
STATUS_TEXT: dict[str, str] = {
    "draft": "待发布",
    # 2026-10-13 用户拍板：铺到自己的店里就算「已铺货」，不再叫「已上架」
    # （「上架」听着像已经发到闲鱼上了，而这一步只是把货收进自己的店铺商品池）
    "published": "已铺货",
    "failed": "发布失败",
    "needs_review": "待审核",
}

# 记录来源中文化
SOURCE_TEXT: dict[str, str] = {
    "handoff": "上架到店铺",
    "draft": "仅存草稿",
    "selection": "选品通过",
}


def status_text(status: str | None) -> str:
    return STATUS_TEXT.get(status or "", status or "—")


def source_text(source: str | None) -> str:
    return SOURCE_TEXT.get(source or "", source or "—")


def save_listing(
    db: Session,
    item: SupplierItem,
    *,
    owner: str = "",
    source: str = "draft",
    price: Decimal | float | str | None = None,
    title: str | None = None,
    handoff: bool = False,
    commit: bool = False,
) -> ChannelListing:
    """按 (item_id, channel, owner) upsert 一条铺货记录，返回记录本身。

    `owner` 是归属账号（`_purchase_owner` 的 key，形如 u1）—— 2026-10-13 起铺货状态
    按账号隔离：同一款货每个账号各一条，A 账号铺的货不会显示在 B 账号的卡片上。
    留空 = 平台级记录（选品引擎批量跑出来的），不参与任何账号的卡片状态。

    `handoff=True` 时同时打上「带入发布」的时间戳（`source` 会自动落成 handoff）。
    """
    if handoff:
        source = "handoff"

    owner_key = str(owner or "").strip()
    price_value = Decimal(str(price)) if price not in (None, "") else None
    row = (
        db.query(ChannelListing)
        .filter(
            ChannelListing.item_id == item.item_id,
            ChannelListing.channel == CHANNEL,
            ChannelListing.owner == owner_key,
        )
        .first()
    )

    if row is None:
        row = ChannelListing(
            item_id=item.item_id,
            channel=CHANNEL,
            owner=owner_key,
            status="draft",
            suggested_price=price_value,
            suggested_title=title or item.title,
            reject_reasons=[],
            source=source,
            handoff_at=utc_now() if handoff else None,
        )
        db.add(row)
    else:
        # 已上架的不改状态，只补「带出去过」的事实
        if row.status != "published":
            row.status = "draft"
            row.reject_reasons = []
        if price_value is not None:
            row.suggested_price = price_value
        if title or item.title:
            row.suggested_title = title or item.title
        row.source = source
        if handoff:
            row.handoff_at = utc_now()

    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def to_brief(row: ChannelListing | None) -> dict:
    """给前端 / 接口用的精简结构（页面状态条、货源广场徽章都吃这一份）。"""
    if row is None:
        return {}
    return {
        "id": row.id,
        "item_id": row.item_id,
        "channel": row.channel,
        "owner": row.owner or "",
        "status": row.status,
        "status_text": status_text(row.status),
        "source": row.source or "",
        "source_text": source_text(row.source),
        "suggested_price": float(row.suggested_price) if row.suggested_price is not None else None,
        "suggested_title": row.suggested_title or "",
        "channel_listing_id": row.channel_listing_id or "",
        "handoff_at": row.handoff_at.isoformat() if row.handoff_at else None,
    }


__all__ = ["CHANNEL", "STATUS_TEXT", "SOURCE_TEXT", "save_listing", "source_text", "status_text", "to_brief"]
