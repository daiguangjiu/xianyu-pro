"""闲鱼上架草稿生成（半自动通道）。

设计约束（来自“底层”授权）：
- 闲鱼“始终”不提供自动选品接口 → 本平台就是没开放API
- 本期“只在本地”生成上架铺货 / CSV / JSON 推给人工发布走流程
- **绝对不**模拟登录、调舰号、碰 access_token/密码
- 可选：后续接入 “開放平台” / 代理 / 爬虫 实现全自动（不修改本文件 接口）

输入：ChannelListing(status='draft', channel='xianyu') + SupplierItem
输出：XianyuListingDraft（包含 title / price / skus / main_image / 营销文案）
       + :func:`export_csv` + :func:`export_json`

与上游各层接口的关系：
    - 选品压价完成后调用 :func:`prepare_pending_listings` 获得铺货列表
    - 人工下单后批量调用 /xianyu/* 路由 上的 导出接口 拿到 脱机交付文件
"""
from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from sqlalchemy.orm import Session

from src.models import ChannelListing, SkuMapping, SupplierItem
from src.pricing.engine import calculate_price, pick_rule
from app.timeutil import to_iso, utc_now

logger = logging.getLogger(__name__)


# =====================================================================
# 常量
# =====================================================================
# 闲鱼平台属性（【重要】): 闲鱼“始终”未提供自动开放API
# - 标题 30 字符限制
# - 上架必须人工（手机 App/PC 客户端）
XIANYU_TITLE_MAX_LEN = 30

# 1688 货运词过滤（仅限标题层）
_XIANYU_BANNED_SUBSTRINGS = [
    "1688",
    "alibaba",
    "跨境",
    "厂家直销",
    "货源",
    "一件代发",
    "代发",
    "微商",
    "陶陶",
    "招代理",
    "全网最低",
    "出口",
]

# 营销标签池（可选，随状态产出）
_DEFAULT_TAGS = ["工厂价", "七天无理由", "现货秒发"]


# =====================================================================
# 数据结构
# =====================================================================
@dataclass
class XianyuSkuDraft:
    """单个 SKU 在闲鱼端的可上架表示。"""

    ali_sku_id: str
    channel_sku_id: str | None       # SkuMapping 中已映射则取；未上架则 None
    spec_text: str                   # 规格文案，如 "白色 / L"
    cost_price: Decimal              # 1688 进价
    sale_price: Decimal                # 闲鱼售价（加价后）
    available_stock: int            # max(0, stock - reserved_stock)


@dataclass
class XianyuListingDraft:
    """一个商品在闲鱼端的完整上架草稿。

    字段仅含“人工上架需要填进闲鱼 App”的属性：
    - title / suggested_price / total_available_stock / main_image
    - skus：可上架的规格价格
    - tags / description_hint：营销文案
    """

    item_id: str
    title: str
    suggested_price: Decimal
    total_available_stock: int
    main_image: str | None
    image_count: int
    skus: list[XianyuSkuDraft] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    description_hint: str = ""
    category_id: str | None = None
    supplier_id: str | None = None
    prepared_at: datetime = field(default_factory=lambda: utc_now())

    # ----- 用于 CSV / JSON 导出 -----
    def to_csv_row(self) -> dict[str, str]:
        """输出 人工上架可直接填进 闲鱼表格 的一行。"""
        return {
            "item_id": self.item_id,
            "title": self.title,
            "suggested_price": str(self.suggested_price),
            "total_available_stock": str(self.total_available_stock),
            "main_image": self.main_image or "",
            "image_count": str(self.image_count),
            "sku_count": str(len(self.skus)),
            "sku_summary": _summarize_skus(self.skus),
            "tags": "|".join(self.tags),
            "description_hint": self.description_hint,
            "category_id": self.category_id or "",
            "supplier_id": self.supplier_id or "",
            "prepared_at": self.prepared_at.isoformat(),
        }

    def to_json_dict(self) -> dict:
        """JSON 序列化友好（Decimal → str）。"""
        data = asdict(self)
        data["suggested_price"] = str(data["suggested_price"])
        data["prepared_at"] = self.prepared_at.isoformat()
        for s in data["skus"]:
            s["cost_price"] = str(s["cost_price"])
            s["sale_price"] = str(s["sale_price"])
        return data


