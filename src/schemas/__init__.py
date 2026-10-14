"""Pydantic v2 请求/响应模型。

约定：
- 入参 = *In / *Request
- 出参 = *Out / *Response
- 数据库模型不直接对外暴露，必须通过 *Out 包装
"""
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ====== 通用 ======
class HealthOut(BaseModel):
    status: str
    version: str
    env: str


# ====== Goods ======
class SupplierItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    item_id: str
    title: str
    category_id: str | None
    cost_price: Decimal
    stock: int
    last_synced_at: datetime | None
    is_oversold: bool = False
    prev_stock: int | None = None
    last_stock_change_at: datetime | None = None

    # ---- 供应链来源 / 审核 ----
    supplier_id: str | None = None
    source_type: str = "third_party"
    audit_status: str = "pending"
    audit_note: str | None = None

    # ---- 上架信息 ----
    item_code: str | None = None
    # 品牌：商品属性，可留空（型号 / 成色 / 重量属于规格维度，在 spec_defs 里）
    brand: str | None = None
    # 供应商友情提示：与渔枢系统商品详情同一口径
    supplier_tip: str | None = None
    # 商品标签（一行一个）：渔枢系统发布时勾选的固定标签，上架时同步过来
    tags: str | None = None
    # 发货方式 / 默认运费 / 最近上架：与渔枢系统商品详情同一口径
    shipping_mode: str | None = None
    shipping_fee: Decimal = Decimal("0")
    listed_at: datetime | None = None
    selling_points: str | None = None
    main_image: str | None = None
    sale_price: Decimal | None = None
    market_price: Decimal | None = None
    # 渔货价：渔货栈对外的成本价 / 进货价
    fish_price: Decimal | None = None
    images: list[str] = Field(default_factory=list)
    detail_images: list[str] = Field(default_factory=list)
    spec_defs: list[dict[str, Any]] = Field(default_factory=list)
    sku_list: dict[str, Any] = Field(default_factory=dict)
    freight_template: str | None = None
    delivery_days: int = 2
    after_sale: str = "7天无理由退换"


class GoodsItemSkuIn(BaseModel):
    """上架表单里的一个 SKU 行。"""

    sku_id: str | None = None
    spec_text: str = ""
    price: Decimal = Field(default=Decimal("0"), ge=0)
    stock: int = Field(default=0, ge=0)
    code: str | None = None


class GoodsItemCreateIn(BaseModel):
    """自营商品上架入参（对应 Web「发布商品」表单）。"""

    title: str = Field(min_length=2, max_length=120)
    category_id: str | None = None
    # 目前 Web 只开放 self_operated；保留 third_party 便于后续代录入货
    source_type: Literal["self_operated", "third_party"] = "self_operated"

    item_code: str | None = None
    # 品牌：商品属性，可留空（型号 / 成色 / 重量属于规格维度，走 spec_defs）
    brand: str | None = Field(default=None, max_length=64)
    selling_points: str | None = Field(default=None, max_length=200)
    # 商品标签（一行一个）：固定标签，手工维护（源头工厂 / 现货速发 …）
    tags: str | None = None

    main_image: str | None = None
    images: list[str] = Field(default_factory=list)
    detail_images: list[str] = Field(default_factory=list)

    cost_price: Decimal = Field(default=Decimal("0"), ge=0)
    sale_price: Decimal | None = Field(default=None, ge=0)
    market_price: Decimal | None = Field(default=None, ge=0)
    # 渔货价：渔货栈对外的成本价 / 进货价（下游拿货价，与 1688 成本价分开记）
    fish_price: Decimal | None = Field(default=None, ge=0)
    stock: int = Field(default=0, ge=0)

    spec_defs: list[dict[str, Any]] = Field(default_factory=list)
    skus: list[GoodsItemSkuIn] = Field(default_factory=list)

    freight_template: str | None = None
    # 闲鱼发货方式：free / distance / fixed / self_pickup（按规则自动套用）
    shipping_mode: str | None = None
    delivery_days: int = Field(default=2, ge=1, le=30)
    after_sale: str = "7天无理由退换"
    detail_html: str | None = None
    # 供应商友情提示（渔货栈特色信息，给自营团队选品 / 经营做参考）
    supplier_tip: str | None = None
    # 默认运费（包邮为 0） / 最近上架（新建页不填，由上架动作自动记）
    shipping_fee: Decimal = Field(default=Decimal("0"), ge=0)
    listed_at: datetime | None = None

    # 提交动作：draft=存草稿 / audit=提交审核 / publish=直接上架
    submit_action: Literal["draft", "audit", "publish"] = "publish"


