"""闲鱼「商品发布页」预填包 + 闲参谋（SaaS）带入通道。

背景（来自业务约定）：
- 闲鱼**始终没有**开放自动发布 API → 发布动作必须由人在闲鱼客户端 / 网页完成；
- 因此本平台不做「自动发布」，只做「**预填**」：把上架所需的全部字段
  （标题 / 描述 / 价格 / 库存 / 图片 / 规格 SKU / 标签）整理成一个
  `XianyuPublishPackage`，供人工或闲参谋直接搬进闲鱼发布页；
- 「把字段带入到闲鱼商品发布页」这件事由 **SaaS 版闲参谋** 的前端完成：
  闲参谋用一次性票据 `ticket` 向本平台换取预填包（`XianyuPublishPackage`），
  再填入闲鱼发布表单。本模块只负责**生成**预填包与票据，不碰账号密码、
  不调闲鱼任何接口（与 `prepare.py` 的约束一致）。

链路：
    渔货栈（本平台）                       闲参谋 SaaS              闲鱼
    ─────────────────────────────────────────────────────────────────────
    build_publish_package()  →  预填包 JSON
    issue_handoff()          →  票据 + 跳转 URL  →  打开闲鱼发布页（人工确认）
    verify_ticket()          ←  闲参谋携票回调取包

闲参谋侧对接（SaaS 前端）：
    1. 收到 `POST /api/v1/xianyu/handoff/{item_id}` 返回的 `publish_url`（含 ticket）
    2. 用 `GET /api/v1/xianyu/handoff/resolve?ticket=…` 换取 `XianyuPublishPackage`
    3. 把 package 字段写入闲鱼商品发布页表单（标题 / 描述 / 价格 / 库存 / 图片 / 规格）
    4. 人工确认后发布，回调 `POST /api/v1/xianyu/{listing_id}/mark-published` 回写状态

票据实现：HMAC-SHA256（app_secret_key 派生）+ 短时效 + 携带 item_id。
生产环境建议叠加 Redis 一次性 nonce 消费（见 `TicketStore` 占位）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.timeutil import UTC, to_iso, utc_now
from src.config import get_settings
from src.models import SupplierItem
from src.channel_xianyu.prepare import (
    XIANYU_TITLE_MAX_LEN,
    prepare_listing,
)

logger = logging.getLogger(__name__)

HANDOFF_CHANNEL = "xianyu"
TICKET_SALT = "xiaoyu.xianmou.xianyu.publish.v1"


# =====================================================================
# 数据结构
# =====================================================================
@dataclass
class XianyuPublishSku:
    """闲鱼发布页「规格」行：规格名 + 价格 + 库存。"""

    spec_name: str
    price: Decimal
    stock: int
    code: str | None = None


@dataclass
class XianyuPublishPackage:
    """闲鱼商品发布页的**完整预填字段**（字段名对齐闲鱼发布表单）。"""

    item_id: str
    title: str                      # ≤ 30 字符（闲鱼硬限制）
    title_length: int
    description: str                # 商品描述（已过滤货盘词）
    price: Decimal                  # 发布价
    original_price: Decimal | None  # 划线价（原价）
    stock: int                      # 可售总库存（已扣预占）
    images: list[str] = field(default_factory=list)
    main_image: str | None = None
    skus: list[XianyuPublishSku] = field(default_factory=list)
    spec_defs: list[dict] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    category_text: str = ""         # 建议挂载的闲鱼类目（人工可改）
    condition: str = "全新"
    delivery: str = ""              # 发货时效承诺
    after_sale: str = ""            # 售后承诺
    freight: str = ""               # 运费模板文案
    selling_points: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=lambda: utc_now())

    def to_dict(self) -> dict[str, Any]:
        """JSON 友好（Decimal → str，datetime → ISO）。"""
        data = asdict(self)
        data["price"] = str(data["price"])
        data["original_price"] = (
            None if data["original_price"] is None else str(data["original_price"])
        )
        for sku in data["skus"]:
            sku["price"] = str(sku["price"])
        data["generated_at"] = self.generated_at.isoformat()
        return data


# =====================================================================
# 预填包生成
# =====================================================================
def build_publish_package(
    item: SupplierItem,
    db: Session,
    *,
    channel: str = HANDOFF_CHANNEL,
) -> XianyuPublishPackage:
    """把 SupplierItem 整理成闲鱼发布页可直接照抄的字段集合。

    与 `prepare.prepare_listing` 的差别：
    - `prepare_*` 面向「选品 / 铺货草稿」（渠道内部结构）
    - 本函数面向「发布表单」（人 / 闲参谋要填的字段），补齐描述、划线价、
      规格 SKU、发货与售后承诺等表单字段。
    """
    draft = prepare_listing(item, db, channel=channel)

    images = _collect_images(item)
    cover = draft.main_image or (images[0] if images else None)
    specs = [dict(s) for s in (item.spec_defs or []) if isinstance(s, dict)]
    skus = [
        XianyuPublishSku(
            spec_name=sku.spec_text,
            price=sku.sale_price,
            stock=sku.available_stock,
        )
        for sku in draft.skus
    ]
    original_price = item.market_price
    if original_price is not None and Decimal(str(original_price)) <= draft.suggested_price:
        # 划线价必须高于发布价，否则不带划线价
        original_price = None

    package = XianyuPublishPackage(
        item_id=item.item_id,
        title=draft.title,
        title_length=len(draft.title),
        description=_build_description(item, draft.description_hint),
        price=draft.suggested_price,
        original_price=original_price,
        stock=draft.total_available_stock,
        images=images,
        main_image=cover,
        skus=skus,
        spec_defs=specs,
        tags=list(draft.tags),
        category_text=_category_text(item),
        condition="全新",
        delivery=f"{int(item.delivery_days or 2)} 天内发货",
        after_sale=item.after_sale or "7天无理由退换",
        freight=item.freight_template or "全国包邮",
        selling_points=(item.selling_points or "").strip(),
        attributes={
            "商家编码": item.item_code or item.item_id,
            "规格数量": str(len(skus)),
        },
        source={
            "supplier_id": item.supplier_id,
            "source_type": item.source_type,
            "category_id": item.category_id,
            "audit_status": item.audit_status,
        },
    )
    logger.info(
        "build_publish_package item_id=%s title_len=%d skus=%d images=%d",
        item.item_id, package.title_length, len(skus), len(images),
    )
    return package


def _collect_images(item: SupplierItem) -> list[str]:
    """主图 + 图组 + 详情图，去重保序。

    以前只收 http(s) 直链，渔枢系统上架的商品图存的是 /media/… 站内路径，
    全被滤掉 → 铺货预填里「图片」一直是 0 张。现在与详情页 `_goods_images`
    同一口径：任何非空地址都收，展示与实际关联图数一致。
    """
    candidates: list[str] = []
    if item.main_image:
        candidates.append(item.main_image)
    for group in (item.images, item.detail_images):
        if isinstance(group, list):
            candidates.extend(str(u) for u in group if u)
    seen: set[str] = set()
    out: list[str] = []
    for url in candidates:
        url = (url or "").strip()
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _build_description(item: SupplierItem, hint: str) -> str:
    """发布描述：卖点 → 参数 → 发货 / 售后承诺（人工可再改）。"""
    lines: list[str] = []
    if item.selling_points:
        lines.append(item.selling_points.strip())
    lines.append(f"{item.title}（全新正品）")
    if item.spec_defs:
        for spec in item.spec_defs:
            if not isinstance(spec, dict):
                continue
            name, values = spec.get("name"), spec.get("values")
            if name and values:
                lines.append(f"{name}：{' / '.join(str(v) for v in values)}")
    lines.append(f"发货时效：{int(item.delivery_days or 2)} 天内发出")
    lines.append(f"售后：{item.after_sale or '7天无理由退换'}")
    lines.append(hint)
    return "\n".join(line for line in lines if line).strip()


def _category_text(item: SupplierItem) -> str:
    """建议挂载的闲鱼类目：优先用本站前台类目路径，否则回落 1688 类目 id。"""
    try:
        from src.goods.catalog import category_path

        paths = category_path()
        if item.category_id and item.category_id in paths:
            return paths[item.category_id]
    except Exception:  # noqa: BLE001 - 类目表缺失不影响预填包
        pass
    return item.category_id or "其他"


# =====================================================================
# 闲参谋（SaaS）带入通道：一次性票据 + 跳转 URL
# =====================================================================
@dataclass
class XianyuHandoff:
    """一次「上架到店铺 → 带入闲鱼发布页」的交接凭据。"""

    item_id: str
    ticket: str
    expires_at: datetime
    ttl: int
    publish_url: str          # 闲参谋（或闲鱼）发布页入口
    package: XianyuPublishPackage

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "ticket": self.ticket,
            "expires_at": self.expires_at.isoformat(),
            "ttl": self.ttl,
            "publish_url": self.publish_url,
            "package": self.package.to_dict(),
        }


def _ticket_secret() -> bytes:
    settings = get_settings()
    return hashlib.sha256(
        f"{settings.app_secret_key}:{TICKET_SALT}".encode("utf-8")
    ).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue_ticket(item_id: str, *, user: str = "", ttl: int | None = None) -> tuple[str, datetime]:
    """签发一次性票据（HMAC 签名 + 过期时间），返回 (ticket, 过期时间)。"""
    settings = get_settings()
    ttl = int(ttl or settings.xianmou_ticket_ttl)
    exp = int(utc_now().replace(tzinfo=UTC).timestamp()) + ttl
    payload = {
        "i": item_id,
        "u": user or "",
        "e": exp,
        "n": secrets.token_urlsafe(8),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    sig = _b64e(hmac.new(_ticket_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}", datetime.fromtimestamp(exp, tz=timezone.utc)


def verify_ticket(ticket: str) -> dict[str, Any] | None:
    """校验票据签名与时效；通过返回 payload，否则 None。

    注意：本实现是**无状态**验签（签名 + 时效）。生产环境若要求「绝对一次性」，
    需在 Redis 中消费 nonce（见 `TicketStore`）。
    """
    if not ticket or "." not in ticket:
        return None
    body, _, sig = ticket.rpartition(".")
    expect = _b64e(hmac.new(_ticket_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if int(payload.get("e") or 0) < int(utc_now().replace(tzinfo=UTC).timestamp()):
        return None
    return payload


class TicketStore:
    """一次性票据消费的占位实现（生产接 Redis：`SETNX ticket 1 EX ttl`）。"""

    def consume(self, ticket: str) -> bool:  # pragma: no cover - 占位
        raise NotImplementedError("TODO: 接入 Redis 实现票据一次性消费")


def xianmou_publish_url() -> str:
    """闲参谋商品发布页入口（未配置返回空串 → 前端退化为复制 / 下载预填包）。"""
    return (get_settings().xianmou_publish_url or "").strip()


def xianyu_publish_url() -> str:
    """闲鱼发布页兜底入口（配置项为空时按 xianyu_api_base 推导）。"""
    base = (get_settings().xianyu_api_base or "https://www.goofish.com").rstrip("/")
    return f"{base}/publish"


def build_publish_url(item_id: str, ticket: str) -> str:
    """拼「带入闲鱼发布页」的跳转 URL。

    - 配置了 `XIANMOU_PUBLISH_URL` → 跳闲参谋发布页（由闲参谋把字段写进闲鱼表单）
    - 未配置 → 跳闲鱼发布页，仅带 item 标识（人工按预填包填写）
    """
    base = xianmou_publish_url()
    if base:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}item_id={item_id}&ticket={ticket}&channel={HANDOFF_CHANNEL}"
    return f"{xianyu_publish_url()}?ref=xiaoyu&item_id={item_id}"


def issue_handoff(
    item: SupplierItem,
    db: Session,
    *,
    channel: str = HANDOFF_CHANNEL,
    user: str = "",
) -> XianyuHandoff:
    """生成预填包 + 票据 + 跳转 URL（Web 详情页 / 货源广场「上架到店铺」共用）。"""
    package = build_publish_package(item, db, channel=channel)
    ticket, exp = issue_ticket(item.item_id, user=user)
    settings = get_settings()
    return XianyuHandoff(
        item_id=item.item_id,
        ticket=ticket,
        expires_at=exp,
        ttl=int(settings.xianmou_ticket_ttl),
        publish_url=build_publish_url(item.item_id, ticket),
        package=package,
    )


__all__ = [
    "HANDOFF_CHANNEL",
    "XIANYU_TITLE_MAX_LEN",
    "XianyuPublishSku",
    "XianyuPublishPackage",
    "XianyuHandoff",
    "TicketStore",
    "build_publish_package",
    "issue_ticket",
    "verify_ticket",
    "issue_handoff",
    "build_publish_url",
    "xianmou_publish_url",
    "xianyu_publish_url",
]
