"""闲鱼渠道适配。

本期目标：半自动上架（生成草稿，人工到闲鱼客户端点发布）。
后续可对接开放平台/爬虫实现全自动。
"""

from src.channel_xianyu.prepare import (
    XIANYU_TITLE_MAX_LEN,
    XianyuListingDraft,
    XianyuSkuDraft,
    export_csv,
    export_json,
    prepare_listing,
    prepare_listing_price,
    prepare_main_image,
    prepare_pending_listings,
    prepare_skus,
    prepare_title,
    prepare_total_stock,
)
from src.channel_xianyu.service import generate_listing_draft

__all__ = [
    # 草稿
    "XianyuSkuDraft",
    "XianyuListingDraft",
    # 单字段生成
    "prepare_title",
    "prepare_listing_price",
    "prepare_total_stock",
    "prepare_main_image",
    "prepare_skus",
    # 主入口
    "prepare_listing",
    "prepare_pending_listings",
    # 导出
    "export_csv",
    "export_json",
    # 常量
    "XIANYU_TITLE_MAX_LEN",
    # 历史
    "generate_listing_draft",
]