class GoodsItemAuditIn(BaseModel):
    """商品审核（通过 / 驳回）。"""

    action: Literal["approve", "reject"]
    note: str | None = None
    by: str = "human"


class GoodsItemBatchAuditIn(BaseModel):
    """批量审核。"""

    item_ids: list[str] = Field(min_length=1)
    action: Literal["approve", "reject"]
    note: str | None = None
    by: str = "human"


class GoodsAuditResultOut(BaseModel):
    """审核结果汇总。"""

    total: int
    succeeded: int
    failed: int
    failed_item_ids: list[str] = Field(default_factory=list)


# ====== Selection ======
class SelectionRunRequest(BaseModel):
    item_ids: list[str] | None = None  # None = 全量
    shadow_mode: bool = False  # 影子模式：只记录不实际铺货


class SelectionRunResponse(BaseModel):
    total: int
    passed: int
    rejected: int
    rejected_reasons: dict[str, int]


# ====== Order ======
class OrderItemIn(BaseModel):
    channel_sku_id: str
    qty: int = Field(ge=1)


class OrderCreateIn(BaseModel):
    downstream_order_id: str
    channel: str = "xianyu"
    buyer_name: str | None = None
    buyer_phone: str | None = None
    address: str | None = None
    items: list[OrderItemIn]
    raw_json: dict[str, Any] = Field(default_factory=dict)


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    downstream_order_id: str
    channel: str
    status: str
    sale_price: Decimal
    qty: int
    channel_sku_id: str
    buyer_name: str | None = None
    buyer_phone: str | None = None


class OrderDetailOut(BaseModel):
    """一笔订单的完整快照：DSO + PO + ExceptionLog 计数。"""

    group_downstream_order_id: str
    channel: str
    downstream_orders: list[OrderOut]
    purchase_orders: list["PurchaseOrderOut"] = Field(default_factory=list)
    exception_count: int = 0


# ====== Purchase ======
class PurchasePushRequest(BaseModel):
    downstream_order_id: str


class PurchasePushResponse(BaseModel):
    ali_order_id: str | None
    status: str
    message: str | None = None
    purchase_orders: list["PurchaseOrderOut"] = Field(default_factory=list)


class PurchaseOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    downstream_order_id: str
    ali_item_id: str
    ali_sku_id: str
    qty: int
    cost_price: Decimal
    ali_order_id: str | None = None
    status: str
    push_log: dict[str, Any] = Field(default_factory=dict)


class PurchaseConfirmPaidIn(BaseModel):
    """人工付款 / 标记失败的入参。"""

    by: str = "human"
    note: str | None = None


# ====== Logistics ======
class LogisticsCallbackIn(BaseModel):
    """物流回写 webhook payload（人工推送 tracking 号的接入点）。"""

    downstream_order_id: str | None = None
    ali_order_id: str | None = None
    shipping_no: str
    carrier: str | None = None
    status: str = "shipped"
    source: str | None = "manual"
    extra: dict[str, Any] = Field(default_factory=dict)


class LogisticsCallbackOut(BaseModel):
    shipping_no: str
    carrier: str = ""
    status: str
    received_at: datetime


OrderDetailOut.model_rebuild()


# ====== Channel Listing ======
class ChannelListingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    item_id: str
    channel: str
    status: str
    suggested_price: Decimal | None
    suggested_title: str | None
    reject_reasons: list[str]


# ====== Xianyu 草稿（半自动曝光）======
class XianyuSkuOut(BaseModel):
    """闲鱼上草稿 SKU 输出（人工上架的“开云环” API）。"""

    ali_sku_id: str
    channel_sku_id: str | None = None
    spec_text: str
    cost_price: Decimal
    sale_price: Decimal
    available_stock: int


class XianyuListingOut(BaseModel):
    """一个闲鱼上商品草稿输出（CSV / JSON 一行）。"""

    item_id: str
    title: str
    suggested_price: Decimal
    total_available_stock: int
    main_image: str | None = None
    image_count: int
    sku_count: int
    tags: list[str] = Field(default_factory=list)
    description_hint: str = ""
    category_id: str | None = None
    supplier_id: str | None = None
    prepared_at: datetime


class XianyuPrepareOut(BaseModel):
    """跑一次批量生成草稿的响应。"""

    total: int
    drafts: list[XianyuListingOut]
    # CSV / JSON 路径返回（如果生成可访问的路径，服务端选写）
    csv_path: str | None = None
    json_path: str | None = None


# ====== Exception ======
class ExceptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    error: str | None
    retry_count: int
    resolved: bool