# =====================================================================
# 草稿生成（主入口）
# =====================================================================
def prepare_listing(
    item: SupplierItem,
    db: Session,
    *,
    channel: str = "xianyu",
    listing: ChannelListing | None = None,
) -> XianyuListingDraft:
    """为单个 SupplierItem 生成完整闲鱼上架草稿。

    入参：
        item      SupplierItem（已被选品选上，ChannelListing.status='draft'）
        db        SQLAlchemy Session
        channel   下游渠道（默认闲鱼）
        listing   可选；传入可拿上一轮草稿状态控制 behavior

    输出：
        XianyuListingDraft 包含 title / price / skus / main_image / tags
        · 不读 / 不写账号密码、不调外部 API
    """
    listing_draft = XianyuListingDraft(
        item_id=item.item_id,
        title=prepare_title(item),
        suggested_price=prepare_listing_price(item, db, channel=channel),
        total_available_stock=prepare_total_stock(item, db, channel=channel),
        main_image=prepare_main_image(item),
        image_count=_count_images(item),
        skus=prepare_skus(item, db, channel=channel),
        tags=list(_DEFAULT_TAGS),
        description_hint=_build_description_hint(item),
        category_id=item.category_id,
        supplier_id=item.supplier_id,
    )

    # 价格区间保护（PriceRule 可能设了 price_min / price_max）
    rule = pick_rule(
        db, channel=channel, category_id=item.category_id, supplier_id=item.supplier_id
    )
    if rule is not None and rule.price_min is not None:
        listing_draft.suggested_price = max(
            listing_draft.suggested_price, rule.price_min
        )
        for sku in listing_draft.skus:
            sku.sale_price = max(sku.sale_price, rule.price_min)

    return listing_draft


def prepare_pending_listings(
    db: Session,
    *,
    channel: str = "xianyu",
    item_ids: list[str] | None = None,
) -> list[XianyuListingDraft]:
    """批量准备草稿：拉 ChannelListing.status='draft' + 对应 SupplierItem，生成闲鱼铺货。

    入参：
        db       SQLAlchemy Session
        channel  渠道（默认 xianyu）
        item_ids 限定 item_id 列表；None = 所有 draft

    输出：
        list[XianyuListingDraft]
    """
    q = db.query(ChannelListing, SupplierItem).join(
        SupplierItem, ChannelListing.item_id == SupplierItem.item_id
    )
    q = q.filter(
        ChannelListing.channel == channel,
        ChannelListing.status == "draft",
        SupplierItem.is_oversold.is_(False),  # 超卖不排上架
    )
    if item_ids:
        q = q.filter(ChannelListing.item_id.in_(item_ids))

    drafts: list[XianyuListingDraft] = []
    for listing, item in q.all():
        try:
            draft = prepare_listing(item, db, channel=channel, listing=listing)
            drafts.append(draft)
        except Exception:  # noqa: BLE001
            logger.exception("prepare_pending_listings: failed item_id=%s", item.item_id)

    logger.info(
        "prepare_pending_listings channel=%s total=%s",
        channel,
        len(drafts),
    )
    return drafts


