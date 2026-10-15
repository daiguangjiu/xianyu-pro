"""SQLAlchemy ORM 模型集合。

所有表通过 `Base` 注册；alembic 依赖 `Base.metadata` 进行 autogenerate。
新增模型：在本目录新增文件并在下方 import 一次。
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

# 渔枢系统的 declarative base：两端一套库，渔货栈的表也注册在这份 metadata 上
from app.db import Base as HubBase
from src.security import EncryptedText


class Base(HubBase):
    """渔货栈模型的公共基类。

    两端合并成一套库之后（2026-09-30），渔货栈与渔枢系统共用**同一个
    declarative base** —— 表都在同一份 metadata 里：一次 create_all 全建好，
    跨端的外键（如 merchants.owner_user_id → users.id）也能正常建出来。
    `__abstract__` 表示这只是个混入公共列的模板，本身不对应任何表。
    """

    __abstract__ = True

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ====== 1. Shop：店铺授权信息 ======
class Shop(Base):
    # 两端合并成一套库后（2026-09-30）：渔枢系统的 shops 表是「店铺档案」
    # （闲鱼昵称 / 运营人 / 状态），这张表是渔货栈的「闲鱼授权凭证」（access_token 等），
    # 语义与字段都不一样，硬并会两边都别扭 —— 改名后同库共存，不撞名。
    __tablename__ = "shop_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    shop_name: Mapped[str | None] = mapped_column(String(128))
    channel: Mapped[str] = mapped_column(String(32), default="xianyu")

    # ⚠️ Token 字段加密落库（Fernet）。业务层仍按 str 读写。
    # 列底层是 LargeBinary；LIKE/ORDER BY 不能用。详见 src/security/crypto.py。
    access_token: Mapped[str | None] = mapped_column(EncryptedText())
    refresh_token: Mapped[str | None] = mapped_column(EncryptedText())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# ====== 2. SupplierItem：货源商品快照 ======
class SupplierItem(Base):
    __tablename__ = "supplier_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    supplier_id: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(512))
    category_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # 三级类目名：渔枢系统「上架到渔货栈」时按商品类目 + 标题联想认出来的。
    # 货源广场按三级筛货先认这一列（认不到才回落「标题里含三级名」），
    # 这样标题里没写三级名的货也能被三级筛出来。
    category_l3: Mapped[str | None] = mapped_column(String(128))
    main_image: Mapped[str | None] = mapped_column(String(512))

    cost_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    stock: Mapped[int] = mapped_column(Integer, default=0)
    return_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=0)  # 近 30 天退货率

    # SKU 列表（含价格、库存、规格），原始 JSON
    sku_list: Mapped[dict] = mapped_column(JSON, default=dict)

    # 完整原始数据，便于回溯
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict)

    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---- 快照 / 变更对比（goods/sync.py 写入）----
    # 上次同步前的 stock / cost_price（用于变更检测）
    prev_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prev_cost_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # 库存最近一次"显著变化"的时间
    last_stock_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 超卖标记：True 时 purchase 模块拒绝推单
    is_oversold: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # 上次超卖时间，便于排查
    oversold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---- 供应链来源 / 审核（goods 模块写入）----
    # self_operated = 自营供应链（本站录入）
    # third_party   = 供应商供应链（1688 同步 / 外部档口）
    source_type: Mapped[str] = mapped_column(
        String(16), default="third_party", index=True
    )
    # pending / approved / rejected
    audit_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    audit_note: Mapped[str | None] = mapped_column(Text)
    audited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---- 上下架（与 audit_status 独立：审核通过后方可上架，商家可自主下架）----
    is_listed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # ---- 平台下架（渔枢系统把商品从渔货栈下架时写入）----
    # 渔货栈在货源广场 / 商品详情页据此给提示：
    # 「渔枢系统已下架 · 时间 · 原因」，并把下架前那份资料快照照「上架预览」的样子展示。
    unlisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unlisted_reason: Mapped[str | None] = mapped_column(String(128))
    unlisted_by: Mapped[str | None] = mapped_column(String(64))
    # 下架前快照：标题 / 主图 / 渔货价 / 建议售价 / 类目 / 商家编码 / 品牌 / 上架时间
    unlisted_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)

    # ---- 归属商家（入驻商家发布的商品；1688 同步货源为 NULL）----
    merchant_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("merchants.id"), index=True
    )

    # ---- 上架信息（自营录入；供应商商品由 1688 同步回填，可能为空）----
    item_code: Mapped[str | None] = mapped_column(String(64))  # 商家编码
    # 品牌：商品属性，可留空（型号 / 成色 / 重量属于规格维度，走 spec_defs）
    brand: Mapped[str | None] = mapped_column(String(64))
    # 型号 / 规格（2026-10-15 用户拍板）：由渔集建档的商品档案字段同步过来
    # （渔集里这两项是「一款货」的公共资料，不是规格维度），详情页「规格参数」里看
    model: Mapped[str | None] = mapped_column(String(128))
    spec: Mapped[str | None] = mapped_column(String(128))
    selling_points: Mapped[str | None] = mapped_column(String(512))  # 卖点
    # 自营定价：sale_price 售价 / market_price 划线价
    sale_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    market_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # 渔货价：渔货栈对外的成本价 / 进货价（下游向渔货栈拿货的价格；未填表示还没定价）
    fish_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # 主图组（第一张为封面）与详情图组
    images: Mapped[list] = mapped_column(JSON, default=list)
    detail_images: Mapped[list] = mapped_column(JSON, default=list)
    # 规格定义 [{"name": "颜色", "values": ["黑", "白"]}]
    spec_defs: Mapped[list] = mapped_column(JSON, default=list)
    freight_template: Mapped[str | None] = mapped_column(String(64))
    # 闲鱼发货方式：free 包邮 / distance 按距离付费 / fixed 固定收费 / self_pickup 买家自提
    # （与渔枢系统同一口径，套用规则见「货源 · 运费模板」）
    shipping_mode: Mapped[str | None] = mapped_column(String(32))
    delivery_days: Mapped[int] = mapped_column(Integer, default=2)  # 承诺发货时效（天）
    after_sale: Mapped[str] = mapped_column(String(64), default="7天无理由退换")
    detail_html: Mapped[str | None] = mapped_column(Text)  # 图文详情
    # 供应商友情提示：渔货栈特色信息（详情页在图文详情下面展示），给自营团队选品 / 经营做参考
    supplier_tip: Mapped[str | None] = mapped_column(Text)
    # 商品标签（一行一个）：渔枢系统发布商品时勾选的固定标签（源头工厂 / 现货速发 …），
    # 上架到渔货栈时同步过来，货源广场卡片与商品详情页展示。
    # 注意：动态标签（今日上新 / 直降 x 元 / 高毛利 x%）由本端按数据实时算，不落这一列。
    tags: Mapped[str | None] = mapped_column(Text)
    # 默认运费：按件运费 / 偏远加收时才填；包邮为 0（与渔枢系统同一口径）
    shipping_fee: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    # 最近上架：上架动作自动记（新建页不再手填，详情页只读展示）
    listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ====== 3. SkuMapping：1688 SKU 与下游 SKU 映射 ======
class SkuMapping(Base):
    __tablename__ = "sku_mappings"
    __table_args__ = (UniqueConstraint("ali_sku_id", "channel", name="uq_ali_channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[str] = mapped_column(String(64), ForeignKey("supplier_items.item_id"), index=True)
    ali_sku_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="xianyu")
    channel_sku_id: Mapped[str | None] = mapped_column(String(128), index=True)

    # 预占库存：防止超卖
    reserved_stock: Mapped[int] = mapped_column(Integer, default=0)


# ====== 4. PriceRule：加价规则 ======
class PriceRule(Base):
    __tablename__ = "price_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 作用范围：global / channel / category / supplier
    scope: Mapped[str] = mapped_column(String(16), default="global", index=True)
    scope_key: Mapped[str | None] = mapped_column(String(128), index=True)

    margin_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=Decimal("0.20"))
    fixed_shipping: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    price_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    priority: Mapped[int] = mapped_column(Integer, default=100)  # 数字越小优先级越高
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# ====== 5. ChannelListing：铺货草稿 / 记录 ======
class ChannelListing(Base):
    __tablename__ = "channel_listings"
    # 2026-10-13 起铺货状态按账号隔离：同一款货每个账号各一条（uq 带上 owner）。
    # owner 为空串 = 平台级记录（选品引擎批量跑出来的），不算任何人的铺货状态。
    __table_args__ = (
        UniqueConstraint("item_id", "channel", "owner", name="uq_item_channel_owner"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="xianyu")
    # 归属账号（_purchase_owner 的 key，形如 u1）；空串 = 平台级
    owner: Mapped[str] = mapped_column(String(64), default="", index=True)

    # draft / published / failed / needs_review
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)

    suggested_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    suggested_title: Mapped[str | None] = mapped_column(String(512))
    reject_reasons: Mapped[list] = mapped_column(JSON, default=list)

    channel_listing_id: Mapped[str | None] = mapped_column(String(128))
    last_published_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # 记录是怎么来的：handoff=详情页「上架到店铺」带入闲鱼发布页 / draft=「仅生成草稿」
    # / selection=货源广场选品批量跑（老数据默认 selection）
    source: Mapped[str] = mapped_column(String(16), default="selection")
    # 最近一次「上架到店铺 → 带入闲鱼发布页」的时间（只记事实，不改状态）
    handoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ====== 5.1 PurchaseCartItem：采购车（详情页「加采购车」）======
class PurchaseCartItem(Base):
    """采购车里的一行：账号 + 货源 + 规格 + 数量（1688 采购车的自营版）。

    渔货栈是向渔货栈平台采购的一方，所以「加采购车」= 把要买的货放进采购车，
    单价就是详情页显示的**渔货价**（向平台采购价）。同一款货 + 同一个规格只有一行，
    重复加购只累加数量（`uq_cart_line`）。
    """

    __tablename__ = "purchase_cart_items"
    __table_args__ = (UniqueConstraint("owner", "item_id", "sku_id", name="uq_cart_line"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 归属账号：登录用户名（未登录时记 anonymous）—— 各人各车，互不串
    owner: Mapped[str] = mapped_column(String(64), index=True)
    item_id: Mapped[str] = mapped_column(String(64), index=True)
    sku_id: Mapped[str] = mapped_column(String(64), default="")
    spec_text: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(512))
    image: Mapped[str | None] = mapped_column(String(512))
    supplier_id: Mapped[str | None] = mapped_column(String(64))
    supplier_name: Mapped[str | None] = mapped_column(String(128))
    # 采购单价（渔货价）与小计 = 单价 × 数量
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    # 加购那一刻的可售库存：结算页把数量上限卡在这里，避免超出
    stock: Mapped[int] = mapped_column(Integer, default=0)
    qty: Mapped[int] = mapped_column(Integer, default=1)
    channel: Mapped[str] = mapped_column(String(16), default="xianyu")
    # 每款货单独的收货地址（2026-10-12 用户拍板：采购车可以一次给好几个人各带一件，
    # 结算时按「收货地址 + 供应商」拆成几张子单，一次付钱、分开发货）
    receiver_name: Mapped[str | None] = mapped_column(String(64))
    receiver_phone: Mapped[str | None] = mapped_column(String(32))
    receiver_address: Mapped[str | None] = mapped_column(Text)


# ====== 5.2 StockOrder / StockOrderItem：采购单（「立即下单」与采购车结算的产物）======
class StockOrder(Base):
    """采购单：渔货栈向渔货栈平台的采购/备货订单（含支付结算，参照 1688 结算页）。

    状态（见 src/self_purchase/order.py 的 STATUS_TEXT / 流转函数）：
    pending_pay 待支付 → paid 待发货 → shipped 已发货 → completed 已完成，
    另有 cancelled 已取消。
    """

    __tablename__ = "stock_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    # pending_pay / paid / shipped / completed / cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending_pay", index=True)
    source: Mapped[str] = mapped_column(String(16), default="buy_now")  # buy_now / cart

    # ---- 商品与金额（照 1688 结算页的「价格明细」）----
    item_kinds: Mapped[int] = mapped_column(Integer, default=0)   # 商品总计 N 种
    total_qty: Mapped[int] = mapped_column(Integer, default=0)    # 共 N 件
    goods_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    freight: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    pay_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)  # 应付

    # ---- 支付（模拟支付通道：余额 / 支付宝 / 微信）----
    pay_method: Mapped[str | None] = mapped_column(String(16))
    pay_no: Mapped[str | None] = mapped_column(String(64))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)

    # ---- 交易与配送方式、收货信息、留言 ----
    trade_type: Mapped[str] = mapped_column(String(16), default="agent")    # 带教代收（老订单有 escrow）
    delivery_type: Mapped[str] = mapped_column(String(16), default="express")  # 快递
    receiver_name: Mapped[str | None] = mapped_column(String(64))
    receiver_phone: Mapped[str | None] = mapped_column(String(32))
    receiver_address: Mapped[str | None] = mapped_column(Text)
    remark: Mapped[str | None] = mapped_column(Text)

    # ---- 履约 ----
    shipping_company: Mapped[str | None] = mapped_column(String(64))
    shipping_no: Mapped[str | None] = mapped_column(String(128))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancel_reason: Mapped[str | None] = mapped_column(String(255))

    # 供应商（结算页「店铺明细」那一栏的归属，来自货源的供应商）
    supplier_id: Mapped[str | None] = mapped_column(String(64))
    supplier_name: Mapped[str | None] = mapped_column(String(128))

    # ---- 合并支付 / 拆单履约（2026-10-12 用户拍板）----
    # order_kind：normal 普通单（立即下单） / main 合并单（只收钱，不发货） / sub 子单（各发各的）
    order_kind: Mapped[str] = mapped_column(String(16), default="normal", index=True)
    # 子单记母单单号（母单为空）；母单的金额 = 各子单合计
    parent_order_no: Mapped[str | None] = mapped_column(String(64), index=True)
    # 子单序号（1 起），母单与 normal 都是 0
    sub_index: Mapped[int] = mapped_column(Integer, default=0)
    # 子单数（母单上记着拆成了几张）
    sub_count: Mapped[int] = mapped_column(Integer, default=0)


class StockOrderItem(Base):
    """采购单明细行（一单一行的写法照 1688 结算页：商品信息 / 数量 / 单价 / 小计）。"""

    __tablename__ = "stock_order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("stock_orders.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[str] = mapped_column(String(64), index=True)
    sku_id: Mapped[str] = mapped_column(String(64), default="")
    spec_text: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(512))
    image: Mapped[str | None] = mapped_column(String(512))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    qty: Mapped[int] = mapped_column(Integer, default=1)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)  # 小计


# ====== 5.3 ShopGoods：店铺商品管理池（详情页「店铺铺货」）======
class ShopGoods(Base):
    """店铺商品池里的一件货：个人闲置店铺没有 PC 后台，所以「店铺铺货」= 加进这里，
    池子里的商品可以**打包下载素材**（图片 / 文案 / 规格表），拿去手机端发布。"""

    __tablename__ = "shop_goods"
    __table_args__ = (
        UniqueConstraint("owner", "shop_id", "item_id", name="uq_shop_goods"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    # 归属店铺（渔枢系统的店铺 id 字符串）；空串 = 还没指定店铺的默认池
    shop_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    shop_name: Mapped[str | None] = mapped_column(String(128))
    item_id: Mapped[str] = mapped_column(String(64), index=True)

    title: Mapped[str | None] = mapped_column(String(512))
    image: Mapped[str | None] = mapped_column(String(512))
    # 渔货价 / 建议售价：池子里两个价都摆出来，方便手机上填
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    suggested_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    stock: Mapped[int] = mapped_column(Integer, default=0)

    # pooled 已入池 / removed 已移出
    status: Mapped[str] = mapped_column(String(16), default="pooled", index=True)
    # 最近一次下载素材的时间与次数
    materials_at: Mapped[datetime | None] = mapped_column(DateTime)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text)


# ====== 6. DownstreamOrder：下游订单 ======
class DownstreamOrder(Base):
    __tablename__ = "downstream_orders"
    __table_args__ = (UniqueConstraint("downstream_order_id", "channel", name="uq_dso_channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    downstream_order_id: Mapped[str] = mapped_column(String(128), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="xianyu")
    channel_sku_id: Mapped[str] = mapped_column(String(128), index=True)
    buyer_name: Mapped[str | None] = mapped_column(String(64))
    buyer_phone: Mapped[str | None] = mapped_column(String(32))
    address: Mapped[str | None] = mapped_column(Text)

    qty: Mapped[int] = mapped_column(Integer, default=1)
    sale_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    # 渔货价（渔货栈向渔货栈拿货的成本价，2026-10-03）：收单时按商品落一份单价。
    # 渔货栈毛利口径 = 销售价 − 运费 + 退款给客户 − 渔货价 + 平台退款 − 平台技术服务费，
    # 以前页面直接借用渔枢系统的 cost_amount（飞书群同步的成本价），跟渔货价不是一个数。
    # 记在订单上而不是实时查商品：改价不该改写历史单的毛利。
    fish_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # 电商正向：pending 待确认 → confirmed 待推单 → pushed 已推单（渔枢系统采购中）
    #           → shipped 已发货 → completed 已完成
    # 逆向：refunding 退款中 → refunded 已退款
    # 挂起/终止：exception 异常单 / cancelled 已取消
    # （流转规则与动作见 src/order/state.py，别在这里手写状态字符串）
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # ---- 履约（渔枢系统采购发货后回写；渔货栈自己也能补录）----
    # 推给渔枢系统之后，渔枢系统那笔订单的 id（回写与跨端跳转都用它）
    hub_order_id: Mapped[int | None] = mapped_column(Integer)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime)
    shipping_company: Mapped[str | None] = mapped_column(String(64))
    shipping_no: Mapped[str | None] = mapped_column(String(128), index=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 退款金额：没填按售价（sale_price）记。
    # **符号口径（2026-10-03）：存负值**（与渔枢系统一致，给出去的钱记负）；
    # 页面 / 接口填正数，由 state.plan 统一转 -abs。
    refund_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime)

    # ---- 归属商家（订单对应的供货商，用于商家端订单管理与结算分账）----
    merchant_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("merchants.id"), index=True
    )


# ====== 7. PurchaseOrder：1688 采购 / 代发单 ======
class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        # 幂等键：下游订单 + SKU
        UniqueConstraint("downstream_order_id", "ali_sku_id", name="uq_dso_ali_sku"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    downstream_order_id: Mapped[str] = mapped_column(String(128), index=True)
    ali_item_id: Mapped[str] = mapped_column(String(64), index=True)
    ali_sku_id: Mapped[str] = mapped_column(String(64), index=True)

    qty: Mapped[int] = mapped_column(Integer, default=1)
    cost_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))

    # 1688 推单返回的订单号
    ali_order_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    # init / pushed / paid / shipped / completed / failed
    status: Mapped[str] = mapped_column(String(16), default="init", index=True)
    push_log: Mapped[dict] = mapped_column(JSON, default=dict)

    # ---- 归属商家（采购成本归属，结算时作为货款抵扣依据）----
    merchant_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("merchants.id"), index=True
    )


# ====== 7.5 OrderEvent：订单状态流转流水 ======
class OrderEvent(Base):
    """订单状态机流水：每做一次动作记一条，详情页的时间线就是它。

    只追加、不修改 —— 状态怎么走到今天的，靠这张表说得清。
    """

    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    downstream_order_id: Mapped[str] = mapped_column(String(128), index=True)
    # 动作 key（见 src/order/state.py ACTIONS）
    action: Mapped[str] = mapped_column(String(32), index=True)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    # 谁做的：登录名 / '渔枢系统' / '系统'
    actor: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str | None] = mapped_column(Text)
    # 当时的快照（物流单号 / 退款金额等），便于事后对账
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # created_at / updated_at 用基类 Base 的（server_default now()）—— 别在这里重复声明，
    # 子类覆盖同名列会把 server_default 和 default 混在一起，SQLite 插入时会报类型错。


# ====== 8. ExceptionLog：异常队列 ======
class ExceptionLog(Base):
    __tablename__ = "exception_logs"

    # SQLite 的自增要求 PK 是 INTEGER；PostgreSQL 仍走 Integer（21 亿条够 MVP）
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


# ====== 9. UserAddress：渔货栈收货地址簿（个人中心 · 地址管理）======
class UserAddress(Base):
    """收货地址簿：一个账号多份收货地址，结算页一键带入。

    归属口径与采购域一致（`owner` = `u{登录用户 id}`，未登录 anonymous），
    各人各的地址；下单时照旧把收货人 / 电话 / 地址落到 stock_orders 的
    `receiver_*` 三个字段上 —— 地址以后改了，历史采购单不受影响。
    """

    __tablename__ = "user_addresses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    receiver_name: Mapped[str] = mapped_column(String(64), default="")
    phone: Mapped[str] = mapped_column(String(32), default="")
    # 省 / 市 / 区（一个文本，不拆三列：填起来快，导入导出也不容易错位）
    region: Mapped[str] = mapped_column(String(128), default="")
    address: Mapped[str] = mapped_column(String(255), default="")
    # 标签：家 / 公司 / 仓库（选填，列表上只是个小标）
    tag: Mapped[str] = mapped_column(String(16), default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # created_at / updated_at 用基类 Base 的（server_default now()），
    # 这里不再重复声明（子类覆盖同名列会把 default 与 server_default 混在一起）。


# ====== 9.1 GuidePref：新手指引「以后不再弹出」======
class GuidePref(Base):
    """渔货栈新手指引的个人偏好（按渔枢账号存）。

    用户 2026-10-11 拍板：弹窗里勾了「以后不再弹出」就**永远**不再弹他
    （换浏览器 / 换设备也一样，所以存在服务端而不是 localStorage）；
    没勾就是「这次先关掉」—— 下次登录还提醒，直到四步做完。
    """

    __tablename__ = "self_guide_prefs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 与采购域同一口径：owner = u{渔枢账号 id}
    owner: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ====== 商家入驻 / 资金结算模型（独立文件，见 src/models/merchant.py） ======
# 注意：必须在 Base 与上述模型定义完成之后 import，否则 merchant.py 里的
# `from src.models import Base` 会因循环导入拿不到 Base。
from src.models.external_identity import ExternalIdentity  # noqa: E402,F401
from src.models.merchant import (  # noqa: E402
    APPLY_STATUS,
    MERCHANT_STATUS,
    SETTLE_STATUS,
    SUBJECT_TYPES,
    USER_ROLES,
    WITHDRAW_STATUS,
    Merchant,
    MerchantApply,
    MerchantUser,
    MerchantWallet,
    SettlementBill,
    User,
    WithdrawApply,
)

# 渔货栈账号 ↔ 渔枢店铺的关联台账（首登自动关联 / 后续同步标记）
# 同样必须在 Base 与上述模型定义完成之后 import。
from src.models.shop_link import (  # noqa: E402
    LINK_SOURCE_AUTO_NAME,
    LINK_SOURCE_AUTO_OWNER,
    LINK_SOURCE_AUTO_PHONE,
    LINK_SOURCE_MANUAL,
    LINK_SOURCE_TEXT,
    ShopSyncMark,
    ShopUserLink,
    source_text as shop_link_source_text,
)

# 渔集价格变动通知（2026-10-15）：渔集改价同步过来时在这里落一条人话通知
from src.models.price_change_notice import PriceChangeNotice  # noqa: E402,F401


__all__ = [
    "Base",
    "Shop",
    "SupplierItem",
    "SkuMapping",
    "PriceRule",
    "ChannelListing",
    "DownstreamOrder",
    "PurchaseOrder",
    "ExceptionLog",
    "UserAddress",
    "GuidePref",
    # 商家入驻 / 结算
    "User",
    "Merchant",
    "MerchantUser",
    "MerchantApply",
    "SettlementBill",
    "MerchantWallet",
    "WithdrawApply",
    "SUBJECT_TYPES",
    "MERCHANT_STATUS",
    "APPLY_STATUS",
    "USER_ROLES",
    "SETTLE_STATUS",
    "WITHDRAW_STATUS",
    # 店铺关联台账（首登自动关联 / 后续同步）
    "ShopUserLink",
    "ShopSyncMark",
    "LINK_SOURCE_AUTO_PHONE",
    "LINK_SOURCE_AUTO_NAME",
    "LINK_SOURCE_AUTO_OWNER",
    "LINK_SOURCE_MANUAL",
    "LINK_SOURCE_TEXT",
    "shop_link_source_text",
    # 渔集价格变动通知
    "PriceChangeNotice",
]