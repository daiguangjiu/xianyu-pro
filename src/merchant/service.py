"""商家入驻 + 商家端商品管理业务逻辑。

分层约定（见 CODEBUDDY.md）：路由只处理 HTTP，业务规则全部落在这里。

覆盖范围：
- 阶段 1：账号注册 / 登录、入驻申请提交、平台审核（通过则开通商家 + 开钱包）
- 阶段 2：商家发布商品（进平台待审核）、商品列表、改价改库存、上下架
- 阶段 4 预留：钱包 / 结算单 / 提现（表已建，本轮只做钱包初始化）

数据安全：所有商家维度的查询都强制带 `merchant_id` 过滤，避免越权；
越权访问返回 None（由路由层统一转 403/404），不抛异常。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, func as sql_func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from src.config import get_settings
from src.goods.catalog import sourcing_tree
from src.models import (
    DownstreamOrder,
    Merchant,
    MerchantApply,
    MerchantUser,
    MerchantWallet,
    PurchaseOrder,
    SettlementBill,
    SkuMapping,
    SupplierItem,
    User,
    WithdrawApply,
)
# 两端一套账号、一套密码（2026-09-30 合并）：哈希与校验都走渔枢系统那套，
# 渔货栈老账号的 bcrypt 老密码首次登录时自动升级，老用户不用重置密码。
from app.portals import ALL_PORTALS, PORTAL_SELF
from app.web.auth import hash_password, verify_user_password
from app.timeutil import beijing_now, to_iso, utc_now


# =====================================================================
# 类目选项（商家发布商品时的下拉候选，复用货源广场的类目树）
# =====================================================================
def list_category_options() -> list[dict[str, str]]:
    """返回 [{"value": 二级类目id, "label": "一级 / 二级"}]（与渔枢系统类目表同一份数据）。"""
    options: list[dict[str, str]] = []
    for top in sourcing_tree():
        for child in top.get("children", []):
            options.append(
                {
                    "value": child["id"],
                    "label": f"{top['name']} / {child['name']}",
                }
            )
    return options


def category_label(category_id: str | None) -> str:
    """二级类目 id → 中文展示名（未命中返回原值）。"""
    if not category_id:
        return "—"
    for top in sourcing_tree():
        for child in top.get("children", []):
            if child["id"] == category_id:
                return f"{top['name']} / {child['name']}"
    return category_id


# =====================================================================
# 账号
# =====================================================================
def get_user_by_phone(db: Session, phone: str) -> User | None:
    return db.scalar(select(User).where(User.phone == phone.strip()))


def get_user_by_id(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def register_user(
    db: Session,
    phone: str,
    password: str,
    nickname: str = "",
    role: str = "merchant",
) -> User:
    """注册账号。调用方需先确认手机号未被占用。

    role 只允许 merchant / reseller；platform_admin 只能由运维在库内指定，
    避免 anyone 注册即拿平台管理权限。
    """
    if role not in ("merchant", "reseller"):
        role = "merchant"
    user = User(
        # 渔枢系统的账号按 username 登录，渔货栈老账号是手机号 —— 手机号直接当用户名，
        # 登录时用户名 / 手机号都认（见 app/web/auth.py 的 authenticate）。
        username=phone.strip(),
        phone=phone.strip(),
        password_hash=hash_password(password),
        display_name=(nickname or phone).strip(),
        # 角色统一按渔枢系统的口径：管理员 admin、其余 user（渔货栈的 merchant / reseller
        # 不再是账号角色，商家身份看 MerchantUser 关联）；端权限写进 portals。
        role="user",
        portals=PORTAL_SELF,
        status="active",
    )
    db.add(user)
    db.flush()
    return user


def authenticate(db: Session, phone: str, password: str) -> User | None:
    """校验账号密码；成功返回 User（并刷新 last_login_at），失败返回 None。

    校验统一走渔枢系统那套（两端一套密码）：老账号的 bcrypt 老密码在首次
    登录时自动升级成新哈希，老用户不用重置密码。
    """
    user = get_user_by_phone(db, phone)
    if user is None or user.status != "active":
        return None
    if not verify_user_password(db, user, password):
        return None
    user.last_login_at = utc_now()
    db.flush()
    return user


def ensure_platform_admin(db: Session) -> User:
    """确保存在一个平台管理员账号（幂等）。

    platform_admin 不能通过注册接口产生（见 register_user 的 role 白名单），
    只能由运维在库内指定；这里用配置项兜底自动创建，避免新环境部署后
    审核台无人可登录、审核动作全部卡死。

    账号/密码来自配置：PLATFORM_ADMIN_PHONE / PLATFORM_ADMIN_PASSWORD，
    默认值仅用于 demo，生产部署必须通过环境变量改掉。
    """
    settings = get_settings()
    phone = (getattr(settings, "platform_admin_phone", "") or "13800000000").strip()
    password = getattr(settings, "platform_admin_password", "") or "admin123456"

    user = get_user_by_phone(db, phone)
    if user is None:
        user = User(
            username=phone,
            phone=phone,
            password_hash=hash_password(password),
            display_name="平台管理员",
            # 角色按渔枢系统的口径：管理员 = admin，且全端可进（管理员不看 portals，写全端）
            role="admin",
            portals=ALL_PORTALS,
            status="active",
        )
        db.add(user)
        db.flush()
        return user
    # 已存在同名账号：强制纠正为管理员角色（历史数据可能是普通成员）
    if user.role != "admin":
        user.role = "admin"
        if PORTAL_SELF not in str(user.portals or ""):
            user.portals = ALL_PORTALS
        db.flush()
    return user


# =====================================================================
# 商家归属
# =====================================================================
def get_merchant_of_user(db: Session, user: User) -> Merchant | None:
    """当前账号关联的商家（取第一个生效的；总账号通常只有一个）。

    同时兼容两种关联方式：
    1. MerchantUser 关联表（多店 / 子账号场景）
    2. Merchant.owner_user_id 直接指向（单店场景，入驻时写入）
    """
    link = db.scalar(
        select(MerchantUser).where(
            MerchantUser.user_id == user.id, MerchantUser.is_active.is_(True)
        )
    )
    if link is not None:
        return db.get(Merchant, link.merchant_id)
    return db.scalar(select(Merchant).where(Merchant.owner_user_id == user.id))


def ensure_wallet(db: Session, merchant: Merchant) -> MerchantWallet:
    """商家钱包：不存在则建（入驻通过 / 首次访问资金页时调用）。"""
    wallet = db.scalar(
        select(MerchantWallet).where(MerchantWallet.merchant_id == merchant.id)
    )
    if wallet is None:
        wallet = MerchantWallet(merchant_id=merchant.id)
        db.add(wallet)
        db.flush()
    return wallet


# =====================================================================
# 入驻申请 + 平台审核
# =====================================================================
def build_apply_from_form(db: Session, user: User, form: dict[str, Any]) -> MerchantApply:
    """把表单字段落成申请快照。所有字段都放申请单，审核通过时才写 Merchant。"""
    cats = form.get("categories") or []
    if isinstance(cats, str):
        cats = [c.strip() for c in cats.split(",") if c.strip()]

    apply = MerchantApply(
        user_id=user.id,
        name=(form.get("name") or "").strip(),
        short_name=(form.get("short_name") or "").strip() or None,
        subject_type=form.get("subject_type") or "enterprise",
        license_no=(form.get("license_no") or "").strip() or None,
        legal_person=(form.get("legal_person") or "").strip() or None,
        contact_name=(form.get("contact_name") or "").strip() or None,
        contact_phone=(form.get("contact_phone") or "").strip() or None,
        contact_email=(form.get("contact_email") or "").strip() or None,
        categories=cats,
        province=(form.get("province") or "").strip() or None,
        city=(form.get("city") or "").strip() or None,
        address=(form.get("address") or "").strip() or None,
        settle_bank_name=(form.get("settle_bank_name") or "").strip() or None,
        settle_account_name=(form.get("settle_account_name") or "").strip() or None,
        settle_bank_account=(form.get("settle_bank_account") or "").strip() or None,
        status="pending",
        submitted_at=utc_now(),
    )
    db.add(apply)
    db.flush()
    return apply


def latest_apply_of_user(db: Session, user: User) -> MerchantApply | None:
    """当前账号最近一次入驻申请（用于展示审核进度 / 驳回原因）。"""
    return db.scalar(
        select(MerchantApply)
        .where(MerchantApply.user_id == user.id)
        .order_by(MerchantApply.id.desc())
    )


def get_apply(db: Session, apply_id: int) -> MerchantApply | None:
    return db.get(MerchantApply, apply_id)


def review_apply(
    db: Session,
    apply: MerchantApply,
    *,
    approve: bool,
    note: str = "",
    reviewer: str = "platform",
) -> MerchantApply:
    """平台审核入驻申请。

    - 通过：建 Merchant（状态 approved）+ 建 MerchantUser(owner) + 开钱包
    - 驳回：只记理由，商家可改资料后重新提交
    """
    apply.review_note = note or None
    apply.reviewed_by = reviewer
    apply.reviewed_at = utc_now()

    if not approve:
        apply.status = "rejected"
        db.flush()
        return apply

    apply.status = "approved"

    # 已通过过的账号重复审核（驳回后重提）→ 更新原商家而不是新建
    merchant = db.scalar(
        select(Merchant).where(Merchant.owner_user_id == apply.user_id)
    )
    if merchant is None:
        merchant = Merchant(owner_user_id=apply.user_id)

    # 用申请快照覆盖商家资料（保证"改资料 → 重审"生效）
    # 注意：必须先填满字段再 add/flush，否则 name(NOT NULL) 会插入失败
    merchant.name = apply.name
    merchant.short_name = apply.short_name
    merchant.subject_type = apply.subject_type
    merchant.license_no = apply.license_no
    merchant.legal_person = apply.legal_person
    merchant.contact_name = apply.contact_name
    merchant.contact_phone = apply.contact_phone
    merchant.contact_email = apply.contact_email
    merchant.categories = apply.categories
    merchant.province = apply.province
    merchant.city = apply.city
    merchant.address = apply.address
    merchant.settle_bank_name = apply.settle_bank_name
    merchant.settle_account_name = apply.settle_account_name
    merchant.settle_bank_account = apply.settle_bank_account
    merchant.status = "approved"
    merchant.reject_reason = None
    merchant.approved_at = utc_now()
    db.add(merchant)
    db.flush()  # 拿到 merchant.id，供下方关联与钱包使用

    # 账号 ↔ 商家关联（owner）
    link = db.scalar(
        select(MerchantUser).where(
            MerchantUser.merchant_id == merchant.id,
            MerchantUser.user_id == apply.user_id,
        )
    )
    if link is None:
        db.add(
            MerchantUser(merchant_id=merchant.id, user_id=apply.user_id, role="owner")
        )

    apply.merchant_id = merchant.id
    ensure_wallet(db, merchant)
    db.flush()
    return apply


# =====================================================================
# 商品管理（商家维度）
# =====================================================================
def _to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def create_merchant_item(
    db: Session, merchant: Merchant, form: dict[str, Any]
) -> SupplierItem:
    """商家发布商品。

    - 归属：merchant_id = 商家；supplier_id 用 ``M{merchant_id}`` 便于与 1688 货源区分
    - 来源：source_type = self_operated（与"本站录入"同义，现有商品管理页可直接看到）
    - 审核：audit_status = pending，is_listed = False（平台审核通过后才上架）
    """
    cost = _to_decimal(form.get("cost_price"))
    sale = _to_decimal(form.get("sale_price"), cost)

    images = form.get("images") or []
    if isinstance(images, str):
        images = [u.strip() for u in images.split(",") if u.strip()]

    item = SupplierItem(
        item_id=f"M{merchant.id}-{int(time.time() * 1000)}",
        merchant_id=merchant.id,
        supplier_id=f"M{merchant.id}",
        title=(form.get("title") or "").strip(),
        category_id=(form.get("category_id") or "").strip() or None,
        main_image=images[0] if images else None,
        cost_price=cost,
        sale_price=sale,
        market_price=_to_decimal(form.get("market_price"), sale),
        # 渔货价：渔货栈对外的成本价 / 进货价，商家没填就留空（不填 0，0 是有意义的价格）
        fish_price=_to_decimal(form["fish_price"]) if form.get("fish_price") else None,
        stock=_to_int(form.get("stock")),
        images=images,
        detail_images=[],
        item_code=(form.get("item_code") or "").strip() or None,
        selling_points=(form.get("selling_points") or "").strip() or None,
        spec_defs=[],
        freight_template=(form.get("freight_template") or "").strip() or None,
        delivery_days=_to_int(form.get("delivery_days"), 2),
        after_sale=(form.get("after_sale") or "7天无理由退换").strip(),
        detail_html=(form.get("detail_html") or "").strip() or None,
        # 发布即进待审核；不直接上架
        source_type="self_operated",
        audit_status="pending",
        is_listed=False,
        # 货源广场（/self/harbor）按 last_synced_at 倒序扫货源、并取前 8 款进「最新上架」：
        # 这里不写时间戳的话该列为 NULL，NULLS LAST 会把商家商品排到扫描窗口的最末尾
        # （货源一超 HARBOR_SCAN_LIMIT 条就被整个切掉），商家就会觉得「我发的商品没展示」。
        last_synced_at=utc_now(),
        raw_json={"merchant_id": merchant.id, "merchant_name": merchant.name},
    )
    db.add(item)
    db.flush()
    return item


def list_merchant_items(
    db: Session,
    merchant: Merchant,
    *,
    keyword: str = "",
    audit_status: str = "",
    listed: str = "",
) -> list[SupplierItem]:
    """本店商品列表（强制 merchant_id 隔离）。"""
    stmt = select(SupplierItem).where(SupplierItem.merchant_id == merchant.id)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(SupplierItem.title.like(like))
    if audit_status:
        stmt = stmt.where(SupplierItem.audit_status == audit_status)
    if listed in ("1", "0"):
        stmt = stmt.where(SupplierItem.is_listed.is_(listed == "1"))
    return list(
        db.scalars(stmt.order_by(SupplierItem.id.desc())).all()
    )


def get_merchant_item(
    db: Session, merchant: Merchant, item_id: str
) -> SupplierItem | None:
    """取本店商品；跨店访问返回 None（路由层转 404，不泄露他店数据）。"""
    return db.scalar(
        select(SupplierItem).where(
            SupplierItem.item_id == item_id,
            SupplierItem.merchant_id == merchant.id,
        )
    )


def update_merchant_item(
    db: Session, merchant: Merchant, item_id: str, form: dict[str, Any]
) -> SupplierItem | None:
    """编辑本店商品（改价 / 改库存 / 改文案）。

    注意：编辑后重新进待审核（防止审核通过后偷偷改价改图）。
    """
    item = get_merchant_item(db, merchant, item_id)
    if item is None:
        return None

    if "title" in form:
        item.title = (form["title"] or "").strip()
    if "sale_price" in form:
        item.sale_price = _to_decimal(form["sale_price"], item.sale_price or Decimal("0"))
    if "cost_price" in form:
        item.cost_price = _to_decimal(form["cost_price"], item.cost_price or Decimal("0"))
    if "market_price" in form:
        item.market_price = _to_decimal(form["market_price"], item.market_price)
    if "fish_price" in form:
        item.fish_price = _to_decimal(form["fish_price"], item.fish_price)
    if "stock" in form:
        item.stock = _to_int(form["stock"], item.stock)
    if "selling_points" in form:
        item.selling_points = (form["selling_points"] or "").strip() or None
    if "category_id" in form:
        item.category_id = (form["category_id"] or "").strip() or None
    if "delivery_days" in form:
        item.delivery_days = _to_int(form["delivery_days"], item.delivery_days)
    if "after_sale" in form:
        item.after_sale = (form["after_sale"] or item.after_sale).strip()

    # 实质性修改后重新审核（审核中/已驳回的商品保持原状态，不反复重置）
    if item.audit_status == "approved":
        item.audit_status = "pending"
        item.audit_note = "商家修改商品信息，重新待审核"
        item.is_listed = False
    db.flush()
    return item


def set_item_listed(
    db: Session, merchant: Merchant, item_id: str, listed: bool
) -> SupplierItem | None:
    """上下架。只有审核通过的商品才能上架。"""
    item = get_merchant_item(db, merchant, item_id)
    if item is None:
        return None
    if listed and item.audit_status != "approved":
        return None  # 未审核通过不允许上架
    item.is_listed = listed
    if listed:
        # 上架即「最近上新」：货源广场按这个时间排「最新上架」，不刷的话重新上架的商品
        # 还是排在老位置（甚至因为 NULL 排到最后），商家会找不到自己刚上架的商品
        item.last_synced_at = utc_now()
    db.flush()
    return item


def merchant_item_stats(db: Session, merchant: Merchant) -> dict[str, int]:
    """商家后台概览：在售 / 待审核 / 已驳回 / 已下架。"""
    rows = db.execute(
        select(SupplierItem.audit_status, SupplierItem.is_listed, sql_func.count(SupplierItem.id))
        .where(SupplierItem.merchant_id == merchant.id)
        .group_by(SupplierItem.audit_status, SupplierItem.is_listed)
    ).all()
    stats = {"total": 0, "on_sale": 0, "pending": 0, "rejected": 0, "off_shelf": 0}
    for audit_status, is_listed, cnt in rows:
        stats["total"] += cnt
        if audit_status == "pending":
            stats["pending"] += cnt
        elif audit_status == "rejected":
            stats["rejected"] += cnt
        elif audit_status == "approved":
            if is_listed:
                stats["on_sale"] += cnt
            else:
                stats["off_shelf"] += cnt
    return stats


# =====================================================================
# 阶段 3：订单管理 + 发货
# =====================================================================
# 商家可见的订单状态机（DownstreamOrder.status）：
#     pending    待接单（下游刚下单）
#     confirmed  已接单（商家确认）
#     pushed     已推单（已下采购单给上游）
#     shipped    已发货（商家填了运单号）
#     completed  已完成（买家签收）
#     refunded   已退款（不参与结算）
#     exception  异常（不参与结算）
ORDER_STATUS_TEXT = {
    "pending": "待接单",
    "confirmed": "已接单",
    "pushed": "待发货",
    "shipped": "已发货",
    "completed": "已完成",
    "refunded": "已退款",
    "exception": "异常",
}
# 结算时计入的有效状态（已发货之后才算商家履约完成）
SETTLEABLE_STATUSES = ("shipped", "completed")


def list_merchant_orders(
    db: Session,
    merchant: Merchant,
    *,
    status: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[DownstreamOrder], int]:
    """本店订单列表（强制 merchant_id 过滤）。返回 (当前页, 总数)。"""
    conds = [DownstreamOrder.merchant_id == merchant.id]
    if status:
        conds.append(DownstreamOrder.status == status)
    if keyword:
        kw = f"%{keyword.strip()}%"
        conds.append(
            or_(
                DownstreamOrder.downstream_order_id.like(kw),
                DownstreamOrder.buyer_name.like(kw),
                DownstreamOrder.buyer_phone.like(kw),
            )
        )
    where = and_(*conds)
    total = db.scalar(
        select(sql_func.count(DownstreamOrder.id)).where(where)
    ) or 0
    rows = db.scalars(
        select(DownstreamOrder)
        .where(where)
        .order_by(DownstreamOrder.id.desc())
        .offset(max(0, (page - 1) * page_size))
        .limit(page_size)
    ).all()
    return list(rows), int(total)


def get_merchant_order(
    db: Session, merchant: Merchant, order_id: int
) -> DownstreamOrder | None:
    """取本店订单；跨商家访问返回 None（路由层转 404，避免探测他人订单）。"""
    return db.scalar(
        select(DownstreamOrder).where(
            DownstreamOrder.id == order_id,
            DownstreamOrder.merchant_id == merchant.id,
        )
    )


def merchant_order_stats(db: Session, merchant: Merchant) -> dict[str, int]:
    """本店订单各状态计数（概览卡片 + 侧边栏角标）。"""
    rows = db.execute(
        select(DownstreamOrder.status, sql_func.count(DownstreamOrder.id))
        .where(DownstreamOrder.merchant_id == merchant.id)
        .group_by(DownstreamOrder.status)
    ).all()
    stats = {
        "total": 0,
        "pending": 0,      # 待接单
        "to_ship": 0,      # 待发货（confirmed + pushed）
        "shipped": 0,
        "completed": 0,
        "refunded": 0,
        "exception": 0,
    }
    for status, cnt in rows:
        stats["total"] += cnt
        if status == "pending":
            stats["pending"] += cnt
        elif status in ("confirmed", "pushed"):
            stats["to_ship"] += cnt
        elif status == "shipped":
            stats["shipped"] += cnt
        elif status == "completed":
            stats["completed"] += cnt
        elif status == "refunded":
            stats["refunded"] += cnt
        elif status == "exception":
            stats["exception"] += cnt
    return stats


def confirm_order(db: Session, order: DownstreamOrder) -> tuple[bool, str]:
    """商家接单：pending → confirmed。返回 (是否成功, 提示语)。"""
    if order.status != "pending":
        return False, f"当前状态「{order_status_text(order.status)}」不可接单"
    order.status = "confirmed"
    raw = dict(order.raw_json or {})
    raw["confirmed_at"] = to_iso(utc_now())
    order.raw_json = raw
    db.flush()
    return True, "已接单，可备货发货"


def ship_order(
    db: Session,
    order: DownstreamOrder,
    *,
    carrier: str,
    shipping_no: str,
) -> tuple[bool, str]:
    """商家发货：写运单号 + 推进状态到 shipped。

    复用 src.logistics.service.receive_callback（同一套 raw_json['logistics']
    结构与 PO 状态推进规则），只是 source 标记为 'merchant'，
    保证"商家后台发货"与"1688 物流回调"落库结果一致。
    """
    if order.status == "refunded":
        return False, "订单已退款，不能发货"
    if order.status == "completed":
        return False, "订单已完成，不能重复发货"
    if not shipping_no.strip():
        return False, "请填写物流单号"

    from src.logistics.service import receive_callback

    try:
        receive_callback(
            db,
            {
                "downstream_order_id": order.downstream_order_id,
                "shipping_no": shipping_no.strip(),
                "carrier": carrier.strip(),
                "status": "shipped",
            },
            source="merchant",
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"发货失败：{exc}"

    db.refresh(order)
    return True, f"已发货（{carrier or '物流'} {shipping_no}）"


def order_goods_amount(order: DownstreamOrder) -> Decimal:
    """订单货款 = 售价 × 数量（结算口径：按下游实收计）。"""
    price = _to_decimal(order.sale_price)
    return (price * int(order.qty or 1)).quantize(Decimal("0.01"))


def order_status_text(status: str | None) -> str:
    return ORDER_STATUS_TEXT.get(status or "", status or "-")


def order_logistics(order: DownstreamOrder) -> dict[str, Any] | None:
    """取订单的物流快照 raw_json['logistics']。"""
    return (order.raw_json or {}).get("logistics")


# =====================================================================
# 阶段 4：结算（平台代收代付 + 抽佣）
# =====================================================================
def _next_bill_no(merchant_id: int) -> str:
    """结算单号：JS + 商家号 + 年月日时分秒 + 随机尾号。"""
    stamp = beijing_now().strftime("%Y%m%d%H%M%S")  # 单号里的日期按北京自然日
    return f"JS{merchant_id:04d}{stamp}{int(time.time() * 1000) % 1000:03d}"


def generate_settlement(
    db: Session,
    merchant: Merchant,
    *,
    note: str | None = None,
) -> tuple[SettlementBill | None, str]:
    """为一个商家生成结算单。

    口径：
    - 纳入：本店 status in (shipped, completed) 且尚未结算过的订单
    - 货款 goods_amount = Σ(售价 × 数量)
    - 佣金 commission   = 货款 × merchant.commission_rate（默认 5%）
    - 实结 settle_amount= 货款 - 佣金 - 退款
    - 生成后把订单打标 raw_json['settled_bill_no']，避免重复结算

    生成后：待结算金额进 wallet.pending_balance（ settled 时才转 available）。
    """
    all_orders = db.scalars(
        select(DownstreamOrder).where(
            DownstreamOrder.merchant_id == merchant.id
        )
    ).all()
    # 已结算过的订单（raw_json 打过 bill_no 标）不再重复计入。
    # 在 Python 侧过滤而非 SQL 层：raw_json 的 JSON 取值语法在 SQLite / PG
    # 之间不通用，且单商家订单量很小，没必要为此写方言分支。
    targets = [
        o
        for o in all_orders
        if o.status in SETTLEABLE_STATUSES
        and not (o.raw_json or {}).get("settled_bill_no")
    ]
    # 已结算后又退款的单：本期不重复计货款，但要从本期实结里扣回。
    # 扣过一次就打 deducted_bill_no 标，避免下期重复扣。
    refund_rows = [
        o
        for o in all_orders
        if o.status == "refunded"
        and (o.raw_json or {}).get("settled_bill_no")
        and not ((o.raw_json or {}).get("refund") or {}).get("deducted_bill_no")
    ]
    refund = sum((order_goods_amount(o) for o in refund_rows), Decimal("0"))

    if not targets and not refund_rows:
        return None, "没有可结算的订单（需已发货或已完成，且未结算过）"

    goods = Decimal("0")
    for o in targets:
        goods += order_goods_amount(o)

    rate = _to_decimal(merchant.commission_rate, Decimal("0.05"))
    commission = (goods * rate).quantize(Decimal("0.01"))
    settle = (goods - commission - refund).quantize(Decimal("0.01"))

    bill = SettlementBill(
        bill_no=_next_bill_no(merchant.id),
        merchant_id=merchant.id,
        period_start=utc_now(),
        period_end=utc_now(),
        order_count=len(targets),
        goods_amount=goods,
        commission=commission,
        refund_amount=refund,
        settle_amount=settle,
        status="pending",
        note=note or "",
    )
    db.add(bill)
    db.flush()

    # 打标，避免重复结算 / 重复扣回
    for o in targets:
        raw = dict(o.raw_json or {})
        raw["settled_bill_no"] = bill.bill_no
        o.raw_json = raw
        flag_modified(o, "raw_json")
    for o in refund_rows:
        raw = dict(o.raw_json or {})
        refund_mark = dict(raw.get("refund") or {})
        refund_mark["deducted_bill_no"] = bill.bill_no
        raw["refund"] = refund_mark
        o.raw_json = raw
        flag_modified(o, "raw_json")
    db.flush()

    wallet = ensure_wallet(db, merchant)
    wallet.pending_balance = _to_decimal(wallet.pending_balance) + settle
    db.commit()
    return bill, f"已生成结算单 {bill.bill_no}：货款 ¥{goods}，佣金 ¥{commission}，实结 ¥{settle}"


def mark_bill_settled(db: Session, bill: SettlementBill) -> tuple[bool, str]:
    """平台把结算单标记为已结算：pending_balance → available_balance。"""
    if bill.status != "pending":
        return False, f"当前状态「{bill.status_text}」不可重复结算"
    wallet = ensure_wallet(db, db.get(Merchant, bill.merchant_id))
    settle = _to_decimal(bill.settle_amount)
    wallet.pending_balance = max(
        Decimal("0"), _to_decimal(wallet.pending_balance) - settle
    )
    wallet.available_balance = _to_decimal(wallet.available_balance) + settle
    bill.status = "settled"
    bill.settled_at = utc_now()
    db.commit()
    return True, f"结算单 {bill.bill_no} 已结算，¥{settle} 转入可提现余额"


def list_bills(db: Session, merchant: Merchant) -> list[SettlementBill]:
    return list(
        db.scalars(
            select(SettlementBill)
            .where(SettlementBill.merchant_id == merchant.id)
            .order_by(SettlementBill.id.desc())
        ).all()
    )


def apply_withdraw(
    db: Session,
    merchant: Merchant,
    *,
    amount: Decimal,
    bank_name: str = "",
    account_name: str = "",
    bank_account: str = "",
) -> tuple[WithdrawApply | None, str]:
    """商家申请提现：校验余额 → 冻结 → 建申请单。"""
    wallet = ensure_wallet(db, merchant)
    available = _to_decimal(wallet.available_balance)
    amount = _to_decimal(amount)
    if amount <= 0:
        return None, "提现金额必须大于 0"
    if amount > available:
        return None, f"可提现余额不足（当前 ¥{available}）"

    wallet.available_balance = available - amount
    wallet.frozen_balance = _to_decimal(wallet.frozen_balance) + amount

    apply_row = WithdrawApply(
        merchant_id=merchant.id,
        apply_no=f"TX{merchant.id:04d}{int(time.time() * 1000)}",
        amount=amount,
        bank_name=bank_name or merchant.settle_bank_name or "",
        account_name=account_name or merchant.settle_account_name or "",
        bank_account=bank_account or merchant.settle_bank_account or "",
        status="pending",
    )
    db.add(apply_row)
    db.flush()
    db.commit()
    return apply_row, f"提现申请已提交（¥{amount}），等待平台审核"


def list_withdraws(db: Session, merchant: Merchant) -> list[WithdrawApply]:
    return list(
        db.scalars(
            select(WithdrawApply)
            .where(WithdrawApply.merchant_id == merchant.id)
            .order_by(WithdrawApply.id.desc())
        ).all()
    )


def review_withdraw(
    db: Session,
    apply_row: WithdrawApply,
    *,
    approve: bool,
    reviewer: str,
    note: str = "",
) -> tuple[bool, str]:
    """平台审核提现。

    通过：frozen → 扣减，withdrawn_total += amount，status=paid
    驳回：frozen → 退回 available，status=rejected
    """
    if apply_row.status != "pending":
        return False, f"当前状态「{apply_row.status_text}」不可重复审核"

    wallet = ensure_wallet(db, db.get(Merchant, apply_row.merchant_id))
    amount = _to_decimal(apply_row.amount)
    frozen = _to_decimal(wallet.frozen_balance)

    if approve:
        wallet.frozen_balance = max(Decimal("0"), frozen - amount)
        wallet.withdrawn_total = _to_decimal(wallet.withdrawn_total) + amount
        apply_row.status = "paid"
        apply_row.paid_at = utc_now()
    else:
        wallet.frozen_balance = max(Decimal("0"), frozen - amount)
        wallet.available_balance = _to_decimal(wallet.available_balance) + amount
        apply_row.status = "rejected"

    apply_row.review_note = note
    apply_row.reviewed_by = reviewer
    apply_row.reviewed_at = utc_now()
    db.commit()
    return True, ("已打款" if approve else "已驳回并退回余额")


# =====================================================================
# 审核台：商家状态管理 + 退款审核
# =====================================================================
def set_merchant_status(
    db: Session,
    merchant: Merchant,
    *,
    status: str,
    note: str = "",
) -> tuple[bool, str]:
    """平台冻结 / 解冻商家。

    approved → frozen：冻结后商品强制下架，商家后台只读（接单与发货被拒）。
    frozen   → approved：解冻，但不自动恢复上架（由商家自行上架，避免误售）。

    其他状态（pending / rejected）不允许走这里，入驻状态只能由审核流程变更。
    """
    if status not in ("approved", "frozen"):
        return False, f"不支持的状态 {status}"
    if merchant.status not in ("approved", "frozen"):
        return False, f"当前状态「{merchant.status_text}」不可冻结/解冻"
    if merchant.status == status:
        return False, f"商家已经是「{MERCHANT_STATUS.get(status, status)}」"

    merchant.status = status
    if note:
        merchant.reject_reason = note

    if status == "frozen":
        # 冻结 → 本店商品全部下架，避免消费者继续下单
        items = db.scalars(
            select(SupplierItem).where(SupplierItem.merchant_id == merchant.id)
        ).all()
        for it in items:
            it.is_listed = False
    db.commit()
    action = "已冻结" if status == "frozen" else "已解冻"
    return True, f"商家「{merchant.name}」{action}"


def merchant_can_operate(merchant: Merchant) -> bool:
    """商家是否可开展经营活动（冻结后不允许接单/发货）。"""
    return merchant.status == "approved"


def list_refund_candidates(
    db: Session,
    *,
    keyword: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[DownstreamOrder]:
    """审核台退款审核列表。

    默认列出全部订单（含已退款的），这样审核过的记录在页面上可追溯；
    之前按 status != refunded 过滤，导致审完就"消失"，无法回溯。
    """
    conds: list = []
    if status:
        conds.append(DownstreamOrder.status == status)
    if keyword:
        kw = f"%{keyword.strip()}%"
        conds.append(
            or_(
                DownstreamOrder.downstream_order_id.like(kw),
                DownstreamOrder.buyer_name.like(kw),
                DownstreamOrder.buyer_phone.like(kw),
            )
        )
    return list(
        db.scalars(
            select(DownstreamOrder)
            .where(and_(*conds))
            .order_by(DownstreamOrder.id.desc())
            .limit(limit)
        ).all()
    )


def refund_order(
    db: Session,
    order: DownstreamOrder,
    *,
    reason: str = "",
) -> tuple[bool, str]:
    """平台标记订单退款。

    影响三段：
    1. 订单状态 → refunded，写 raw_json['refund'] 留痕（原因 / 时间 / 是否已结算）
    2. 释放预占库存（SkuMapping.reserved_stock 回退），货还能再卖
    3. 结算：若该单此前已结算过，金额在下期结算单的 refund_amount 里扣回
       （见 generate_settlement 的「已结算后退款」分支）
    """
    if order.status == "refunded":
        return False, "该订单已退款，不可重复操作"

    already_settled = bool((order.raw_json or {}).get("settled_bill_no"))

    # 释放预占库存
    ali_sku_id = (order.raw_json or {}).get("ali_sku_id")
    if ali_sku_id:
        mapping = db.scalar(
            select(SkuMapping).where(
                SkuMapping.ali_sku_id == str(ali_sku_id),
                SkuMapping.channel == order.channel,
            )
        )
        if mapping is not None:
            mapping.reserved_stock = max(
                0, int(mapping.reserved_stock or 0) - int(order.qty or 1)
            )

    raw = dict(order.raw_json or {})
    raw["refund"] = {
        "reason": reason or "平台判定退款",
        "refunded_at": to_iso(utc_now()),
        "already_settled": already_settled,
        "amount": str(order_goods_amount(order)),
    }
    order.raw_json = raw
    flag_modified(order, "raw_json")
    order.status = "refunded"

    pos = db.scalars(
        select(PurchaseOrder).where(
            PurchaseOrder.downstream_order_id == order.downstream_order_id
        )
    ).all()
    for po in pos:
        if po.status not in ("completed", "failed"):
            po.status = "failed"
            log = dict(po.push_log or {})
            log["refund_reason"] = reason or "平台判定退款"
            po.push_log = log
            flag_modified(po, "push_log")

    db.commit()
    tip = "（该单已结算，金额将在下期结算单扣回）" if already_settled else ""
    return True, f"订单 {order.downstream_order_id} 已标记退款{tip}"


def merchant_finance_summary(db: Session, merchant: Merchant) -> dict[str, Decimal]:
    """资金概览：可提现 / 待结算 / 冻结 / 累计提现 + 累计货款与佣金。"""
    wallet = ensure_wallet(db, merchant)
    goods_sum = db.scalar(
        select(sql_func.coalesce(sql_func.sum(SettlementBill.goods_amount), 0)).where(
            SettlementBill.merchant_id == merchant.id
        )
    ) or 0
    commission_sum = db.scalar(
        select(sql_func.coalesce(sql_func.sum(SettlementBill.commission), 0)).where(
            SettlementBill.merchant_id == merchant.id
        )
    ) or 0
    return {
        "wallet": wallet,
        "available": _to_decimal(wallet.available_balance),
        "pending": _to_decimal(wallet.pending_balance),
        "frozen": _to_decimal(wallet.frozen_balance),
        "withdrawn": _to_decimal(wallet.withdrawn_total),
        "goods_total": _to_decimal(goods_sum),
        "commission_total": _to_decimal(commission_sum),
        "commission_rate": _to_decimal(merchant.commission_rate, Decimal("0.05")),
    }


__all__ = [
    "list_category_options",
    "category_label",
    "get_user_by_phone",
    "get_user_by_id",
    "register_user",
    "authenticate",
    "get_merchant_of_user",
    "ensure_wallet",
    "build_apply_from_form",
    "latest_apply_of_user",
    "get_apply",
    "review_apply",
    "create_merchant_item",
    "list_merchant_items",
    "get_merchant_item",
    "update_merchant_item",
    "set_item_listed",
    "merchant_item_stats",
    # 阶段 3：订单 / 发货
    "ORDER_STATUS_TEXT",
    "list_merchant_orders",
    "get_merchant_order",
    "merchant_order_stats",
    "confirm_order",
    "ship_order",
    "order_goods_amount",
    "order_status_text",
    "order_logistics",
    # 阶段 4：结算 / 提现
    "generate_settlement",
    "mark_bill_settled",
    "list_bills",
    "apply_withdraw",
    "list_withdraws",
    "review_withdraw",
    "merchant_finance_summary",
    # 审核台：管理员 / 商家状态 / 退款审核
    "ensure_platform_admin",
    "set_merchant_status",
    "merchant_can_operate",
    "list_refund_candidates",
    "refund_order",
]