# =====================================================================
# 单个字段生成
# =====================================================================
def prepare_title(item: SupplierItem) -> str:
    """生成闲鱼端标题。

    策略：
    1. 从 1688 原始标题剥掉 货运词 / 多余空格 / emoji
    2. 压缩长度 ≤ 30 字符（闲鱼硬限制）
    3. 【可选】可被运营调度调整
    """
    title = (item.title or "").strip()

    # 1. 过滤 1688 货运词
    for bad in _XIANYU_BANNED_SUBSTRINGS:
        title = title.replace(bad, "")

    # 2. 压嘴多余字符
    title = re.sub(r"[\s\u3000]+", " ", title)
    title = re.sub(r"[【】\[\]()()【】（）()「」『』]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()

    # 3. 去口吐 (例：``【热售】` 之类的“”括号 加贴纸)
    title = title.strip(" ·、,，*【】「」-+=~！!?.？")

    # 4. 限长
    if len(title) > XIANYU_TITLE_MAX_LEN:
        title = title[:XIANYU_TITLE_MAX_LEN].rstrip()

    # 5. 【只剩标点】→ 给出默认 fallback
    if not title or len(title.strip()) < 2:
        title = f"新品上架{item.item_id[-6:]}"

    return title


def prepare_listing_price(
    item: SupplierItem,
    db: Session,
    *,
    channel: str = "xianyu",
    default_margin: float = 0.20,
) -> Decimal:
    """闲鱼展示价（主价格）。

    策略：
    - 有 PriceRule → 用 rule.margin_rate 加价
    - 没 PriceRule → 用 default_margin=0.20 兜底
    - 底价取 SKU 最低价（如有），否则 item.cost_price
    """
    rule = pick_rule(
        db, channel=channel, category_id=item.category_id, supplier_id=item.supplier_id
    )

    # 底价：SKU 最低价 / item.cost_price
    sku_payload = (item.sku_list or {}).get("skus") or []
    if sku_payload:
        sku_costs = [
            Decimal(str(s.get("price") or 0)) for s in sku_payload if s.get("price")
        ]
        base_cost = min(sku_costs) if sku_costs else item.cost_price
    else:
        base_cost = item.cost_price

    return _markup(base_cost, rule, default_margin=default_margin)


def prepare_skus(
    item: SupplierItem,
    db: Session,
    *,
    channel: str = "xianyu",
) -> list[XianyuSkuDraft]:
    """为闲鱼上架准备 SKU 列表（含 available_stock = stock - reserved）。

    策略：
    1. 从 item.sku_list['skus'] 拉 1688 原始 SKU
    2. 从 SkuMapping 拉 ali_sku_id → channel_sku_id + reserved_stock
    3. sale_price 按 PriceRule 加价（每个 SKU 自己的 cost 为底）
    4. available_stock = max(0, sku.stock - reserved_stock)
    5. 去除可用库存 ≤ 0 的 SKU （避免上架后不可售）
    """
    rule = pick_rule(
        db, channel=channel, category_id=item.category_id, supplier_id=item.supplier_id
    )

    sku_payload = (item.sku_list or {}).get("skus") or []
    mappings: dict[str, SkuMapping] = {
        m.ali_sku_id: m
        for m in db.query(SkuMapping)
        .filter(SkuMapping.item_id == item.item_id, SkuMapping.channel == channel)
        .all()
    }

    drafts: list[XianyuSkuDraft] = []
    for raw in sku_payload:
        sku_id = str(raw.get("sku_id") or "")
        if not sku_id:
            continue
        cost = Decimal(str(raw.get("price") or 0))
        stock = int(raw.get("stock") or 0)
        spec = str(raw.get("spec") or "").strip()

        mapping = mappings.get(sku_id)
        reserved = int(mapping.reserved_stock) if mapping else 0
        available = max(0, stock - reserved)

        if available <= 0:
            # 可售库存为 0 → 不上架这个 SKU
            continue

        sale_price = _markup(cost, rule)

        drafts.append(
            XianyuSkuDraft(
                ali_sku_id=sku_id,
                channel_sku_id=mapping.channel_sku_id if mapping else None,
                spec_text=spec or "默认规格",
                cost_price=cost,
                sale_price=sale_price,
                available_stock=available,
            )
        )

    return drafts


def _markup(
    cost: Decimal,
    rule,
    *,
    default_margin: float = 0.20,
) -> Decimal:
    """cost → 闲鱼售价（受 PriceRule 区间保护）。"""
    if rule is not None:
        price = (
            cost * (Decimal("1") + Decimal(str(rule.margin_rate))) + rule.fixed_shipping
        )
        if rule.price_min is not None:
            price = max(price, rule.price_min)
        if rule.price_max is not None:
            price = min(price, rule.price_max)
    else:
        price = cost * (Decimal("1") + Decimal(str(default_margin)))
    return price.quantize(Decimal("0.01"))


def prepare_total_stock(
    item: SupplierItem,
    db: Session,
    *,
    channel: str = "xianyu",
) -> int:
    """计算 available_total_stock。

    策略：
    - 如 item 有 SKU 列表 → 用每 SKU available = max(0, stock - reserved) 求和
    - 否则 → item.stock - 该商品所有 SkuMapping.reserved_stock 之和
    """
    sku_payload = (item.sku_list or {}).get("skus") or []
    if sku_payload:
        mappings: dict[str, SkuMapping] = {
            m.ali_sku_id: m
            for m in db.query(SkuMapping)
            .filter(SkuMapping.item_id == item.item_id, SkuMapping.channel == channel)
            .all()
        }
        total = 0
        for raw in sku_payload:
            sku_id = str(raw.get("sku_id") or "")
            if not sku_id:
                continue
            stock = int(raw.get("stock") or 0)
            reserved = int(mappings[sku_id].reserved_stock) if sku_id in mappings else 0
            total += max(0, stock - reserved)
        return total
    # fallback：只有 item.stock 时，扣所有 reserved
    reserved = _sum_reserved_stock(db, item.item_id, channel=channel)
    return max(0, int(item.stock or 0) - reserved)


def prepare_main_image(item: SupplierItem) -> str | None:
    """主图 URL：取 main_image，验证是合法 URL。"""
    url = (item.main_image or "").strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        return None
    return url


def prepare_skus(
    item: SupplierItem,
    db: Session,
    *,
    channel: str = "xianyu",
) -> list[XianyuSkuDraft]:
    """为闲鱼上架准备 SKU 列表（含 available_stock = stock - reserved）。

    策略：
    1. 从 item.sku_list['skus'] 拉 1688 原始 SKU
    2. 从 SkuMapping 拉 ali_sku_id → channel_sku_id + reserved_stock
    3. sale_price 按 PriceRule 加价
    4. available_stock = max(0, sku.stock - reserved_stock)
    5. 去除可用库存 ≤ 0 的 SKU （避免上架后不可售）
    """
    rule = pick_rule(
        db, channel=channel, category_id=item.category_id, supplier_id=item.supplier_id
    )

    sku_payload = (item.sku_list or {}).get("skus") or []
    mappings: dict[str, SkuMapping] = {
        m.ali_sku_id: m
        for m in db.query(SkuMapping)
        .filter(SkuMapping.item_id == item.item_id, SkuMapping.channel == channel)
        .all()
    }

    drafts: list[XianyuSkuDraft] = []
    for raw in sku_payload:
        sku_id = str(raw.get("sku_id") or "")
        if not sku_id:
            continue
        cost = Decimal(str(raw.get("price") or 0))
        stock = int(raw.get("stock") or 0)
        spec = str(raw.get("spec") or "").strip()

        mapping = mappings.get(sku_id)
        reserved = int(mapping.reserved_stock) if mapping else 0
        available = max(0, stock - reserved)

        if available <= 0:
            # 可售库存为 0 → 不上架这个 SKU
            continue

        sale_price = calculate_price(
            item,
            rule=rule,
            margin_rate=None if rule is not None else 0.20,
            fixed_shipping=rule.fixed_shipping if rule else Decimal("0"),
        )
        # “_顶起”。如果使用 base 是 item.cost_price，这里的 sale_price 会是 item 层面价
        # 为避免每个 SKU 相同价，改为 用 SKU 自己的 cost 重算：
        if rule is not None:
            sale_price = (
                (cost * (Decimal("1") + Decimal(str(rule.margin_rate))) + rule.fixed_shipping)
                .quantize(Decimal("0.01"))
            )
        else:
            sale_price = (
                (cost * Decimal("1.20"))
                .quantize(Decimal("0.01"))
            )

        drafts.append(
            XianyuSkuDraft(
                ali_sku_id=sku_id,
                channel_sku_id=mapping.channel_sku_id if mapping else None,
                spec_text=spec or "默认规格",
                cost_price=cost,
                sale_price=sale_price,
                available_stock=available,
            )
        )

    return drafts


# =====================================================================
# 导出（CSV / JSON）
# =====================================================================
_XIANYU_CSV_HEADER = [
    "item_id",
    "title",
    "suggested_price",
    "total_available_stock",
    "main_image",
    "image_count",
    "sku_count",
    "sku_summary",
    "tags",
    "description_hint",
    "category_id",
    "supplier_id",
    "prepared_at",
]


def export_csv(drafts: list[XianyuListingDraft], file_path: str | Path) -> Path:
    """导出人工上架 CSV。

    CSV 列为“人偶表"模板：item_id / 售价 / 总可用库存 / SKU 摘要 / 文案 / 标签。
    使用 utf-8-sig 让 Excel 直接打开不乱码。
    """
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=_XIANYU_CSV_HEADER)
        writer.writeheader()
        for d in drafts:
            writer.writerow(d.to_csv_row())
    logger.info("export_csv drafts=%d path=%s", len(drafts), path)
    return path


def export_json(drafts: list[XianyuListingDraft], file_path: str | Path) -> Path:
    """导出人工上架 JSON（包含完整 SKU 详情）。"""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "channel": "xianyu",
        "generated_at": to_iso(utc_now()),
        "drafts": [d.to_json_dict() for d in drafts],
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info("export_json drafts=%d path=%s", len(drafts), path)
    return path


# =====================================================================
# 工具函数
# =====================================================================
def _category_label(item: SupplierItem) -> str:
    """类目的**中文路径**（预填给闲鱼时用），认不到就空串 —— 别把类目 id 往外给。

    2026-10-16：以前直接写 ``item.category_id``，页面上就成了「类目：1688-653」
    这种机器码，买家看不懂、运营也没法核对。
    """
    try:
        from src.goods.catalog import category_path

        paths = category_path()
        if item.category_id and item.category_id in paths:
            return str(paths[item.category_id] or "")
    except Exception:  # noqa: BLE001 - 类目表缺失不影响预填包
        pass
    return ""


def _build_description_hint(item: SupplierItem) -> str:
    """生成推荐描述文案（人工可参考修改）。

    2026-10-16 重写：这段文案是要**贴进闲鱼商品描述**的，以前写的
    「【工厂一手货源】类目：1688-653。进价 ¥45.50。建议描述点：…」两句都留不得 ——
    类目 id 买家看不懂，**进货价是底价更不能往外说**，「建议描述点」是给运营看的
    内部提示，也不该出现在商品描述里。现在只留一句人读得懂、又不漏底价的话。
    """
    cat = _category_label(item)
    return f"【{cat or '精选好货'} · 一手货源】全新正品，现货速发。"


def _count_images(item: SupplierItem) -> int:
    """统计可用图片数：从 raw_json.images / sku_list 推导。"""
    raw = item.raw_json or {}
    images = raw.get("images") or raw.get("imageUrls") or []
    if isinstance(images, list):
        return len([u for u in images if u])
    if isinstance(images, str):
        return 1 if images else 0
    # fallback：main_image = 1
    return 1 if (item.main_image or "").strip() else 0


def _sum_reserved_stock(
    db: Session, item_id: str, *, channel: str
) -> int:
    """累计该商品在 channel 下被预占的库存。"""
    total = 0
    for m in (
        db.query(SkuMapping)
        .filter(SkuMapping.item_id == item_id, SkuMapping.channel == channel)
        .all()
    ):
        total += int(m.reserved_stock or 0)
    return total


def _summarize_skus(skus: list[XianyuSkuDraft]) -> str:
    """压缩 SKU 为可读摘要：`规格@¥价:库存 | ...`"""
    if not skus:
        return ""
    return " | ".join(
        f"{s.spec_text}@¥{s.sale_price}:{s.available_stock}" for s in skus
    )


__all__ = [
    # dataclass
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
]