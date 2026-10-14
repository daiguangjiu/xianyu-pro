"""Web UI 路由（Jinja2 模板）。

- 只读页面为主，复用 ORM 查询直接渲染（service 层只读函数优先；写操作不在此暴露）。
- 全部 `include_in_schema=False`，不污染 Swagger。
- 任何依赖项失败 → 页面降级为空列表 + 顶部 warning，不抛 500。

页面地图：
    /                     概览
    /harbor               渔货栈 · 货源广场（1688 风格两级类目 + 精选货源）
    /orders               订单（侧边栏子菜单按状态过滤）
    /goods                商品列表（?source=self_operated 自营商品列表）
    /goods/new            商品上架（顶栏「自营商品」菜单）
    /goods/detail/{id}    商品详情 + 闲鱼发布预填包（上架到店铺带入发布页）
    /inventory /pricing /oauth /logs                      供应链与系统页
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import func as sql_func, or_, select
from sqlalchemy.orm import Session

from src import __version__
from src.config import get_settings
from src.deps import get_db
from src.version import (
    APP_NAME,
    APP_TAGLINE,
    CHANGELOG,
)
from src.channel_xianyu.listing import (
    save_listing,
    source_text as listing_source_text,
    to_brief,
)
from src.channel_xianyu.publish import (
    build_publish_package,
    issue_handoff,
    xianmou_publish_url,
    xianyu_publish_url,
)
from src.goods.catalog import (
    AFTER_SALE_OPTIONS,
    DEFAULT_SPEC_TEMPLATE,
    FREIGHT_TEMPLATES,
    SHIPPING_CHOICES,
    SPEC_TEMPLATES,
    SOURCING_CATEGORY_FALLBACK,
    SOURCING_CATEGORY_FALLBACK_HINT,
    category_path,
    category_tree,
    hub_display_base,
    hub_freight_templates,
    hub_node_items,
    purchase_node_windows,
    shipping_mode_label,
    sourcing_leaves,
    sourcing_l3_owner,
    sourcing_leaf_index,
    sourcing_leaf_names_by_sub,
    sourcing_match,
    sourcing_sub_leaves,
    sourcing_tree,
    split_item_tags,
    supplier_display_name,
    tag_kind_of,
)
from src.goods.hub_price import resolve_cost
from src.goods.service import next_item_code
from src.merchant.router import _current_user
from src.models import (
    ChannelListing,
    DownstreamOrder,
    ExceptionLog,
    PriceRule,
    PurchaseOrder,
    Shop,
    ShopGoods,
    SkuMapping,
    StockOrder,
    SupplierItem,
)
from src.pricing import calculate_price
from src.pricing.engine import pick_rule
from src.order import flow as order_flow
from src.order import hub_sync, state as order_state
from src.self_purchase import (
    PAY_METHODS as PAY_METHODS_FOR_VIEW,
    TRADE_TYPES as TRADE_TYPES_FOR_VIEW,
    add_address,
    add_to_cart,
    add_to_pool,
    address_as_dict,
    cancel_order,
    cart_count,
    cart_summary,
    complete_order,
    confirm_pay,
    create_order,
    create_split_order,
    default_address,
    freight_of,
    get_address,
    get_items as get_stock_items,
    get_order as get_stock_order,
    group_lines,
    line_of,
    list_addresses,
    list_cart,
    list_orders as list_stock_orders,
    list_pool,
    mark_shipped,
    materials_zip,
    parse_address_text,
    pay_order,
    pool_summary,
    progress_of,
    remove_address,
    remove_from_pool,
    remove_lines,
    set_default,
    set_line_address,
    status_counts as stock_status_counts,
    status_text as stock_status_text,
    sub_orders as stock_sub_orders,
    to_buy_line,
    update_address,
    update_qty,
)
from src.web import hub_orders, hub_shops
from app import timeutil
from app.timeutil import TZ_COOKIE, UTC, beijing_now, to_iso, utc_now
from jinja2 import pass_context

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web"], include_in_schema=False)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
templates = Jinja2Templates(directory=str(_REPO_ROOT / "templates"))

# 已推 / 已付 / 已发 / 已完成 —— 计入利润与销量的 PO 状态
_SOLD_PO_STATUSES = ("pushed", "paid", "shipped", "completed")


# ---------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------
def _mask_db_url(url: str) -> str:
    return re.sub(r"://([^:]+):[^@]+@", r"://\1:****@", url)


def _mask_token(token: str | None) -> str:
    if not token:
        return ""
    if len(token) <= 12:
        return "****"
    return f"{token[:4]}****{token[-4:]}"


def _ts(dt: datetime | None) -> float:
    """统一成 epoch 秒，避免 naive / aware 混排报错。"""
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _user_tz(request) -> str | None:
    """页面时间用哪个时区：`?tz=` 优先，其次 cookie `tz`（浏览器时区），都没有就北京。

    只认 IANA 名字（Asia/Shanghai 这种），脏值由 timeutil 回落到北京。
    """
    try:
        tz = request.query_params.get("tz") if request is not None else None
        if not tz and request is not None:
            tz = request.cookies.get(TZ_COOKIE)
        return tz or None
    except Exception:  # noqa: BLE001  拿不到就按默认时区渲染
        return None


@pass_context
def _dt_filter(context, value, pattern: str = "%Y-%m-%d %H:%M"):
    """模板里渲染时间统一用它：``{{ order.created_at | dt }}`` / ``| dt('%m-%d %H:%M')``。

    库里存的是 UTC，这里按用户时区（cookie `tz`，默认北京）转成本地时间再格式化 ——
    模板里不要直接 `.strftime()`，否则显示的是 UTC（比北京早 8 小时）。
    """
    return timeutil.fmt(value, pattern, _user_tz(context.get("request")))


@pass_context
def _ts_filter(context, value) -> int:
    """UTC naive -> epoch 毫秒：给前端 `<time data-ts>` 用（浏览器再按本地时区显示）。"""
    dt = timeutil.to_local(value, _user_tz(context.get("request")))
    if dt is None:
        return 0
    return int(dt.replace(tzinfo=UTC).timestamp() * 1000)


templates.env.globals["version"] = __version__
templates.env.globals["app_name"] = APP_NAME
templates.env.globals["app_tagline"] = APP_TAGLINE
templates.env.filters["mask_token"] = _mask_token
templates.env.filters["dt"] = _dt_filter
templates.env.filters["ts"] = _ts_filter


# 货源广场链接构造器（暴露给模板）：让切榜单 / 切类目 / 切三级类目时
# 都能保留其它筛选条件（搜索词 / 节点 / 分页）。
def harbor_url(
    tab: str | None = None,
    cat: str | None = None,
    sub: str | None = None,
    l3: str | None = None,
    q: str | None = None,
    node: str | None = None,
    page: int | None = None,
) -> str:
    return _harbor_url(tab or "", cat, sub, q or "", page, l3=l3, node=node)


templates.env.globals["harbor_url"] = harbor_url


# ---------------------------------------------------------------------
# 状态中文化：页面徽章 / 图表 / 时间线统一走此映射（未知值原样回退）
# ---------------------------------------------------------------------
STATUS_TEXT: dict[str, str] = {
    # 下游订单（DSO）
    "pending": "待确认",
    "confirmed": "待推单",
    "pushed": "已推单",
    "shipped": "已发货",
    "completed": "已完成",
    "exception": "异常单",
    "refunded": "已退款",
    # 采购单（PO）
    "init": "待推单",
    "paid": "已付款",
    "failed": "失败",
    # 铺货记录（ChannelListing）
    # 2026-10-13 用户拍板：铺到自己店里 = 「已铺货」，不再叫「已上架」
    "published": "已铺货",
    "draft": "待发布",
    "needs_review": "待审核",
}


def status_text(s: str | None) -> str:
    """英文状态码 → 中文展示（与侧边栏订单子菜单命名一致）。"""
    return STATUS_TEXT.get(s or "", s or "—")


templates.env.globals["status_text"] = status_text
templates.env.globals["status_text_map"] = STATUS_TEXT
# 铺货记录来源（上架到店铺 / 仅存草稿 / 选品通过）中文化
templates.env.globals["listing_source_text"] = listing_source_text
# 采购单状态（待支付 / 待发货 / 已发货 / 已完成 / 已取消）中文化
templates.env.globals["stock_status_text"] = stock_status_text
templates.env.globals["pay_methods"] = PAY_METHODS_FOR_VIEW
# 交易方式（带教代收 / 在线支付，老订单的「担保交易」也认得）
templates.env.globals["trade_types"] = TRADE_TYPES_FOR_VIEW


# SSO 会话 cookie（闲参谋跳转 / 内嵌进来的学员）
_SSO_USER_COOKIE = "sso_user"


def _nav_counts(db: Session, request: Request | None = None) -> dict[str, int]:
    """侧边栏徽标：订单各状态 + 货源 / 上架 / 异常 / 采购车。"""
    dso = dict(
        db.execute(
            select(DownstreamOrder.status, sql_func.count(DownstreamOrder.id)).group_by(
                DownstreamOrder.status
            )
        ).all()
    )
    total_orders = sum(dso.values())

    # 商品：按来源 / 审核状态聚合（一次扫表，供侧边栏与商品管理页共用）
    by_source = dict(
        db.execute(
            select(SupplierItem.source_type, sql_func.count(SupplierItem.id)).group_by(
                SupplierItem.source_type
            )
        ).all()
    )
    by_audit = dict(
        db.execute(
            select(SupplierItem.audit_status, sql_func.count(SupplierItem.id)).group_by(
                SupplierItem.audit_status
            )
        ).all()
    )

    return {
        "orders": total_orders,
        "dso_total": total_orders,
        "pending": dso.get("pending", 0),
        "confirmed": dso.get("confirmed", 0),
        "pushed": dso.get("pushed", 0) + dso.get("shipped", 0),
        "completed": dso.get("completed", 0),
        "exception": dso.get("exception", 0),
        "refunded": dso.get("refunded", 0),
        "items": sum(by_source.values()),
        "items_pending": by_audit.get("pending", 0),
        "items_self": by_source.get("self_operated", 0),
        "items_third": by_source.get("third_party", 0),
        # 在架货源 = 上架到渔货栈的货源数（「我的」弹窗里那一个数）
        "harbor_listed": db.scalar(
            select(sql_func.count(SupplierItem.id)).where(SupplierItem.is_listed.is_(True))
        )
        or 0,
        "logs_unresolved": db.scalar(
            select(sql_func.count(ExceptionLog.id)).where(ExceptionLog.resolved.is_(False))
        ) or 0,
        **_purchase_counts(db, request),
    }


def _purchase_counts(db: Session, request: Request | None = None) -> dict[str, int]:
    """采购域徽标：采购车行数 / 采购单数 / 待支付数（各人看自己那份）。"""
    if request is None:
        return {"cart": 0, "stock_orders": 0, "stock_pending_pay": 0, "pool": 0}
    try:
        owner, _name = _purchase_owner(request)
        by_status = stock_status_counts(db, owner)
        pooled = pool_summary(db, owner).get("pooled", 0)
    except Exception:  # 表还没建好 / 桥断：徽标置零，别把页面拖挂
        return {"cart": 0, "stock_orders": 0, "stock_pending_pay": 0, "pool": 0}
    return {
        "cart": cart_count(db, owner),
        "stock_orders": sum(by_status.values()),
        "stock_pending_pay": by_status.get("pending_pay", 0),
        "pool": int(pooled or 0),
    }


def _menu_allowed(request: Request) -> set[str]:
    """自营端菜单：这个账号在渔枢「角色权限」里勾了哪几项（管理员 / 没挂角色 = 全部）。

    2026-10-11：三套子系统的菜单级权限收口到渔枢系统的「角色权限」页一块儿配。
    读不到（渔枢那边还没这套东西 / 桥断 / 没登录）就按「全部都能进」处理 ——
    权限系统出问题不该把整站菜单弄没。
    """
    from ..nav import ALL_KEYS  # noqa: PLC0415  延迟导入：nav 不依赖 router，反过来会绕
    try:
        from app.web.permissions import perms_of_user  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return set(ALL_KEYS)
    try:
        uid = getattr(hub_shops.viewer(request), "id", None)
        if not uid:
            return set(ALL_KEYS)
        return set(perms_of_user(int(uid), "self"))
    except Exception:  # noqa: BLE001
        return set(ALL_KEYS)


def _guide_of(request: Request, db: Session | None) -> dict | None:
    """新手指引的进度（外部学员才有，见 self_purchase/guide.py）。

    读不到一律 None（弹窗不渲染）—— 引导出问题不该挡住人用系统。
    """
    if db is None:
        return None
    try:
        from ..self_purchase.guide import guide_of  # noqa: PLC0415

        uid = getattr(hub_shops.viewer(request), "id", None)
        if not uid:
            return None
        return guide_of(db, int(uid))
    except Exception as exc:  # noqa: BLE001
        logger.debug("取新手指引失败：%s", exc)
        return None


def _ensure_shop_synced(hub) -> None:
    """触发一次店铺同步（幂等 + 自带节流，见 `web/shop_sync.py`）。

    要在**查列表之前**调：自动带过来的店铺得这一屏就看到，等到渲染时才同步，
    页面上显示的还是同步前那份（用户得手动刷新一次才看得到）。
    """
    if not getattr(hub, "id", None):
        return
    try:
        from .shop_sync import ensure_synced  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.debug("店铺自动同步不可用：%s", exc)
        return
    ensure_synced(hub)


def _shop_sync_of(request: Request) -> dict[str, Any]:
    """店铺自动同步：第一次进渔货栈就把他名下的渔枢店铺带过来，之后按节流增量补。

    见 `web/shop_sync.py` —— 只记关联台账，不复制店铺数据（店铺档案只有渔枢
    系统库里那一份）。异常一律吞掉：少带几家店不该把页面打挂。
    """
    hub = hub_shops.viewer(request)
    _ensure_shop_synced(hub)
    uid = getattr(hub, "id", None)
    if not uid:
        return {}
    try:
        from .shop_sync import mark_of  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.debug("店铺同步状态不可用：%s", exc)
        return {}
    return mark_of(int(uid))


def _common_ctx(
    request: Request,
    active_nav: str,
    *,
    db: Session | None = None,
    active_sub: str | None = None,
    nav_crumb: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    settings = get_settings()
    base = {
        "request": request,
        "active_nav": active_nav,
        "active_sub": active_sub,
        "nav_crumb": nav_crumb,
        "nav_counts": _nav_counts(db, request) if db is not None else None,
        "now": utc_now(),
        "env": settings.app_env,
        "db_url_masked": _mask_db_url(settings.database_url),
        "sso_user": request.cookies.get(_SSO_USER_COOKIE) or "",
        "current_user": _current_user(request, db) if db is not None else None,
        # 顶栏采购车悬停弹框（照 1688）：最近加入的货品 / 降价提示 / 剩余货品数
        "cart_preview": _cart_preview(db, request) if db is not None else None,
        # 菜单级权限（渔枢「角色权限」里勾的渔货栈那几项）：模板按它决定显示哪些入口
        "menu_allowed": _menu_allowed(request),
        # 新手指引：只有外部学员给（走完 4 步就不再给），内部人员一直是 None
        "guide": _guide_of(request, db),
        # 店铺自动同步（首登把渔枢系统里属于他的店铺带过来，见 web/shop_sync.py）
        "shop_sync": _shop_sync_of(request),
    }
    # 顶栏右上角：用户名 / 身份 / 关联店铺数（店铺档案在渔枢系统，见 web/hub_shops.py）
    base["viewer"] = hub_shops.viewer_summary(request, base["current_user"])
    base.update(extra)
    return base


def self_url(path: str) -> str:
    """站内页面链接：统一补上渔货栈前缀（页面挂在 /self 下，见 config.self_prefix）。

    渔货栈和渔枢系统跑在同一个端口上，**没前缀的 `/orders` 是渔枢系统的订单页**。
    侧边栏菜单、列表与详情页里的站内链接一律走这个函数，别再手写裸路径 ——
    少写一个 `/self`，点进去就是另一端的页面。
    """
    prefix = get_settings().self_prefix
    if prefix and path.startswith("/") and not path.startswith(prefix):
        return f"{prefix}{path}"
    return path


templates.env.globals["self_url"] = self_url


@pass_context
def menu_can(ctx, key: str) -> bool:
    """模板里判断「这个菜单能不能显示」（见 _menu_allowed）。

    页面没传 menu_allowed 时一律放行 —— 权限系统没跑到，不该把菜单整片弄没。
    """
    allowed = ctx.get("menu_allowed")
    if not allowed:
        return True
    return str(key) in allowed


templates.env.globals["menu_can"] = menu_can


def _redirect(path: str, msg: str = "", kind: str = "ok") -> RedirectResponse:
    """站内跳转：统一带上渔货栈前缀（页面挂在 /self 下），可选带一条提示。

    `msg` / `kind` 会挂在查询串上，页面自己渲染提示（渔货栈没有全局 flash）。
    """
    path = self_url(path)
    if msg:
        joiner = "&" if "?" in path else "?"
        path = f"{path}{joiner}{urlencode({'msg': msg, 'kind': kind})}"
    return RedirectResponse(path, status_code=303)


# ---------------------------------------------------------------------
# 渔货栈：货源广场类目（1688 风格两级类目，见 goods/catalog.SOURCING_CATEGORY_TREE）
# ---------------------------------------------------------------------
def _harbor_category(item, cat_paths: dict[str, str] | None = None) -> tuple[str, str]:
    """把货源挂到 (一级类目, 二级类目)；未命中归入「其他」。

    归类优先级（2026-10-02 起）：
      1. 商品自己带类目 id —— 渔枢系统「上架到渔货栈」推过来的商品都带（落到对应类目），
         渔货栈录入的商品也带，按它认最准；
      2. 没类目 id 的（1688 抓来的货源大多没有）回落到按标题关键词匹配。
    """
    category_id = (getattr(item, "category_id", "") or "").strip()
    if category_id:
        paths = cat_paths if cat_paths is not None else category_path()
        path = paths.get(category_id) or ""
        if " / " in path:
            top, sub = path.split(" / ", 1)
            return top.strip(), sub.strip()
    return sourcing_match(getattr(item, "title", "") or "")


def _product_key(title: str | None, selling_points: str | None) -> str:
    """同款商品键：标题去掉卖点后缀（如「厂家直销 支持一件代发」）。

    用于统计「上架商家数」——同一款商品由多少个供货商在架。
    """
    text = (title or "").strip()
    sp = (selling_points or "").strip()
    if sp and text.endswith(sp):
        stripped = text[: -len(sp)].strip(" ·-|")
        if stripped:
            return stripped
    return text


def _resolve_cat(value: str | None) -> str | None:
    """`cat` 参数归一化：支持一级类目名 / id / 副标签(如「数码电脑」)。"""
    if not value or value in ("全部", "all"):
        return None
    for node in sourcing_tree():
        if value in (node["name"], node["id"], node["hint"]):
            return node["name"]
    # 传了二级类目名 → 归到它所属的一级类目
    for leaf in sourcing_leaf_index():
        if value == leaf["name"]:
            return leaf["parent_name"]
    return None


def _resolve_sub(value: str | None) -> str | None:
    """`sub` 参数归一化：二级类目名。"""
    if not value or value in ("全部", "all"):
        return None
    names = {leaf["name"] for leaf in sourcing_leaf_index()}
    return value if value in names else None


def _harbor_url(
    tab: str,
    cat: str | None,
    sub: str | None,
    keyword: str,
    page: int | None = None,
    *,
    l3: str | None = None,
    node: str | None = None,
) -> str:
    """拼货源广场链接，保留当前榜单 / 类目 / 三级类目 / 采购节点 / 搜索条件。

    路径走 `self_url` 补渔货栈前缀：合并部署时渔货栈页面挂在 `/self` 下，
    裸 `/harbor` 没挂载（点了类目 / 榜单 / 翻页就是 404）。
    """
    params: list[tuple[str, str]] = []
    # 默认是「最新上架」（tab=new），非默认才写进 URL，链接更好看也更好分享
    if tab and tab != "new":
        params.append(("tab", tab))
    if cat and cat != "全部":
        params.append(("cat", cat))
    if sub and sub != "全部":
        params.append(("sub", sub))
    if l3:
        params.append(("l3", l3))
    if node:
        params.append(("node", node))
    if keyword:
        params.append(("q", keyword))
    if page and page > 1:
        params.append(("page", str(page)))
    path = f"/harbor?{urlencode(params)}" if params else "/harbor"
    return self_url(path)


def _page_window(page_no: int, page_total: int, width: int = 7) -> list[int]:
    """页码窗口：总页数超出 width 时以当前页为中心滑动，避免页码溢出。"""
    if page_total <= width:
        return list(range(1, page_total + 1))
    start = max(1, min(page_no - width // 2, page_total - width + 1))
    return list(range(start, start + width))


def _profit_expr():
    """利润表达式（2026-09-29 起全站统一口径）：毛利 = (售价 − 成本) × 件数。

    成本优先取商品的「渔货价」（渔货栈对外成本价），没填渔货价才回落到 PO 采购价。
    用它的地方记得 outerjoin 上 SupplierItem（按 item_id 对商品）。
    """
    unit_cost = sql_func.coalesce(SupplierItem.fish_price, PurchaseOrder.cost_price)
    return (DownstreamOrder.sale_price - unit_cost) * PurchaseOrder.qty


# =====================================================================
# 1. 首页 / → 货源广场；概览挪到 /overview
# =====================================================================
@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def home(request: Request) -> RedirectResponse:
    """渔货栈首页 = 货源广场（2026-10-03 用户拍板：整个渔货栈以 /self/harbor 为首页）。

    这里只做跳转：登录后的落点、端切换、老书签、顶栏品牌 logo 都还打在 `/self/` 上，
    不重定向的话首页就仍旧是概览页。概览本身挪到 `/overview`（菜单「工作台 · 概览」）。

    查询串要带着走：退出登录 / 动作结果都是 `/self/?msg=…` 这种地址，丢了就没有提示条。
    """
    target = self_url("/harbor")
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(target, status_code=307)


@router.api_route("/overview", methods=["GET", "HEAD"], response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    now_utc = utc_now()
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    today_orders = db.scalar(
        select(sql_func.count(DownstreamOrder.id)).where(
            DownstreamOrder.created_at >= today_start
        )
    ) or 0
    pending_push = db.scalar(
        select(sql_func.count(DownstreamOrder.id)).where(
            DownstreamOrder.status.in_(("pending", "confirmed"))
        )
    ) or 0
    shipped_po = db.scalar(
        select(sql_func.count(PurchaseOrder.id)).where(
            PurchaseOrder.status == "shipped"
        )
    ) or 0
    profit_today = db.scalar(
        select(sql_func.coalesce(sql_func.sum(_profit_expr()), 0))
        .select_from(DownstreamOrder)
        .join(
            PurchaseOrder,
            PurchaseOrder.downstream_order_id == DownstreamOrder.downstream_order_id,
        )
        .outerjoin(SupplierItem, SupplierItem.item_id == PurchaseOrder.ali_item_id)
        .where(DownstreamOrder.created_at >= today_start)
        .where(PurchaseOrder.status.in_(("pushed", "paid", "shipped", "completed")))
    ) or 0

    dso_status = [
        {"status": s, "count": c}
        for s, c in db.execute(
            select(DownstreamOrder.status, sql_func.count(DownstreamOrder.id))
            .group_by(DownstreamOrder.status)
            .order_by(DownstreamOrder.status)
        ).all()
    ]
    po_status = [
        {"status": s, "count": c}
        for s, c in db.execute(
            select(PurchaseOrder.status, sql_func.count(PurchaseOrder.id))
            .group_by(PurchaseOrder.status)
            .order_by(PurchaseOrder.status)
        ).all()
    ]
    counts = {
        "shops": db.scalar(select(sql_func.count(Shop.id))) or 0,
        "items": db.scalar(select(sql_func.count(SupplierItem.id))) or 0,
        "mappings": db.scalar(select(sql_func.count(SkuMapping.id))) or 0,
        # 铺货记录按人隔离后（2026-10-13）一条记录 = 一个账号的一款货；
        # 平台级记录（owner 为空，选品跑出来的）不算任何人的铺货
        "listings": db.scalar(
            select(sql_func.count(ChannelListing.id)).where(
                ChannelListing.status == "published",
                ChannelListing.owner != "",
            )
        ) or 0,
    }

    ctx = _common_ctx(
        request, "dashboard", db=db,
        nav_crumb=["工作台", "概览"],
        kpi={
            "today_orders": today_orders,
            "pending_push": pending_push,
            "shipped_po": shipped_po,
            "profit_today": float(profit_today),
        },
        dso_status=dso_status,
        po_status=po_status,
        counts=counts,
    )
    return templates.TemplateResponse("dashboard.html", ctx)


# =====================================================================
# 2. 渔货栈 · 货源广场 /harbor
# =====================================================================
# 单次扫描的货源上限（价格 / 标签 / 类目均在内存中派生，无法下推到 SQL）
HARBOR_SCAN_LIMIT = 5000
# feed 流每页卡片数（1688 版式：滚动分区加载）
HARBOR_PAGE_SIZE = 48
# 悬浮面板里每个二级类目「默认摆」几个三级类目（其余折叠，点「展开全部 N 个」再看）
HARBOR_LEAF_LIMIT = 12
# 趋势飙升榜 / 采购节点一屏最多摆几款（类目栏展开后整行拉高，多出来的位置就给它们）
HARBOR_TREND_LIMIT = 8
HARBOR_NODE_LIMIT = 7
# 服务端最多带多少个三级：1688 那套三级一共一万多个，但只给「当前展开的那个一级」发
# （最多的一级「五金、工具」954 个），全带也就一百来 KB；再兜个上限防极端数据
HARBOR_LEAF_MAX = 1000
# 左侧「货源类目」栏默认摆几个一级类目，其余收起（末尾「更多类目」点开看全部）。
# 类目栏是两列网格：10 个类目 + 第 1 格「全部货源」= 11 格，加上「更多类目」正好 6 行
HARBOR_RAIL_VISIBLE = 10


def _leaf_chips(
    names, leaf_counts: dict, sub_name: str, l3_filter: str = "", limit: int = HARBOR_LEAF_MAX
) -> list[dict]:
    """二级类目下的三级类目 chip：有货源的排前面，最多 `limit` 个。

    三级类目是「渔枢系统类目管理」里那棵树的一部分，只摆前 12 个会把「男式休闲皮鞋」
    这种没货源的三级整个藏掉，看着就像该类目没有三级；所以这里按上限一次给全，
    由前端折叠成默认 12 个 + 「展开全部 N 个」，点开就能看到完整清单。

    当前选中的三级无条件保留（哪怕它没货源），否则点进去看不出自己选了哪个。
    """
    items = [
        {
            "name": name,
            "count": int(leaf_counts.get((sub_name, name), 0) or 0),
            "active": bool(l3_filter) and l3_filter == name,
        }
        for name in names
        if name
    ]
    items.sort(key=lambda x: (-x["count"], x["name"]))
    picked = items[:limit]
    if l3_filter and not any(x["active"] for x in picked):
        hit = next((x for x in items if x["name"] == l3_filter), None)
        if hit is not None:
            picked = [hit] + picked[: limit - 1]
    return picked


@router.get("/harbor", response_class=HTMLResponse)
def harbor(
    request: Request,
    q: str | None = None,
    cat: str | None = None,
    sub: str | None = None,
    l3: str | None = None,
    node: str | None = None,
    # 默认「最新上架」（tab=new）：2026-10-04 起顶部那块独立的「最新上架的货源」取消，
    # 新货直接并进 feed —— 进广场第一眼就是刚上架的，不用翻页也不用猜排序
    tab: str = "new",
    page: int = 1,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """对外展示的「渔货栈」货源广场（版式对齐 1688 货源市场）。

    结构：顶部四块（三级类目栏 / 趋势飙升榜 / 采购节点 / 我的经营卡）
        → 推荐类目横排条 → 精选货源（二级类目 Tab + 榜单排序）
        → 货源卡 feed 流（分页，每页 HARBOR_PAGE_SIZE 款；默认按最新上架倒序）。
    `l3` 为三级类目（类目栏悬浮面板选到再下级），`node` 为选中的采购节点
    （切换节点 → 卡片下方主推货源联动）。
    学员 / 商家在此浏览货源，看「拿货价 + 预估利润率 + 销量」，
    一键「上架到店铺」→ 前端调 `POST /api/v1/selection/run` 生成铺货草稿，
    再由商品详情页把字段带入闲鱼商品发布页。
    """
    items = db.execute(
        # 已经在渔枢系统那边下架的货源不再出现在广场上（只有 8 款在架时 47 款下架货
        # 会把列表塞满、物料也白白算一遍）。下架快照仍可在商品详情页看（那边按
        # item_id 单独取，不走这里）。
        select(SupplierItem)
        .where(SupplierItem.is_listed.is_not(False))
        .order_by(SupplierItem.last_synced_at.desc().nullslast())
        .limit(HARBOR_SCAN_LIMIT)
    ).scalars().all()

    # 生效规则一次性取出并按 priority 排好，避免逐条 item 查库（N+1）
    rules = db.execute(
        select(PriceRule)
        .where(PriceRule.is_active.is_(True))
        .order_by(PriceRule.priority)
    ).scalars().all()

    def _match_rule(category_id: str | None, supplier_id: str | None) -> PriceRule | None:
        for scope, key in (
            ("channel", "xianyu"),
            ("category", category_id),
            ("supplier", supplier_id),
            ("global", None),
        ):
            for r in rules:
                if r.scope == scope and (r.scope_key == key or scope == "global"):
                    return r
        return None

    item_ids = [i.item_id for i in items]

    # 渠道 SKU（每个 item 取第一条映射）
    mapping_by_item: dict[str, SkuMapping] = {}
    if item_ids:
        for m in db.execute(
            select(SkuMapping)
            .where(SkuMapping.item_id.in_(item_ids))
            .order_by(SkuMapping.id)
        ).scalars().all():
            mapping_by_item.setdefault(m.item_id, m)

    # 铺货记录（每个 item 取第一条）—— 只看**当前账号**名下那条。
    # 2026-10-13 起按账号隔离：铺货记录带 owner，以前全站共用一条，
    # 于是「A 账号入了池 → 包括学员在内的所有账号都看到已上架」。
    listing_owner, _listing_owner_name = _purchase_owner(request)
    listing_by_item: dict[str, ChannelListing] = {}
    if item_ids:
        for li in db.execute(
            select(ChannelListing)
            .where(ChannelListing.item_id.in_(item_ids))
            .where(ChannelListing.owner == listing_owner)
            .order_by(ChannelListing.id)
        ).scalars().all():
            listing_by_item.setdefault(li.item_id, li)

    # 销量：按货源聚合已成交 PO 件数
    sold_by_item: dict[str, int] = {}
    if item_ids:
        sold_by_item = {
            k: int(v or 0)
            for k, v in db.execute(
                select(
                    PurchaseOrder.ali_item_id,
                    sql_func.coalesce(sql_func.sum(PurchaseOrder.qty), 0),
                )
                .where(PurchaseOrder.ali_item_id.in_(item_ids))
                .where(PurchaseOrder.status.in_(_SOLD_PO_STATUSES))
                .group_by(PurchaseOrder.ali_item_id)
            ).all()
        }

    now_utc = utc_now()
    fresh_cutoff = now_utc - timedelta(hours=24)

    # 供应商货源数：≥3 款视为「源头工厂」（1688 同款标签语义），卡片循环内即需用到
    sup_counter: dict[str, int] = {}
    for it in items:
        if it.supplier_id:
            sup_counter[it.supplier_id] = sup_counter.get(it.supplier_id, 0) + 1

    # 「X 家上架」的真实值（2026-10-13 用户拍板）：有多少个账号把这款货铺进自己店铺。
    # 铺货记录按账号去重（一人一条），平台级记录（owner 为空，选品跑的）不算。
    # 以前这个数取的是「同款商品有多少供货商在架」，与「谁铺过」没关系，所以永远显示 1。
    listed_shop_by_item: dict[str, int] = {}
    if item_ids:
        listed_shop_by_item = {
            k: int(v or 0)
            for k, v in db.execute(
                select(
                    ChannelListing.item_id,
                    sql_func.count(sql_func.distinct(ChannelListing.owner)),
                )
                .where(ChannelListing.item_id.in_(item_ids))
                .where(ChannelListing.status == "published")
                .where(ChannelListing.owner != "")
                .group_by(ChannelListing.item_id)
            ).all()
        }

    # 展示基数：渔枢系统「货源 · 展示基数」页配的，渔货栈只把它加在**卡片展示**上
    # （真实值照旧另算：排序 / 热销榜 / 详情页「在售闲鱼号」一个都不加）
    _display_base = hub_display_base()
    listed_shops_base = int(_display_base.get("listed_shops_base") or 0)
    sold_base = int(_display_base.get("sold_base") or 0)

    # 类目路径表（二级 id → 一级 / 二级）：整页算一次，卡片归类直接用
    cat_paths = category_path()

    cards: list[dict[str, Any]] = []
    for it in items:
        # 成本口径（2026-09-29 起全站统一）：渔货价 = 自营的成本价，没填渔货价才回落采购价
        cost = Decimal(str(it.fish_price if it.fish_price is not None else (it.cost_price or 0)))
        rule = _match_rule(it.category_id, it.supplier_id)
        # 建议售价走唯一口径（商品档案的建议售价优先，没填才按加价率算）：
        # 卡片的「高毛利 x%」是按它和渔货价现算的，口径不统一会算出假毛利
        price_dec = _suggested_price_of(db, it)
        price = float(price_dec) if price_dec and float(price_dec) > 0 else float(calculate_price(it, rule) or 0)
        rate = float((price - float(cost)) / price) if price and price > 0 else 0.0
        prev_cost = (
            Decimal(str(it.prev_cost_price)) if it.prev_cost_price is not None else None
        )
        cut_amount = float(prev_cost - cost) if prev_cost is not None and prev_cost > cost else 0.0
        listing = listing_by_item.get(it.item_id)
        mapping = mapping_by_item.get(it.item_id)

        cat1, cat2 = _harbor_category(it, cat_paths)
        key = _product_key(it.title, it.selling_points)
        is_new = bool(it.last_synced_at and _ts(it.last_synced_at) >= _ts(fresh_cutoff))
        is_self = (it.source_type or "") == "self_operated"
        # 货源标签 = 固定标签（渔枢系统发布商品时勾选的，人工认定的事实）
        #          + 动态标签（按这件货的真实数据当场算，不落库）
        # 没勾过固定标签的（1688 货源没有「发布勾选」这一步）按供货商款数 / 库存兜底派生
        fixed_tags: list[dict[str, str]] = [
            {"text": name, "kind": tag_kind_of(name), "fixed": True}
            for name in split_item_tags(it.tags)
        ]
        if not fixed_tags and sup_counter.get(it.supplier_id, 0) >= 3:
            fixed_tags.append({"text": "源头工厂", "kind": tag_kind_of("源头工厂"), "fixed": False})
        tags: list[dict[str, str]] = list(fixed_tags)
        if cut_amount > 0:
            tags.append({"text": f"降价{cut_amount:g}元", "kind": "danger"})
        if rate >= 0.35:
            tags.append({"text": f"高毛利 {rate * 100:.0f}%", "kind": "success"})
        if is_new:
            tags.append({"text": "今日上新", "kind": "info"})
        if it.is_oversold:
            tags.append({"text": "库存预警", "kind": "warn"})
        elif not fixed_tags and (it.stock or 0) >= 100:
            # 没勾「现货速发」这类固定标签时的兜底：库存够多就是能马上发
            tags.append({"text": "现货速发", "kind": "success"})
        if sold_by_item.get(it.item_id, 0) > 0:
            tags.append({"text": "已开单", "kind": "info"})

        # 1688 feed 卡：图内促销贴片 + 标题前缀标签（均由真实数据派生）
        if cut_amount > 0:
            promo = f"直降 {cut_amount:g} 元"
        elif is_new:
            promo = "今日上新 · 抢鲜"
        elif rate >= 0.35:
            promo = f"高毛利 {rate * 100:.0f}%"
        elif (it.stock or 0) >= 200:
            promo = f"现货 {it.stock} 件"
        else:
            promo = ""
        # 标题前缀：自营 / 首个固定标签（源头工厂…）/ 兜底的首个动态标签 / 二级类目
        if is_self:
            prefix = "自营"
        elif fixed_tags:
            prefix = fixed_tags[0]["text"]
        elif tags:
            prefix = tags[0]["text"]
        else:
            prefix = cat2

        shop_name = "渔货栈自营" if is_self else supplier_display_name(it.supplier_id, cat2)
        # 真实值：铺过这款货的账号数（展示时会加上渔枢后台配的基数）
        merchant_count = listed_shop_by_item.get(it.item_id, 0)

        cards.append({
            "item_id": it.item_id,
            "title": it.title,
            "main_image": it.main_image,
            "cost_price": float(cost),
            "suggested_price": float(price),
            "profit_rate": rate,
            "stock": it.stock or 0,
            # 真实销量：排序、热销榜计数、统计都用它（不含展示基数）
            "sold": sold_by_item.get(it.item_id, 0),
            # 卡片上给人看的「已售 N」= 展示基数 + 真实销量
            "sold_shown": sold_by_item.get(it.item_id, 0) + sold_base,
            "return_rate": float(it.return_rate or 0),
            "is_oversold": bool(it.is_oversold),
            "is_self": is_self,
            "cat1": cat1,
            "cat2": cat2,
            # 三级类目：渔枢系统上架过来的货带着这一列（按商品类目 + 标题联想认的），
            # 没有的就回落「三级名出现在标题里」那个老口径（见下面过滤与计数）
            "l3": (getattr(it, "category_l3", "") or "").strip(),
            "tags": tags,
            "promo": promo,
            "prefix": prefix,
            "company": shop_name,
            "shop_name": shop_name,
            # 真实值：铺过这款货的店铺数（详情页「在售闲鱼号」等统计口径用它）
            "merchants": merchant_count,
            # 卡片上给人看的「X 家上架」= 展示基数 + 真实店铺数
            "merchants_shown": merchant_count + listed_shops_base,
            "supplier_id": it.supplier_id,
            "delivery_days": int(it.delivery_days or 2),
            "after_sale": it.after_sale or "",
            "channel_sku_id": mapping.channel_sku_id if mapping else None,
            "listing_status": listing.status if listing else None,
            "listing_id": listing.id if listing else None,
            # 平台下架（渔枢系统把商品从渔货栈下架）：渔货栈在卡片与详情页看到提示
            "unlisted": bool(getattr(it, "unlisted_at", None) and not it.is_listed),
            "unlisted_at": getattr(it, "unlisted_at", None),
            "unlisted_reason": getattr(it, "unlisted_reason", "") or "",
            "unlisted_by": getattr(it, "unlisted_by", "") or "",
            "last_synced_at": it.last_synced_at,
            "is_new": is_new,
            "cut_amount": cut_amount,
            # 搜索用：品牌 / 商家编码（渔枢系统上架过来的商品这几项都有）
            "brand": it.brand or "",
            "item_code": it.item_code or "",
            "category_id": it.category_id or "",
        })

    # —— 采购节点 / 活动页（节点日历 + 关键词实时匹配的节点货源）——
    # 节点关键词就是这个活动的「搜索条件」：点了某个活动，下面的 feed 直接按它出结果
    # （与手输的关键词 / 类目 / 榜单是「与」的关系，全部一起生效）。
    nodes: list[dict[str, Any]] = []
    # 运营手动配置的商品（2026-10-11）：按节点标识取，在这个活动里**排最前**（当置顶用）；
    # 关键词匹配那套系统规则一点没变，两组去重后一起展示。
    node_manual = hub_node_items()
    card_by_id = {c["item_id"]: c for c in cards}
    for _node in purchase_node_windows(beijing_now().date())[:3]:
        manual = [card_by_id[i] for i in node_manual.get(_node["id"], []) if i in card_by_id]
        manual_set = {c["item_id"] for c in manual}
        matched = len(cards)
        pool = cards
        hit_ids: set[str] = set()
        if _node["keywords"]:
            hit = [c for c in cards if any(k in (c["title"] or "") for k in _node["keywords"])]
            if hit:
                matched = len(hit)
                # 命中款数不足 3 时按热度补齐，保证节点主推位有内容
                hit_ids = {c["item_id"] for c in hit}
                pool = hit + [c for c in cards if c["item_id"] not in hit_ids]
        # 手动配置的按运营排的顺序在最前，其余（关键词命中 / 补齐的）照旧按热度排
        rest = [c for c in pool if c["item_id"] not in manual_set]
        rest.sort(key=lambda c: (c["sold"], c["profit_rate"]), reverse=True)
        nodes.append({
            **_node,
            # 手动配的、关键词没命中的那些也算这个活动的货，一起报数
            "count": matched + len(manual_set - hit_ids),
            # 节点下命中多少款就摆多少（主推 1 + 小卡 6）：节点板块高度跟着内容走
            "cards": (manual + rest)[:HARBOR_NODE_LIMIT],
        })
    # 只有「点了某个活动」才过滤 feed：默认进页面不替用户选活动
    node_param_id = (node or "").strip()
    node_filter = next((n for n in nodes if n["id"] == node_param_id), None) if node_param_id else None
    node_id = node_filter["id"] if node_filter else None  # 分页 / 类目链接要用

    # —— 搜索（商品名 / 商品ID / 供货商 / 品牌 / 商家编码 / 类目）——
    # 多店铺几个字段一起匹配：渔枢系统上架过来的商品带品牌与商家编码，
    # 按编码搜（对账、跟供应商沟通时最常用）也得搜得到，否则「搜不到」看起来像没上架。
    keyword = (q or "").strip().lower()
    filtered = cards
    if keyword:
        fields = ("title", "item_id", "supplier_id", "brand", "item_code", "cat1", "cat2")
        filtered = [
            c
            for c in cards
            if any(keyword in str(c.get(f) or "").lower() for f in fields)
        ]
    # —— 活动节点（选了活动页就把它的关键词当搜索条件带进来）——
    node_keywords: list[str] = []
    node_manual_ids: set[str] = set()
    if node_filter:
        # 手动配置的商品也算这个活动的货：标题没命中关键词也要在活动页里看得见
        node_manual_ids = set(node_manual.get(node_filter["id"], []))
        if node_filter["keywords"]:
            node_keywords = [k for k in node_filter["keywords"]]
            filtered = [
                c for c in filtered
                if any(k in (c["title"] or "") for k in node_keywords)
                or c["item_id"] in node_manual_ids
            ]

    # —— 两级类目统计（基于关键词过滤后的集合，保证类目数字与列表一致）——
    cat_counts: dict[str, int] = {}
    sub_counts_all: dict[str, int] = {}
    sub_parent: dict[str, str] = {}
    leaf_counts: dict[tuple[str, str], int] = {}
    l3_names_by_sub = sourcing_leaf_names_by_sub()
    for c in filtered:
        cat_counts[c["cat1"]] = cat_counts.get(c["cat1"], 0) + 1
        sub_counts_all[c["cat2"]] = sub_counts_all.get(c["cat2"], 0) + 1
        sub_parent[c["cat2"]] = c["cat1"]
        _title = c["title"] or ""
        _l3 = c["l3"]
        if _l3:
            # 渔枢系统上架时已经认过三级：直接计到那个三级上（标题里没写也数得到）
            leaf_counts[(c["cat2"], _l3)] = leaf_counts.get((c["cat2"], _l3), 0) + 1
        for _n in l3_names_by_sub.get(c["cat2"], ()):
            if _n != _l3 and _n in _title:
                leaf_counts[(c["cat2"], _n)] = leaf_counts.get((c["cat2"], _n), 0) + 1
    cat_options = sorted(cat_counts.items(), key=lambda kv: (-kv[1], kv[0]))

    cat_filter = _resolve_cat(cat)
    sub_filter = _resolve_sub(sub)
    l3_filter = (l3 or "").strip() or None
    _l3_owner = sourcing_l3_owner(l3_filter) if l3_filter else None
    if _l3_owner is None:
        l3_filter = None
    else:  # 三级类目自带归属，避免 URL 上 cat / sub 与 l3 打架
        cat_filter, sub_filter = _l3_owner
    if cat_filter:
        filtered = [c for c in filtered if c["cat1"] == cat_filter]
    # 选了某个一级类目后，二级类目面板 / Tab 的货源数收敛到该类目内
    sub_counts: dict[str, int] = {}
    if cat_filter:
        for c in filtered:
            sub_counts[c["cat2"]] = sub_counts.get(c["cat2"], 0) + 1
    else:
        sub_counts = dict(sub_counts_all)
    if sub_filter:
        filtered = [c for c in filtered if c["cat2"] == sub_filter]
    if l3_filter:
        filtered = [
            c
            for c in filtered
            # 先认商品自己带的三级类目（渔枢系统上架时认的），认不到才回落标题匹配
            if c["l3"] == l3_filter or (not c["l3"] and l3_filter in (c["title"] or ""))
        ]

    # —— 左侧类目栏（1688 版式：一级类目 + 数字，悬浮展开二级类目）——
    # 三级类目一万多个，页面只摆得下每级十来个：先按「有货源」排，剩下的给「更多」入口。
    # 这份映射一次取全（sourcing_leaves 内部会重建整棵树，逐个二级去取会变成 O(n²)）
    leaves_by_sub = sourcing_leaves()
    cat_rail: list[dict[str, Any]] = []
    for node in sourcing_tree():
        children = [
            {
                "id": child["id"],
                "name": child["name"],
                "icon": child["icon"],
                "accent": node["accent"],
                "count": sub_counts_all.get(child["name"], 0),
                "active": sub_filter == child["name"],
                "idx": _i,
                # 三级留给「当前展开面板」那一屏再填（见下面 panel_name），
                # 53 个一级类目各摆上千个三级会把页面撑到好几 MB
                "leaves": [],
                "leaf_total": len(leaves_by_sub.get(child["id"], ())),
            }
            for _i, child in enumerate(node["children"], start=1)
        ]
        cat_rail.append({
            "id": node["id"],
            "name": node["name"],
            "hint": node["hint"],
            "icon": node["icon"],
            "accent": node["accent"],
            "count": cat_counts.get(node["name"], 0),
            "active": cat_filter == node["name"],
            "open_idx": next((ch["idx"] for ch in children if ch["active"]), 1),
            "children": children,
        })
    # 「其他」收底：认不出类目的货源总得有个地方去（用户 2026-10-04 拍板）。
    # 类目表里已经有一级「其他」就用它，没有才补一个，别列两遍
    if not any(n["name"] == SOURCING_CATEGORY_FALLBACK for n in cat_rail):
        cat_rail.append({
            "id": "other",
            "name": SOURCING_CATEGORY_FALLBACK,
            "hint": "未归入类目",
            "icon": "bi-three-dots",
            "accent": "slate",
            "count": cat_counts.get(SOURCING_CATEGORY_FALLBACK, 0),
            "active": cat_filter == SOURCING_CATEGORY_FALLBACK,
            "children": [],
        })
    # 兜底项永远排在最后：它不该抢在真类目前面
    cat_rail.sort(key=lambda n: 1 if n["name"] == SOURCING_CATEGORY_FALLBACK else 0)

    # —— 推荐类目面板（1688 版式：二级类目图标磁贴）——
    panel_node = next((n for n in cat_rail if n["active"]), None)
    if panel_node is None and keyword:
        # 搜索态：把命中的二级类目聚合成「推荐类目」，避免面板空转
        panel_node = next(
            (n for n in cat_rail if any(ch["count"] for ch in n["children"])), None
        )
    # 选中「其他」时：它下面有二级就像正常类目那样显示面板，没有（纯兜底）才跳过
    if panel_node and panel_node["name"] == SOURCING_CATEGORY_FALLBACK and not panel_node["children"]:
        panel_node = None
    # 三级类目只填给「这一屏要展开的那个一级类目」：悬浮面板一次只显示一个，
    # 其余一级仍带二级名与「全部 N 款」入口，点进去自然能看到三级
    if panel_node is None:
        panel_node = next((n for n in cat_rail if n["count"]), None)
    if panel_node is not None:
        for child in panel_node["children"]:
            child["leaves"] = _leaf_chips(
                leaves_by_sub.get(child["id"], ()), leaf_counts, child["name"], l3_filter
            )
    panel_cats = panel_node["children"] if panel_node else []
    if not panel_cats:
        # 未选类目：按货源数取前 16 个二级类目作为「推荐类目」
        ranked = sorted(sub_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:16]
        leaf_index = {leaf["name"]: leaf for leaf in sourcing_leaf_index()}
        panel_cats = [
            {
                "id": leaf_index[name]["id"],
                "name": name,
                "icon": leaf_index[name]["icon"],
                "accent": leaf_index[name]["accent"],
                "count": cnt,
                "active": sub_filter == name,
            }
            for name, cnt in ranked
            if cnt and name in leaf_index
        ]
    panel_title = panel_node["name"] if panel_node else "推荐类目"
    panel_hint = panel_node["hint"] if panel_node else "全网热卖货源"

    # —— 精选货源 Tab（1688 版式：二级类目横排）——
    sub_tabs = [
        {
            "name": name,
            "count": cnt,
            "cat1": sub_parent.get(name, ""),
            "active": sub_filter == name,
        }
        for name, cnt in sorted(sub_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:14]
        if name != SOURCING_CATEGORY_FALLBACK_HINT
    ]

    # —— 榜单排序 ——
    # 默认就是「最新上架」：2026-10-04 用户拍板，顶部那块独立的「最新上架的货源」区块取消，
    # 直接并进 feed —— 刚上架（渔枢系统发布 / 商家发布 / 重新上架）的货按利润率排会沉到
    # 很后面（341 款里能排到第 6 页），进货源广场第一眼就该看到它们。
    # 想按别的口径看，顶部 tab 可以切「推荐 / 热销 / 降价」。
    if tab == "recommend":  # 利润率 × 库存规模
        filtered.sort(
            key=lambda c: c["profit_rate"] * (1 + min(c["stock"], 500) / 500),
            reverse=True,
        )
    elif tab == "hot":
        filtered.sort(key=lambda c: (c["sold"], c["stock"]), reverse=True)
    elif tab == "cut":
        filtered.sort(key=lambda c: (c["cut_amount"], c["profit_rate"]), reverse=True)
    else:  # new（默认）：按最近上架 / 同步时间倒序
        filtered.sort(key=lambda c: _ts(c["last_synced_at"]), reverse=True)

    # —— feed 流分页（排序后切片；顶部运营位仍基于全量 cards）——
    page_total = max((len(filtered) + HARBOR_PAGE_SIZE - 1) // HARBOR_PAGE_SIZE, 1)
    page_no = max(1, min(int(page or 1), page_total))
    page_start = (page_no - 1) * HARBOR_PAGE_SIZE
    page_cards = filtered[page_start:page_start + HARBOR_PAGE_SIZE]
    page_nav = [
        {
            "no": n,
            "active": n == page_no,
            "url": _harbor_url(
                tab, cat_filter, sub_filter, keyword, None if n == 1 else n,
                l3=l3_filter, node=node_id,
            ),
        }
        for n in _page_window(page_no, page_total)
    ]

    # —— 运营位 4 卡（全部由真实数据派生）——
    new_items = sorted(
        [c for c in cards if c["is_new"]], key=lambda c: _ts(c["last_synced_at"]), reverse=True
    )
    cut_items = sorted([c for c in cards if c["cut_amount"] > 0], key=lambda c: c["cut_amount"], reverse=True)
    hot_items = sorted(cards, key=lambda c: (c["sold"], c["stock"]), reverse=True)
    suppliers = sorted(sup_counter.items(), key=lambda kv: -kv[1])

    ops = {
        "new": {"count": len(new_items), "rank": 1, "label": "今日上新", "tab": "new"},
        "cut": {"count": len(cut_items), "rank": 2, "label": "降价捡漏", "tab": "cut"},
        "hot": {"count": len([c for c in cards if c["sold"] > 0]), "rank": 3, "label": "热销榜单", "tab": "hot"},
        "star": {"count": len(suppliers), "rank": 4, "label": "供应之星", "tab": "recommend",
                 "names": [s for s, _ in suppliers[:3]]},
    }

    # —— 顶部运营位 1：趋势飙升榜（榜单类目 = 当前二级类目 / 货源最多的二级类目）——
    trend_cat = sub_filter or (
        sorted(sub_counts_all.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        if sub_counts_all else ""
    )
    trend_pool = [c for c in cards if c["cat2"] == trend_cat] or cards
    trend = {
        "cat": trend_cat or "全品类",
        "pool": len(trend_pool),
        # 入选商品有几款就摆几款（2 列最多 4 行 = 8 个）：类目栏展开后整行被拉高，
        # 只摆 4 个会让卡片下半截空着（用户 2026-10-04 反馈「按高度展示完整」）
        "cards": sorted(
            trend_pool,
            key=lambda c: (c["sold"], c["profit_rate"], min(c["stock"], 500)),
            reverse=True,
        )[:HARBOR_TREND_LIMIT],
    }

    # —— 顶部运营位 2：采购节点（节点日历 + 关键词实时匹配节点货源）——
    # 主推位永远展示「离今天最近的那个节点」；只有用户点了某个活动（node=xx）
    # 才把它作为搜索条件带进 feed，并让对应的 chip 高亮。
    node_sel = node_filter or (nodes[0] if nodes else None)
    for _n in nodes:
        _n["active"] = bool(node_filter and _n["id"] == node_filter["id"])

    # —— 顶部运营位 3：我的经营卡 ——
    # 数据全部取真实库表：订单 / 推单两张表没有账号归属列，所以三个数字与「订单 N 单」
    # 是全店口径（卡上标「全店」，不装成"我的"）；采购单 / 采购车按当前账号存，单独列。
    # 账号名取当前登录身份（渔枢系统登录用户），不再有「演示访客」这种写死的兜底。
    dso_by_status = dict(
        db.execute(
            select(DownstreamOrder.status, sql_func.count(DownstreamOrder.id)).group_by(
                DownstreamOrder.status
            )
        ).all()
    )
    po_by_status = dict(
        db.execute(
            select(PurchaseOrder.status, sql_func.count(PurchaseOrder.id)).group_by(
                PurchaseOrder.status
            )
        ).all()
    )
    _mine = _purchase_counts(db, request)
    _owner_key, _owner_name = _purchase_owner(request)
    _viewer = hub_shops.viewer_summary(request)
    _viewer_name = str(_viewer.get("name") or "").strip()
    if _viewer_name == "—":
        _viewer_name = ""
    _viewer_name = _viewer_name or ("" if _owner_key == "anonymous" else _owner_name)
    _viewer_role = str(_viewer.get("role_text") or "").strip()
    if _viewer_role == "—":
        _viewer_role = ""
    _logged_in = _owner_key != "anonymous" or bool(_viewer_name)
    me = {
        "to_ship": dso_by_status.get("pending", 0) + dso_by_status.get("confirmed", 0),
        "to_receive": dso_by_status.get("pushed", 0) + dso_by_status.get("shipped", 0),
        "after_sale": dso_by_status.get("refunded", 0) + dso_by_status.get("exception", 0),
        "orders": sum(dso_by_status.values()),
        "purchases": sum(c for s, c in po_by_status.items() if s != "failed"),
        "failed_po": po_by_status.get("failed", 0),
        # 当前账号身份（登录了就显示真名，没登录就直接说未登录）
        "name": _viewer_name or "未登录",
        "avatar": (_viewer_name or "客")[:1].upper(),
        "role_text": _viewer_role if _logged_in else "登录渔枢系统后显示你的账号",
        # 订单 / 推单表没有账号归属列：上面这几个数字是全店口径，模板据此标注「全店」
        "shared_scope": True,
        # 按当前账号隔离的数据（与侧边栏徽标同一套口径）
        "my_pos": _mine["stock_orders"],
        "my_cart": _mine["cart"],
    }

    ctx = _common_ctx(
        request, "harbor", db=db,
        active_sub="harbor:rank" if tab in ("hot", "cut") else "harbor:plaza",
        nav_crumb=["货源", "渔货栈", "货源广场"],
        cards=page_cards,
        page_no=page_no,
        page_total=page_total,
        page_nav=page_nav,
        page_size=HARBOR_PAGE_SIZE,
        page_from=page_start + 1 if filtered else 0,
        page_to=page_start + len(page_cards),
        page_url=lambda p: _harbor_url(tab, cat_filter, sub_filter, keyword, p, l3=l3_filter, node=node_id),
        reset_url=_harbor_url(tab, cat_filter, sub_filter, keyword, None, l3=l3_filter, node=node_id),
        total=len(cards),
        shown=len(filtered),
        keyword=q or "",
        cat_filter=cat_filter or "全部",
        cat_raw=cat or "",
        cat_options=cat_options,
        cat_total=len(cat_counts),
        cat_rail=cat_rail,
        # 类目栏超过 15 个就先收起（1688 那套有 60 个一级，全列出来这一栏比整页还长），
        # 末尾给一个「…」，点开看全部；当前选中的类目正好在收起的那截里就直接展开
        rail_visible=HARBOR_RAIL_VISIBLE,
        rail_open=any(n["active"] for n in cat_rail[HARBOR_RAIL_VISIBLE:]),
        panel_cats=panel_cats,
        panel_title=panel_title,
        panel_hint=panel_hint,
        # 三级类目一次给全，前端默认只摆这么多，其余折叠在「展开全部 N 个」里
        leaf_fold_limit=HARBOR_LEAF_LIMIT,
        panel_count=sum(ch["count"] for ch in panel_cats),
        sub_tabs=sub_tabs,
        sub_filter=sub_filter or "全部",
        sub_total=len(sub_counts),
        l3_filter=l3_filter or "",
        l3_url=lambda name: _harbor_url(
            tab, cat_filter, sub_filter, keyword, None, l3=name, node=node_id
        ),
        tab=tab,
        ops=ops,
        trend=trend,
        nodes=nodes,
        node_sel=node_id or "",
        # 选中的活动页（节点）：名称 + 关键词，结果栏与空态提示要用
        node_name=node_filter["name"] if node_filter else "",
        node_keywords=node_keywords,
        active_node=node_sel,
        node_url=lambda nid: _harbor_url(
            tab, cat_filter, sub_filter, keyword, None, l3=l3_filter, node=nid
        ),
        me=me,
        shelved_count=sum(
            1 for c in cards if c["listing_status"] in ("draft", "published", "needs_review")
        ),
        published_count=sum(1 for c in cards if c["listing_status"] == "published"),
        # 「铺货到店铺」的前置条件（2026-10-11）：没店铺 / 没过审就把卡片按钮置灰，
        # 点了提示去找带教老师（后端在 /self/goods/{id}/pool 与 xianyu handoff 另有兜底）
        shops_approved=int(_shops_approved(request) or 0),
        shop_required_msg=SHOP_REQUIRED_MSG,
    )
    return templates.TemplateResponse("harbor.html", ctx)


@router.get("/harbor/leaves")
def harbor_leaves() -> JSONResponse:
    """货源广场的三级类目清单（一级 → 二级 → 三级名），给类目栏悬浮面板按需补全。

    三级类目一共一万多个，全渲染进 HTML 会把页面撑到好几 MB，所以页面只给「当前展开的
    那个一级」带三级；鼠标移到别的一级时前端拉这份清单（一次性、之后走内存缓存），
    把该一级下所有二级的三级补出来 —— 效果与点击进入该类目一致，二级 / 三级都能点选。
    """
    tree: dict[str, dict[str, list[str]]] = {}
    for top in sourcing_tree():
        tree[top["name"]] = {
            sub["name"]: [leaf["name"] for leaf in sub["children"] if leaf["name"]]
            for sub in top["children"]
        }
    return JSONResponse({"tree": tree, "fold_limit": HARBOR_LEAF_LIMIT})


# =====================================================================
# 2.9 我要录单 /orders/new（选品商品订单 + 其他订单）
# =====================================================================
# 「其他订单」的录入模板：与飞书群里报单的格式一致（照着填就不会漏字段）
OTHER_ORDER_TEMPLATE = """【闲鱼订单】
下单时间：
闲鱼订单号：
商品：
数量：
实付款：
收货人：
电话：
收货地址：
买家留言："""


@router.get("/orders/new", response_class=HTMLResponse)
def order_new_page(
    request: Request,
    item_id: str = "",
    shop_id: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """我要录单：闲鱼出单后在这儿录一笔在线订单。

    - 带 item_id 进来 = 选品商品订单（从「我的店铺商品」点的「下单」），商品自动带出来；
    - 不带 = 其他订单，按飞书群模板填，或传一张闲鱼订单详情图让系统认。
    """
    owner, name = _purchase_owner(request)

    # 「我的店铺商品」里的货：下单页可以直接挑（不用先回到池子页）
    goods: list[dict] = []
    try:
        from ..self_purchase.shop_pool import list_pool  # noqa: PLC0415

        for r in list_pool(db, owner, shop_id=None):
            goods.append(
                {
                    "item_id": str(getattr(r, "item_id", "") or ""),
                    "title": str(getattr(r, "title", "") or ""),
                    "image": str(getattr(r, "image", "") or ""),
                    "shop_id": int(getattr(r, "shop_id", 0) or 0),
                    "shop_name": str(getattr(r, "shop_name", "") or ""),
                    "price": float(getattr(r, "price", 0) or 0),
                    "suggested_price": float(getattr(r, "suggested_price", 0) or 0),
                }
            )
    except Exception as exc:  # noqa: BLE001  池子读不出来也能手工填
        logger.debug("取我的店铺商品失败：%s", exc)

    try:
        from ..self_purchase import manual_order  # noqa: PLC0415

        snap = manual_order.item_snapshot(db, item_id) if item_id else manual_order.item_snapshot(db, "")
    except Exception as exc:  # noqa: BLE001
        logger.debug("取商品快照失败：%s", exc)
        snap = {"item_id": item_id, "title": "", "image": "", "fish_price": 0.0,
                "suggested_price": 0.0}

    return templates.TemplateResponse(
        "order_new.html",
        _common_ctx(
            request, "orders", db=db,
            active_sub="orders:new",
            nav_crumb=["交易", "订单", "我要录单"],
            owner_name=name,
            shops=_my_shop_options(request),
            goods=goods,
            snap=snap,
            item_id=item_id,
            shop_id=shop_id,
            template_text=OTHER_ORDER_TEMPLATE,
        ),
    )


@router.post("/orders/parse-buyer")
async def order_parse_buyer(request: Request) -> JSONResponse:
    """粘贴一段闲鱼下单用户信息 → 认出收货人 / 电话 / 地址（只解析不落库）。"""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001  表单提交也算
        form = await request.form()
        payload = dict(form)
    text = str((payload or {}).get("text", "") or "")
    try:
        from ..self_purchase import manual_order  # noqa: PLC0415

        parsed = manual_order.parse_buyer(text)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": str(exc)})
    return JSONResponse(
        {
            "ok": bool(parsed.get("ok")),
            "message": str(parsed.get("message") or ""),
            "buyer_name": str(parsed.get("receiver_name") or ""),
            "phone": str(parsed.get("phone") or ""),
            "address": " ".join(
                x for x in (str(parsed.get("region") or ""), str(parsed.get("address") or "")) if x
            ).strip(),
            "amount": "",
        }
    )


@router.post("/orders/upload-image")
async def order_upload_image(request: Request) -> JSONResponse:
    """上传一张闲鱼订单详情图：存下来并试着认出单上的信息（认不全就手填）。"""
    form = await request.form()
    files = [v for v in form.values() if hasattr(v, "read")]
    if not files:
        return JSONResponse({"ok": False, "message": "没收到图片"})
    up = files[0]
    data = await up.read()
    if not data:
        return JSONResponse({"ok": False, "message": "图片是空的"})
    try:
        from ..self_purchase import manual_order  # noqa: PLC0415

        url = manual_order.save_upload(str(getattr(up, "filename", "") or ""), data)
        if not url:
            return JSONResponse({"ok": False, "message": "图片没存下来（服务器目录不可写）"})
        got = manual_order.ocr_of(url)
    except Exception as exc:  # noqa: BLE001
        logger.debug("订单详情图处理失败：%s", exc)
        return JSONResponse({"ok": False, "message": str(exc)})

    parsed = got.get("buyer") or {}
    return JSONResponse(
        {
            "ok": bool(got.get("ok")),
            "url": url,
            "message": str(got.get("message") or ""),
            "text": str(got.get("text") or ""),
            "amount": str(got.get("amount") or ""),
            "buyer_name": str(parsed.get("receiver_name") or ""),
            "phone": str(parsed.get("phone") or ""),
            "address": " ".join(
                x for x in (str(parsed.get("region") or ""), str(parsed.get("address") or "")) if x
            ).strip(),
        }
    )


@router.post("/orders/new")
async def order_create(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    """提交一笔在线订单：落 downstream_orders，状态「待确认」，后面走正常流转。"""
    form = await request.form()
    owner, _name = _purchase_owner(request)

    def val(key: str, default: str = "") -> str:
        return str(form.get(key, default) or default).strip()

    item_id = val("item_id")
    title = val("title")
    try:
        from ..self_purchase import manual_order  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return _redirect("/orders", f"下单失败：{exc}", "err")

    try:
        qty = max(int(val("qty", "1") or 1), 1)
    except ValueError:
        qty = 1
    try:
        sale_price = float(val("sale_price", "0") or 0)
    except ValueError:
        sale_price = 0.0
    try:
        fish_price = float(val("fish_price", "0") or 0)
    except ValueError:
        fish_price = 0.0

    shop_name = val("shop_name")
    try:
        shop_id = int(val("shop_id", "0") or 0)
    except ValueError:
        shop_id = 0
    # 店铺名没填就按 id 从选项里补一个（订单上要留得下是哪个店出的单）
    if shop_id and not shop_name:
        for opt in _my_shop_options(request):
            if int(getattr(opt, "id", 0) or 0) == shop_id:
                shop_name = str(getattr(opt, "name", "") or "")
                break

    if not (item_id or title):
        return _redirect("/orders/new", "商品不能空：选一款货或写上商品名", "err")

    images = [str(x) for x in str(val("images")).split(",") if x]
    try:
        row = manual_order.create_manual_order(
            db,
            owner=owner,
            item_id=item_id,
            title=title,
            qty=qty,
            sale_price=sale_price,
            fish_price=fish_price,
            buyer_name=val("buyer_name"),
            buyer_phone=val("buyer_phone"),
            address=val("address"),
            shop_id=shop_id,
            shop_name=shop_name,
            note=val("note"),
            images=images,
            operator=val("operator") or _name,
            source=(
                manual_order.SOURCE_SHOP_GOODS if item_id else manual_order.SOURCE_OTHER
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("手工录单落库失败：%s", exc)
        return _redirect("/orders/new", f"录单失败：{exc}", "err")

    return _redirect(
        f"/orders/{row.downstream_order_id}",
        f"录单已建（{row.downstream_order_id}）：待确认，确认接单后进「待发货」",
    )


# =====================================================================
# 3. 订单列表 /orders
# =====================================================================
@router.get("/orders", response_class=HTMLResponse)
def orders_list(
    request: Request,
    status: str | None = None,
    msg: str = "",
    kind: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """渔货栈自己的订单：以 `downstream_orders` 为主体，字段与渔枢系统端保持一致。

    金额口径（售价 / 采购成本 / 退款 / 平台技术服务费 / 毛利）和推单状态在渔枢系统维护，
    这里按单号把渔枢系统侧的事实取回来合并展示（只读，见 `src/web/hub_orders.py`）；
    单子还没推到渔枢系统就显示「未建档」，页面上能一键补推。
    """
    # 2026-10-14「我要录单」：内部自营同学在飞书群报的单也按运营姓名同步进来，
    # 幂等（同单号只建一次）；桥断了不挡订单列表。
    try:
        hub_orders.sync_group_orders(db)
    except Exception:  # noqa: BLE001
        logger.debug("飞书群报单同步失败（不挡订单列表）", exc_info=True)

    stmt = select(DownstreamOrder).order_by(DownstreamOrder.created_at.desc())
    if status:
        stmt = stmt.where(DownstreamOrder.status == status)
    dsos = db.execute(stmt.limit(200)).scalars().all()

    dso_ids = [d.downstream_order_id for d in dsos]
    po_by_dso: dict[str, list[PurchaseOrder]] = {k: [] for k in dso_ids}
    if dso_ids:
        pos = db.execute(
            select(PurchaseOrder).where(PurchaseOrder.downstream_order_id.in_(dso_ids))
        ).scalars().all()
        for p in pos:
            po_by_dso.setdefault(p.downstream_order_id, []).append(p)

    # 渔枢系统同一单号的订单事实（金额 / 收货 / 推单状态）：字段两边一致
    hub_map = hub_orders.by_order_nos(dso_ids)
    rows = []
    for d in dsos:
        no = hub_orders.base_order_no(d.downstream_order_id)
        rows.append(
            {
                "dso": d,
                "order_no": no,
                "pos": po_by_dso.get(d.downstream_order_id, []),
                "hub": hub_map.get(no),
            }
        )

    status_counts = dict(
        db.execute(
            select(DownstreamOrder.status, sql_func.count(DownstreamOrder.id))
            .group_by(DownstreamOrder.status)
        ).all()
    )

    ctx = _common_ctx(
        request, "orders", db=db,
        active_sub=f"orders:{status}" if status else "orders:all",
        nav_crumb=["交易", "订单"] + ([status] if status else ["全部订单"]),
        rows=rows,
        status_filter=status,
        status_counts=status_counts,
        total=sum(status_counts.values()),
        hub_available=hub_orders.available(),
        hub_count=sum(1 for r in rows if r["hub"]),
        # 状态机：列表页的筛选 chips 与状态列都按这套状态来（含新增的已发货 / 退款中 / 已取消）
        status_labels=order_state.STATUS_LABELS,
        status_order=order_state.STATUS_ORDER,
        msg=msg,
        kind=kind,
    )
    return templates.TemplateResponse("orders_list.html", ctx)


def _field_of(obj, key: str) -> str:
    """从 dict 或对象里取字段（hub 可能是 dict 也可能是 ORM 对象）。"""
    if isinstance(obj, dict):
        return str(obj.get(key) or "").strip()
    return str(getattr(obj, key, "") or "").strip()


def _order_trace(db: Session, dsos, hub, *, force: bool = False) -> dict:
    """订单详情的物流轨迹：走三端共用的「物流轨迹服务」（带本地存档）。

    单号优先取渔枢系统回写的，其次渔货栈自己记的；没有单号就返回空结构
    （页面显示「还没发货」，不会去调接口）。
    """
    empty = {
        "ok": False, "msg": "", "status": "", "nodes": [], "mail_no": "",
        "cp_name": "", "from_cache": False, "queried_at": None,
        "is_final": False, "stale": False,
    }
    mail_no = _field_of(hub, "tracking_no")
    company = _field_of(hub, "tracking_company")
    if not mail_no:
        for d in dsos:
            if (d.shipping_no or "").strip():
                mail_no = (d.shipping_no or "").strip()
                company = (d.shipping_company or "").strip()
                break
    if not mail_no:
        return empty
    try:
        from app.services import logistics_trace
    except Exception:  # noqa: BLE001  没接渔枢系统的部署不显示轨迹
        return empty
    phone = ""
    for d in dsos:
        phone = str(getattr(d, "buyer_phone", "") or "").strip()
        if phone:
            break
    try:
        return logistics_trace.lookup(
            db, mail_no, cp_name=company, phone=phone, force=force
        )
    except Exception as exc:  # noqa: BLE001  查不到轨迹不影响看订单
        logger.warning("物流轨迹查询失败（%s）：%s", mail_no, exc)
        return dict(empty, msg=f"物流轨迹查询失败：{exc}")


# =====================================================================
# 4. 订单详情 /orders/{dso_id}
# =====================================================================
@router.get("/orders/{dso_id:path}", response_class=HTMLResponse)
def order_detail(
    dso_id: str,
    request: Request,
    msg: str = "",
    kind: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    dsos = db.execute(
        select(DownstreamOrder)
        .where(DownstreamOrder.downstream_order_id.like(f"{dso_id}%"))
        .order_by(DownstreamOrder.id)
    ).scalars().all()
    if not dsos:
        ctx = _common_ctx(
            request, "orders", db=db,
            active_sub="orders:all",
            nav_crumb=["交易", "订单", "未找到"],
            not_found=True, dso_id=dso_id,
        )
        return templates.TemplateResponse("order_detail.html", ctx)

    pos = db.execute(
        select(PurchaseOrder).where(
            PurchaseOrder.downstream_order_id.in_([d.downstream_order_id for d in dsos])
        )
    ).scalars().all()
    exceptions = db.execute(
        select(ExceptionLog)
        .where(
            ExceptionLog.payload["downstream_order_id"].as_string().in_(
                [d.downstream_order_id for d in dsos]
            )
        )
        .order_by(ExceptionLog.id.desc())
        .limit(50)
    ).scalars().all()

    # 渔枢系统那笔单：金额口径 / 推单状态 / 收货信息（字段与渔枢系统端一致）
    order_no = hub_orders.base_order_no(dsos[0].downstream_order_id)
    hub = hub_orders.one(order_no)

    # 渔枢系统履约回写：它那边发了货 / 退了款，这边跟着往下流转（幂等）
    synced: list[str] = []
    if hub:
        try:
            synced = hub_sync.sync_from_hub(db, dso_id, hub)
        except Exception as exc:  # noqa: BLE001  回写失败不影响看订单
            logger.warning("渔枢系统履约回写失败（订单 %s）：%s", dso_id, exc)
        if synced:
            db.refresh(dsos[0])
            hub = hub_orders.one(order_no)
            if not msg:
                msg = "渔枢系统已回写：" + "、".join(
                    order_state.ACTIONS[s].label for s in synced if s in order_state.ACTIONS
                )

    events = order_flow.events_of(db, [d.downstream_order_id for d in dsos])

    # 物流轨迹：走三端共用的「物流轨迹服务」（app.services.logistics_trace）——
    # 同一单号一小时之内不会重复调菜鸟，直接给本地存档；?trace_force=1 才是强制实时查。
    trace = _order_trace(
        db,
        dsos,
        hub,
        force=request.query_params.get("trace_force") == "1",
    )

    ctx = _common_ctx(
        request, "orders", db=db,
        active_sub="orders:all",
        nav_crumb=["交易", "订单", dsos[0].downstream_order_id],
        dsos=dsos, pos=pos, exceptions=exceptions,
        dso_id=dso_id, not_found=False,
        current_statuses={d.status for d in dsos},
        order_no=order_no, hub=hub,
        trace=trace,
        hub_available=hub_orders.available(),
        # 状态机：进度条 / 当前能做什么动作 / 履约字段
        fsum=order_flow.summary(dsos),
        # 渔货栈自己的毛利（定稿公式，成本用渔货价而不是渔枢系统的飞书群成本价）
        self_profit=order_flow.self_profit(dsos, hub),
        events=events,
        status_labels=order_state.STATUS_LABELS,
        action_labels={k: v.label for k, v in order_state.ACTIONS.items()},
        synced=synced,
        msg=msg, kind=kind,
    )
    return templates.TemplateResponse("order_detail.html", ctx)


@router.post("/orders/{dso_id:path}/action")
async def order_action(
    dso_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """订单状态机的一次动作（确认接单 / 推给渔枢系统采购 / 标记发货 / 确认收货 / 退款 …）。

    能不能做由 `src/order/state.py` 说了算：不在当前状态允许列表里的动作直接拒绝，
    不会留下「已发货但没有物流单号」这种脏数据。
    """
    form = await request.form()
    action = str(form.get("action") or "").strip()
    if not action:
        return _redirect(f"/orders/{dso_id}", "没指定要做什么动作", "err")

    hub_user = hub_shops.viewer(request)
    actor = str(getattr(hub_user, "username", "") or "").strip() or "运营"
    data = {
        "shipping_no": str(form.get("shipping_no") or "").strip(),
        "shipping_company": str(form.get("shipping_company") or "").strip(),
        "refund_amount": str(form.get("refund_amount") or "").strip(),
    }
    ok, text = order_flow.apply_action(
        db, dso_id, action, actor=actor, note=str(form.get("note") or "").strip(), **data
    )
    return _redirect(f"/orders/{dso_id}", text, "ok" if ok else "err")


@router.post("/orders/{dso_id:path}/sync-hub")
def order_push_to_hub(
    dso_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """把渔货栈这笔单推到渔枢系统（补建档 / 补空字段，幂等）。

    收单时会自动推一次（src/order/service.py 的 _sync_to_hub）；这里给运营一个
    手动的补推入口：当时同步失败、或渔枢系统后来才部署起来的情况，点一下就能补上。
    """
    if not hub_orders.available():
        return _redirect(f"/orders/{dso_id}", "当前部署没接入渔枢系统，推不了", "err")
    dso = db.execute(
        select(DownstreamOrder).where(DownstreamOrder.downstream_order_id == dso_id)
    ).scalars().first()
    if dso is None:
        return _redirect("/orders", f"找不到订单 {dso_id}", "err")
    try:
        order, action = hub_orders.push_to_hub(db, dso)
    except Exception as exc:  # noqa: BLE001  推不过去也要留住渔货栈这单
        return _redirect(f"/orders/{dso_id}", f"推送到渔枢系统失败：{exc}", "err")

    from app.services.dso_sync import ACTION_CREATED, ACTION_FILLED, ACTION_SKIPPED

    if action == ACTION_SKIPPED:
        return _redirect(f"/orders/{dso_id}", "这笔单没有单号，推不了", "err")
    text = {
        ACTION_CREATED: "已在渔枢系统建档（状态：新建，等审单推单）",
        ACTION_FILLED: "渔枢系统已有这笔单：补齐了空字段",
    }.get(action, "渔枢系统已有这笔单，字段都齐，无需改动")
    return _redirect(f"/orders/{dso_id}", f"{text}（渔枢系统订单 #{order.id}）")


# =====================================================================
# 5. 商品管理 /goods（列表 / 卡片双布局）
# =====================================================================
_GOODS_AUDIT_FILTERS = ("draft", "pending", "approved", "rejected")
_GOODS_SOURCE_FILTERS = ("self_operated", "third_party")


def _goods_stats(db: Session) -> dict[str, int]:
    """商品管理页的 chips 计数（全量，不受当前筛选影响）。"""
    by_source = dict(
        db.execute(
            select(SupplierItem.source_type, sql_func.count(SupplierItem.id)).group_by(
                SupplierItem.source_type
            )
        ).all()
    )
    by_audit = dict(
        db.execute(
            select(SupplierItem.audit_status, sql_func.count(SupplierItem.id)).group_by(
                SupplierItem.audit_status
            )
        ).all()
    )
    return {
        "total": sum(by_source.values()),
        "self": by_source.get("self_operated", 0),
        "third": by_source.get("third_party", 0),
        "draft": by_audit.get("draft", 0),
        "pending": by_audit.get("pending", 0),
        "approved": by_audit.get("approved", 0),
        "rejected": by_audit.get("rejected", 0),
    }


@router.get("/goods", response_class=HTMLResponse)
def goods_list(
    request: Request,
    view: str = "list",
    source: str = "all",
    audit: str = "all",
    q: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """供应链 → 商品管理。

    - `view=list` 表格布局：字段密集 + 批量勾选，便于**商品审核**
    - `view=card` 卡片布局：突出主图与关键指标，便于**日常查阅**
    - `source` 区分自营 / 供应商供应链；`audit` 过滤审核状态
    """
    stmt = select(SupplierItem).order_by(SupplierItem.last_synced_at.desc().nullslast())
    if source in _GOODS_SOURCE_FILTERS:
        stmt = stmt.where(SupplierItem.source_type == source)
    if audit in _GOODS_AUDIT_FILTERS:
        stmt = stmt.where(SupplierItem.audit_status == audit)
    keyword = (q or "").strip()
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                SupplierItem.title.like(like),
                SupplierItem.item_id.like(like),
                SupplierItem.supplier_id.like(like),
                # 品牌 / 商家编码 / 类目：渔枢系统上架过来的商品靠这几项才搜得到
                SupplierItem.brand.like(like),
                SupplierItem.item_code.like(like),
                SupplierItem.category_id.like(like),
            )
        )
    items = db.execute(stmt.limit(200)).scalars().all()

    item_ids = [i.item_id for i in items]
    mapping_counts: dict[str, int] = {}
    listing_status: dict[str, str] = {}
    if item_ids:
        mapping_counts = {
            iid: cnt
            for iid, cnt in db.execute(
                select(SkuMapping.item_id, sql_func.count(SkuMapping.id))
                .where(SkuMapping.item_id.in_(item_ids))
                .group_by(SkuMapping.item_id)
            ).all()
        }
        for li in db.execute(
            select(ChannelListing).where(ChannelListing.item_id.in_(item_ids))
        ).scalars().all():
            listing_status.setdefault(li.item_id, li.status)

    # 自营商品已独立为顶层菜单（与「商品管理」同级），侧边栏高亮按来源分流
    if source == "self_operated":
        active_nav, active_sub = "goods_self", "goods_self:list"
    elif audit == "pending":
        active_nav, active_sub = "goods", "goods:pending"
    else:
        active_nav, active_sub = "goods", "goods:list"

    crumb_group = "自营商品" if active_nav == "goods_self" else "商品管理"
    ctx = _common_ctx(
        request, active_nav, db=db,
        active_sub=active_sub,
        nav_crumb=["供应链", crumb_group, "卡片浏览" if view == "card" else "商品列表"],
        items=items,
        mapping_counts=mapping_counts,
        listing_status=listing_status,
        stats=_goods_stats(db),
        view="card" if view == "card" else "list",
        source_filter=source,
        audit_filter=audit,
        keyword=keyword,
        shown=len(items),
        # 「铺货到店铺」前置条件（2026-10-11）：没店铺 / 没过审 → 按钮置灰 + 点提示
        shops_approved=int(_shops_approved(request) or 0),
        shop_required_msg=SHOP_REQUIRED_MSG,
    )
    return templates.TemplateResponse("goods.html", ctx)


# =====================================================================
# 5b. 商品上架 /goods/new（自营供应链）
# =====================================================================
@router.get("/goods/new", response_class=HTMLResponse)
def goods_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """自营商品发布页（表单交互对齐微信小店 / 抖店：类目 → 图文 → 规格 SKU → 价格 → 物流）。"""
    ctx = _common_ctx(
        request, "goods_self", db=db,
        active_sub="goods_self:new",
        nav_crumb=["供应链", "自营商品", "商品上架"],
        categories=category_tree(),
        spec_templates=SPEC_TEMPLATES,
        default_spec=DEFAULT_SPEC_TEMPLATE,
        # 运费模板 / 发货方式与渔枢系统同一口径（规则在「货源 · 运费模板」维护）
        freight_templates=hub_freight_templates(),
        shipping_choices=SHIPPING_CHOICES,
        after_sale_options=AFTER_SALE_OPTIONS,
        supplier_count=db.scalar(
            select(sql_func.count(sql_func.distinct(SupplierItem.supplier_id)))
        ) or 0,
    )
    return templates.TemplateResponse("goods_new.html", ctx)


def _hub_product_service():
    """借用渔枢系统的标题 / 别名推荐（同一套类目特性规则）；独立部署时返回 None。"""
    try:
        from app.services import product as hub_product  # noqa: PLC0415

        return hub_product
    except Exception:  # noqa: BLE001  渔货栈单独部署时没有渔枢系统那套
        return None


def _self_category_text(category_id: str | None) -> str:
    """二级类目 id → 「一级 / 二级」中文路径（类目特性规则按它查）。"""
    return category_path().get(category_id or "", "")


def _hub_db_scope():
    """拿渔枢系统库的会话（类目 / 类目特性规则 / 运费规则都住在那边）。

    与 `src/goods/catalog.py::_session_scope` 同一套：优先渔枢系统自己的连接，
    独立部署（没有 app 包）时回退渔货栈自己的库 —— 两边同库时两者等价。
    """
    from src.goods.catalog import _session_scope  # noqa: PLC0415

    return _session_scope()


async def _request_json(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001  前端传错格式就按空处理，不要 500
        return {}
    return data if isinstance(data, dict) else {}


def _suggested_price_of(db: Session, item: SupplierItem) -> Decimal:
    """商品级「建议售价」的唯一口径（详情页 / 铺货记录 / 预填包显示都用它）。

    商品档案填了建议零售价（sale_price）就先用它；没填才回落到按成本 + 加价率算出来的价。
    以前详情页显示建议售价 99、而预填包 / 铺货记录按加价率算出 54.60，两个价并存很乱。
    """
    if (item.sale_price or 0) > 0:
        return Decimal(str(item.sale_price))
    rule = pick_rule(
        db, channel="xianyu", category_id=item.category_id, supplier_id=item.supplier_id
    )
    return calculate_price(item, rule)


@router.post("/goods/{item_id}/listing", response_class=JSONResponse)
async def goods_listing_action(
    item_id: str, request: Request, db: Session = Depends(get_db)
) -> JSONResponse:
    """商品详情页（1688 版）底部两个按钮的服务端入口。

    - `mode=handoff`：铺到自己的店铺 —— 落一条铺货记录（source=handoff + 带入发布时间）
      + 生成闲鱼发布预填包 / 发布页 URL（页面拿到后 window.open）
    - `mode=draft`：仅生成草稿 —— 只落记录（source=draft），不打开任何页面

    以前这两步在前端各拼各的（一个打开发布页、一个直接打 /api/v1/selection/run），
    点完在自己系统里**什么都不留**，「我的上架」永远是空的。现在统一在这里落库：
    同一款货只有一条记录（幂等），已上架（published）的记录不改状态。

    已被渔枢系统下架的货源 → 409（与 POST /api/v1/xianyu/handoff 同一道闸）。
    """
    payload = await _request_json(request)
    mode = str(payload.get("mode") or "handoff").strip().lower()
    if mode not in ("handoff", "draft"):
        return JSONResponse({"detail": "mode 只支持 handoff / draft"}, status_code=400)

    item = db.query(SupplierItem).filter(SupplierItem.item_id == item_id).first()
    if item is None:
        return JSONResponse({"detail": f"商品不存在：{item_id}"}, status_code=404)
    if getattr(item, "unlisted_at", None) and not item.is_listed:
        return JSONResponse(
            {"detail": "该商品已由渔枢系统从渔货栈下架，不能再上架到店铺"},
            status_code=409,
        )

    package = build_publish_package(item, db)
    # 落库的价格用「建议售价」口径（与详情页显示的同一个数：商品档案填了建议零售价就用它，
    # 否则回落到按成本 + 加价率算出来的价），免得铺货记录和详情页两个价
    suggested = _suggested_price_of(db, item)
    # 归属当前账号：2026-10-13 起铺货状态按人隔离，A 铺的货不出现在 B 的卡片上
    _owner_key, _owner_name = _purchase_owner(request)
    if mode == "draft":
        # 仅存草稿：不跑选品规则、不打开页面，静默落一条可查的记录
        row = save_listing(
            db,
            item,
            owner=_owner_key,
            source="draft",
            price=suggested,
            title=package.title,
            commit=True,
        )
        return JSONResponse({
            "ok": True,
            "mode": mode,
            "listing": to_brief(row),
            # 「我的上架」页已下线（2026-10-11）：记录仍有，去「我的店铺商品」看铺货状态
            "listing_url": self_url("/shop-goods"),
            "publish_url": "",
            "message": f"铺货草稿 #{row.id} 已保存（{status_text(row.status)}）",
        })

    issued = issue_handoff(item, db)
    data = issued.to_dict()
    row = save_listing(
        db,
        item,
        owner=_owner_key,
        source="handoff",
        price=suggested,
        title=package.title,
        handoff=True,
        commit=True,
    )
    # 与「店铺商品池」互通（2026-10-03）：铺到自己店里 = 这件货进池，
    # 两边看的是同一件事，不会出现「我的上架有、商品池没有」。
    pooled = _pool_item_for_listing(db, item, request)
    data.update({
        "ok": True,
        "mode": mode,
        "listing": to_brief(row),
        # 「我的上架」页已下线（2026-10-11）：铺货记录仍落库，页面去「我的店铺商品」看
        "listing_url": self_url("/shop-goods"),
        # 池子那条（与铺货记录同一件事的两份记录，互通后状态一致）
        "pool": pooled,
        "pool_url": self_url("/shop-goods"),
        "message": (
            f"已创建铺货记录 #{row.id}（{status_text(row.status)}）"
            + ("· 已同步进店铺商品池 " if pooled else "")
            + " · 正在打开闲鱼发布页…"
        ),
    })
    return JSONResponse(data)


@router.get("/goods/{item_id}/listing", response_class=JSONResponse)
def goods_listing_status(
    item_id: str, request: Request, db: Session = Depends(get_db)
) -> JSONResponse:
    """查这款货在**当前账号**名下的铺货记录（页面刷新 / 货源广场徽章用，没有就返回 {}）。"""
    owner, _name = _purchase_owner(request)
    row = (
        db.query(ChannelListing)
        .filter(
            ChannelListing.item_id == item_id,
            ChannelListing.channel == "xianyu",
            ChannelListing.owner == owner,
        )
        .first()
    )
    return JSONResponse({"listing": to_brief(row)})


@router.get("/goods/next-code", response_class=JSONResponse)
def goods_next_code(category_id: str = "", db: Session = Depends(get_db)) -> JSONResponse:
    """商家编码预览：YHZ + 一二三级类目简拼 + 日期 + 4 位流水号（提交时才真正占号）。"""
    return JSONResponse({"code": next_item_code(db, category_id)})


@router.post("/goods/suggest-title", response_class=JSONResponse)
async def goods_suggest_title(request: Request) -> JSONResponse:
    """AI 生成渔货商品标题：品牌词 + 产品词 + 修饰词 + 热搜词 + 长尾词（填满 30 字）。"""
    payload = await _request_json(request)
    hub = _hub_product_service()
    scope = _hub_db_scope()
    if hub is None or scope is None:
        return JSONResponse({"titles": [], "has_rule": False, "title_limit": 30})
    with scope() as hub_db:
        data = hub.suggest_titles(
            hub_db,
            name=str(payload.get("name") or ""),
            brand=str(payload.get("brand") or ""),
            category=_self_category_text(str(payload.get("category_id") or "")),
            selling_points=str(payload.get("selling_points") or ""),
        )
    return JSONResponse(data)


@router.get("/goods/shipping-default", response_class=JSONResponse)
def goods_shipping_default(category_id: str = "") -> JSONResponse:
    """按类目给出该用的运费模板与闲鱼发货方式（发布页预选，留「自动」时由后端套用）。"""
    empty = {"freight_template": "", "freight_fee": 0, "shipping_mode": "", "freight_rule": {}, "method_rule": {}}
    scope = _hub_db_scope()
    if scope is None:
        return JSONResponse(empty)
    try:
        from app.services import shipping as hub_shipping  # noqa: PLC0415

        with scope() as hub_db:
            data = hub_shipping.resolve_for_product(
                hub_db, category_text=_self_category_text(category_id), supplier_id=None
            )
    except Exception:  # noqa: BLE001  独立部署时没有渔枢系统那套规则表
        return JSONResponse(empty)
    return JSONResponse(data)


@router.get("/goods/category-rules", response_class=JSONResponse)
def goods_category_rules(category_id: str = "") -> JSONResponse:
    """类目特性规则：发布页展示该类目的经营提示（同时是标题 / 别名的词库来源）。"""
    empty = {"category": "", "tips": [], "words": {}}
    scope = _hub_db_scope()
    if scope is None:
        return JSONResponse(empty)
    try:
        from app.services import category as hub_category  # noqa: PLC0415

        with scope() as hub_db:
            rule = hub_category.rules_for(hub_db, _self_category_text(category_id))
    except Exception:  # noqa: BLE001
        return JSONResponse(empty)
    rules = rule["rules"]
    return JSONResponse(
        {
            "category": rule.get("category", ""),
            "tips": rules["tips"],
            "words": {
                "产品词": rules["product"], "修饰词": rules["modifier"],
                "热搜词": rules["hot"], "长尾词": rules["longtail"],
            },
        }
    )


# =====================================================================
# 5c. 商品详情 /goods/detail/{item_id}（1688 版式）
# =====================================================================
# 2026-10-03 起商品详情页只有这一版（照 1688：左图 / 右价 / 规格选择 /
# 服务承诺 / 底部结算条 / 右侧货源卡 / 下方 Tab / 同类货源），经典版已废弃
# —— 老地址 /goods/detail/{item_id}/1688 做 301 跳到这里（见下面那条路由），
# 别处的链接一律写无后缀那个。
# 数据口径：渔货价 resolve_cost、建议售价取商品「建议售价」（没填才用定价引擎）、
# SKU 来自 sku_list、上架动作走 handoff / listing 接口。
@router.get("/goods/detail/{item_id}", response_class=HTMLResponse)
def goods_detail(
    item_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """商品详情页（1688 版式）：渔货栈商品的全量信息 + 采购 / 铺货入口。"""
    item = db.query(SupplierItem).filter(SupplierItem.item_id == item_id).first()
    if item is None:
        return templates.TemplateResponse(
            "goods_detail_1688.html",
            _common_ctx(
                request, "goods", db=db,
                active_sub="goods:list",
                nav_crumb=["渔货栈", "货源广场", "未找到"],
                not_found=True, item_id=item_id,
            ),
            status_code=404,
        )

    rule = pick_rule(
        db, channel="xianyu", category_id=item.category_id, supplier_id=item.supplier_id
    )
    suggested = _suggested_price_of(db, item)
    cost_info = resolve_cost(item)
    cost = cost_info.value
    profit_rate = float((suggested - cost) / suggested) if suggested and suggested > 0 else 0.0

    sku_rows, reserved_total = _detail_sku_rows(db, item, rule, suggested)

    # 铺货记录只看当前账号自己那条（2026-10-13 起按账号隔离）
    _detail_owner, _detail_owner_name = _purchase_owner(request)
    listing = db.execute(
        select(ChannelListing)
        .where(ChannelListing.item_id == item.item_id)
        .where(ChannelListing.owner == _detail_owner)
        .order_by(ChannelListing.id)
        .limit(1)
    ).scalars().first()

    images = _goods_images(item)
    package = build_publish_package(item, db)
    # 在售闲鱼号：统计数量（按已铺货的账号数），与经典版 / 渔枢系统同一口径
    # 铺货记录按人隔离后一条记录 = 一个账号，所以按 owner 去重计数
    live_shop_count = (
        db.scalar(
            select(sql_func.count(sql_func.distinct(ChannelListing.owner))).where(
                ChannelListing.item_id == item.item_id,
                ChannelListing.status == "published",
                ChannelListing.owner != "",
            )
        )
        or 0
    )

    # 商品详情按「组」展示：文案一组一段（渔货栈这份只有 detail_html，按段还原），
    # 详情图每 9 张一组 —— 与渔枢系统查看页同一口径
    from app.services import product as hub_product  # noqa: PLC0415

    _detail_images = [u for u in (item.detail_images or []) if u]
    # 1688 抓来的货没有单独的详情图字段，图都混在 detail_html 里：捞出来单独成组，
    # 文案里就不重复放了（不然文案组里既有字又有图，分组展示就白做了）
    for _src in hub_product.strip_detail_images(item.detail_html or "")[1]:
        if _src and _src not in _detail_images:
            _detail_images.append(_src)
    _detail_groups = hub_product.split_detail_groups(item.detail_html or "")
    _category_text = category_path().get(item.category_id or "", item.category_id or "未分类")

    ctx = _common_ctx(
        request,
        "goods_self" if item.source_type == "self_operated" else "goods",
        db=db,
        active_sub="goods_self:list" if item.source_type == "self_operated" else "goods:list",
        nav_crumb=["渔货栈", "货源广场", "商品详情"],
        item=item,
        detail_groups=_detail_groups,
        detail_image_groups=hub_product.detail_image_groups(_detail_images),
        detail_image_group_size=hub_product.DETAIL_IMAGE_GROUP_SIZE,
        # AI 重写某组文案时带上的商品信息（品牌 / 类目 / 卖点决定重写出来的词）
        rewrite_ctx={
            "name": item.title or "",
            "brand": item.brand or "",
            "category": _category_text,
            "selling_points": item.selling_points or "",
        },
        category_text=_category_text,
        rule=rule,
        suggested_price=float(suggested),
        profit_rate=profit_rate,
        cost=float(cost),
        cost_info=cost_info,
        reserved_total=reserved_total,
        available_stock=max(0, int(item.stock or 0) - reserved_total),
        sku_rows=sku_rows,
        spec_defs=[s for s in (item.spec_defs or []) if isinstance(s, dict)],
        images=images,
        # 卖点：渔枢系统商品档案维护、随商品同步过来；详情页独立「商品卖点」页签展示
        selling_point_list=_selling_point_list(item),
        # 详情图 = 单独维护的那份 + detail_html 里混着的那些（去重、保序）
        detail_images=_detail_images,
        listing=listing,
        live_shop_count=live_shop_count,
        package=package.to_dict(),
        xianmou_url=xianmou_publish_url(),
        xianyu_url=xianyu_publish_url(),
        profit_amount=float(suggested - cost),
        shipping_mode_label=shipping_mode_label(item.shipping_mode),
        # 商品标签（固定标签）：渔枢系统发布商品时勾选的，作为商品属性展示；
        # 「直降 x 元 / 高毛利 x%」这类动态标签不在这里，卡片上按数据实时算
        item_tags=[
            {"text": name, "kind": tag_kind_of(name)}
            for name in split_item_tags(item.tags)
        ],
        unlisted=bool(item.unlisted_at and not item.is_listed),
        unlisted_at=item.unlisted_at,
        unlisted_reason=item.unlisted_reason or "",
        unlisted_by=item.unlisted_by or "",
        unlisted_snapshot=item.unlisted_snapshot if isinstance(item.unlisted_snapshot, dict) else {},
        # 1688 版底部「同类货源推荐」：同类目优先，不够再按最近同步补
        similar=_similar_items(db, item),
        # 店铺商品池：右列那个标记从「未铺货」改成池子口径
        pooled=_pooled_of(request, db, item.item_id),
        # 2026-10-11：详情页直接「复制商品文案 / 批量下载图片」（不用先去池子下素材包）。
        # 文案与素材包里的「文案.txt」是同一份（shop_pool._copy_text）。
        copy_text=_detail_copy_text(request, db, item),
        # 主图 + 详情图：批量下载用（去重保序）
        download_images=_dedup_urls(list(images or []) + list(_detail_images or [])),
        # 「店铺铺货」的前置条件（2026-10-11）：名下没有审核通过的店铺就把按钮置灰，
        # 点了给 SHOP_REQUIRED_MSG 那句原话。ctx 里的 0 只是给模板判断用的
        # （后端兜底另有 _shops_approved，它对「没登录态」不拦）。
        shops_approved=int(_shops_approved(request) or 0),
        shop_required_msg=SHOP_REQUIRED_MSG,
    )
    return templates.TemplateResponse("goods_detail_1688.html", ctx)


def _dedup_urls(urls) -> list[str]:
    """图片去重保序（批量下载别重复下一张）。"""
    out: list[str] = []
    for url in urls or []:
        text = str(url or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _detail_copy_text(request: Request, db, item) -> str:
    """详情页「复制商品文案」用的文本：与素材包里的「文案.txt」同一份。

    没进「我的店铺商品」也能复制（文案只跟货有关）；取不到就退回标题 + 描述。
    """
    try:
        from ..self_purchase.shop_pool import _copy_text  # noqa: PLC0415

        owner, _name = _purchase_owner(request)
        row = None
        try:
            from ..models import ShopGoods  # noqa: PLC0415

            row = (
                db.execute(
                    select(ShopGoods).where(
                        ShopGoods.owner == owner, ShopGoods.item_id == item.item_id
                    )
                )
                .scalars()
                .first()
            )
        except Exception:  # noqa: BLE001
            row = None
        return _copy_text(item, row)
    except Exception as exc:  # noqa: BLE001
        logger.debug("生成商品文案失败：%s", exc)
        return str(item.title or "")


# =====================================================================
# 5d. 商品图片：一键打包下载（电脑）+ 保存到手机相册（2026-10-13）
# =====================================================================
# 以前「下载全部图片」是浏览器里逐张触发下载 —— 二十张图要点二十次下载、
# 手机上更是一张张长按存，现场根本不用。现在两条路：
#   · 电脑（货源/自营端详情页「下载图片包」）：后端打包成 zip 一次性下来；
#   · 手机（「存到手机相册」）：给一个图片墙页，所有图一次铺开，长按就能存
#     —— 浏览器不允许网页直接写相册，这是唯一合规又省事的做法。
MAX_ZIP_IMAGES = 24  # 每类最多打包多少张（与素材包同一口径，防一下拉几百张）


def _item_image_sets(item) -> tuple[list[str], list[str]]:
    """这款货的主图与详情图（去重保序）：详情页下载、zip 包、保存页共用这一份。

    详情图 = 单独维护的那份 + 详情 HTML 里混着的那些（1688 抓来的货图都在正文里），
    与详情页 ``download_images`` 完全同源 —— 别两边各剥一次，免得张数对不上。
    """
    from app.services import product as hub_product  # noqa: PLC0415

    mains = _dedup_urls([item.main_image, *(getattr(item, "images", None) or [])])
    details = list(getattr(item, "detail_images", None) or [])
    for src in hub_product.strip_detail_images(item.detail_html or "")[1]:
        if src and src not in details:
            details.append(src)
    return mains[:MAX_ZIP_IMAGES], _dedup_urls(details)[:MAX_ZIP_IMAGES]


_IMAGE_MAGICS = (  # 常见位图的文件头：写包前验一遍，别再把 HTML 错误页当 jpg 打包
    b"\xff\xd8\xff",              # JPEG
    b"\x89PNG\r\n\x1a\n",         # PNG
    b"GIF8",                      # GIF87a / GIF89a
    b"RIFF",                      # WEBP（RIFF....WEBP）
)


def _looks_like_image(data: bytes | None) -> bool:
    if not data:
        return False
    if any(data.startswith(m) for m in _IMAGE_MAGICS):
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _read_local_image(url: str) -> bytes | None:
    """站内图（``/media/...``）不走 HTTP 回环、直接读本地 media 目录。

    以前 ``_images_zip_of`` 对站内图也发 httpx 回环请求 —— /media 没进免登录前缀，
    服务端裸抓被鉴权中间件 303 到登录页，跟随重定向拿到 200 的 HTML 原样存成
    jpg，解压出来全是空白图。站内图本来就在同一台机器的磁盘上，直接读又快又稳。
    """
    path = str(url or "").split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/media/"):
        return None
    try:
        from app.config import settings  # noqa: PLC0415

        root = Path(str(getattr(settings, "media_path", ""))).resolve()
        if not str(root):
            return None
        target = (root / path[len("/media/"):]).resolve()
        if root != target and root not in target.parents:
            return None  # 越出 media 目录的路径一律不给读
        if not target.is_file():
            return None
        return target.read_bytes()
    except Exception:  # noqa: BLE001
        return None


def _abs_image_url(url: str, request: Request | None = None) -> str:
    """把图补成能抓的绝对地址。

    渔枢上架过来的货源，图多半是站内的 ``/media/products/xxx.jpg``（相对路径）——
    httpx 认不出没有域名的地址，以前直接拿去请求，结果整包除了 README 一张图都没有。
    """
    if not url:
        return ""
    if url.startswith(("http://", "https://", "data:")):
        return url
    base = str(request.base_url).rstrip("/") if request is not None else ""
    if not base or "testserver" in base:
        try:
            from app.config import settings  # noqa: PLC0415

            base = str(getattr(settings, "public_base_url", "") or "").rstrip("/")
        except Exception:  # noqa: BLE001
            base = ""
    if not base:
        base = "http://127.0.0.1:8000"  # 合并部署时服务就在本机这个端口
    return base + "/" + url.lstrip("/")


def _images_zip_of(item, request: Request | None = None) -> tuple[bytes, str]:
    """把这款货的图打包成 zip，返回 (字节, 文件名)。

    包里按「主图 / 详情图」分两个目录，编号 01、02…（顺序即详情页的顺序）；
    个别图抓不下来不影响出包，缺哪些写进 README.txt。
    """
    from ..self_purchase.shop_pool import _ext_of, _fetch  # noqa: PLC0415

    mains, details = _item_image_sets(item)
    failed: list[str] = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder, urls in (("主图", mains), ("详情图", details)):
            for index, url in enumerate(urls, 1):
                # 站内 /media 图直接读本地磁盘（httpx 回环会被鉴权 303 到登录页，
                # 登录页 HTML 存成 jpg 就是用户看到的「空白图」）；外链图才走抓取
                data = _read_local_image(url)
                if data is None and not url.startswith("/media/"):
                    data = _fetch(_abs_image_url(url, request))
                if not _looks_like_image(data):
                    failed.append(f"{folder} {index:02d}  {url}")
                    continue
                zf.writestr(f"{folder}/{index:02d}{_ext_of(url)}", data)
        readme = [
            "渔货栈 · 商品图片包",
            f"商品：{item.title or ''}",
            f"货源：{item.item_id}",
            "",
            f"主图 {len(mains)} 张、详情图 {len(details)} 张，"
            f"成功 {len(mains) + len(details) - len(failed)} 张。",
            "",
            "解压后「主图 / 详情图」两个目录按编号排好序，直接就能传到闲鱼发布页。",
        ]
        if failed:
            readme += ["", "以下图片这次没抓到（图床临时挂了，稍后重下即可）：", *failed]
        zf.writestr("README.txt", "\n".join(readme) + "\n")

    code = str(item.item_id or "images").replace("/", "-")
    title = re.sub(r"[\\/:*?\"<>|\s]+", "_", str(item.title or ""))[:20]
    return buf.getvalue(), f"{title or '商品'}图片_{code}.zip"


@router.get("/goods/{item_id}/images.zip")
def goods_images_zip(item_id: str, request: Request, db: Session = Depends(get_db)):
    """商品图片包：主图 + 详情图一次性打包下载（替代以前的一张张下载）。"""
    item = db.query(SupplierItem).filter(SupplierItem.item_id == item_id).first()
    if item is None:
        return JSONResponse(
            {"ok": False, "message": f"货源不存在：{item_id}"}, status_code=404,
        )
    data, name = _images_zip_of(item, request)
    # 文件名带中文：既给 ASCII 兜底又给 RFC 5987 的 UTF-8 写法（旧浏览器只认前者）
    ascii_name = "images-" + str(item.item_id or "").replace("/", "-") + ".zip"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/zip",
        headers={
            "Content-Disposition":
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
        },
    )


@router.get("/goods/{item_id}/images", response_class=HTMLResponse)
def goods_images_page(
    item_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """保存到相册专用页：所有图一次铺开，长按每张即可存进手机相册。

    浏览器不允许网页直接写相册（iOS / 安卓都一样），所以这是手机上唯一的合规做法 ——
    总比在详情页逐张点开强：这里一屏全看见，也能顺手挑不要的。
    """
    item = db.query(SupplierItem).filter(SupplierItem.item_id == item_id).first()
    if item is None:
        return templates.TemplateResponse(
            "goods_images.html",
            _common_ctx(request, "goods", db=db, active_sub="goods:list",
                        nav_crumb=["渔货栈", "货源广场", "未找到"],
                        not_found=True, item_id=item_id, item=None,
                        mains=[], details=[]),
            status_code=404,
        )
    mains, details = _item_image_sets(item)
    return templates.TemplateResponse(
        "goods_images.html",
        _common_ctx(
            request, "goods", db=db, active_sub="goods:list",
            nav_crumb=["渔货栈", "货源广场", "保存图片"],
            item=item, mains=mains, details=details,
            detail_url=self_url(f"/goods/detail/{item.item_id}"),
            zip_url=self_url(f"/goods/{item.item_id}/images.zip"),
        ),
    )


@router.get("/goods/detail/{item_id}/1688", response_class=HTMLResponse)
def goods_detail_legacy(item_id: str, request: Request) -> RedirectResponse:
    """老地址（带 /1688 后缀）301 到无后缀那个：详情页只有一版了。"""
    return RedirectResponse(url=self_url(f"/goods/detail/{item_id}"), status_code=301)


def _detail_sku_rows(
    db: Session,
    item: SupplierItem,
    rule: PriceRule | None,
    suggested: Decimal | float | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """SKU 明细（与经典版同一口径）+ 预占总数。

    **建议售价的口径（2026-10-11 用户报障后收口）**：优先用渔枢系统侧给每个规格
    填的建议售价，其次用商品级建议售价，最后才按加价规则现算。

    以前只按 `规格成本 × (1 + 加价率) + 运费` 现算，于是详情页出现两个数打架：
    顶上说「建议售价 ¥30.00」（商品级），规格行与「规格价区间」却是 ¥40.38（现算），
    用户看了认为价格不对。现在三处同源。
    """
    rows: list[dict[str, Any]] = []
    reserved_total = 0
    mappings = {
        m.ali_sku_id: m
        for m in db.execute(
            select(SkuMapping).where(SkuMapping.item_id == item.item_id)
        ).scalars().all()
    }
    for raw in (item.sku_list or {}).get("skus") or []:
        sku_id = str(raw.get("sku_id") or "")
        mapping = mappings.get(sku_id)
        reserved = int(mapping.reserved_stock or 0) if mapping else 0
        reserved_total += reserved
        stock = int(raw.get("stock") or 0)
        sku_cost = Decimal(str(raw.get("price") or 0))
        # 渔货价没定（渔枢系统侧留空）时不编价：价格给 None，页面显示「待定」而不是 ¥0.00
        priced = sku_cost > 0
        # 建议售价三档兜底：规格级（渔枢填的）→ 商品级 → 按加价规则现算
        hub_sale = Decimal(str(raw.get("sale_price") or 0))
        base_sale = Decimal(str(suggested or 0))
        if hub_sale > 0:
            sale_price: Decimal | None = hub_sale
        elif base_sale > 0:
            sale_price = base_sale
        elif priced:
            sale_price = (
                (sku_cost * (1 + Decimal(str(rule.margin_rate))) + rule.fixed_shipping)
                if rule is not None else sku_cost * Decimal("1.20")
            )
        else:
            sale_price = None
        rows.append({
            "sku_id": sku_id,
            "spec_text": raw.get("spec_text") or raw.get("spec") or "默认规格",
            "cost_price": float(sku_cost) if priced else None,
            "sale_price": float(sale_price) if sale_price is not None and sale_price > 0 else None,
            # 渔枢系统侧填的建议售价 / 市场价（留空为 None，页面不显示）
            "hub_sale_price": float(hub_sale) or None,
            "market_price": float(raw.get("market_price") or 0) or None,
            "stock": stock,
            "reserved": reserved,
            "available": max(0, stock - reserved),
            "code": raw.get("code"),
            "channel_sku_id": mapping.channel_sku_id if mapping else None,
        })
    return rows, reserved_total


def _similar_items(db: Session, item: SupplierItem, limit: int = 6) -> list[dict[str, Any]]:
    """同类货源推荐：同类目优先（渔枢系统上架的商品都带类目），不够再按最近同步补齐。"""
    rows: list[SupplierItem] = []
    if item.category_id:
        rows = list(
            db.execute(
                select(SupplierItem)
                .where(
                    SupplierItem.item_id != item.item_id,
                    SupplierItem.category_id == item.category_id,
                    # 下架的不再推荐（点了也只能看快照，铺不了货）
                    SupplierItem.is_listed.is_not(False),
                )
                .order_by(SupplierItem.last_synced_at.desc().nullslast())
                .limit(limit)
            ).scalars().all()
        )
    if len(rows) < limit:
        have = {r.item_id for r in rows}
        extra = db.execute(
            select(SupplierItem)
            .where(
                SupplierItem.item_id != item.item_id,
                SupplierItem.is_listed.is_not(False),
            )
            .order_by(SupplierItem.last_synced_at.desc().nullslast())
            .limit(limit * 4)
        ).scalars().all()
        for row in extra:
            if len(rows) >= limit:
                break
            if row.item_id in have:
                continue
            have.add(row.item_id)
            rows.append(row)

    out: list[dict[str, Any]] = []
    for row in rows:
        price = float(row.sale_price or 0) or float(row.fish_price or 0) or float(row.cost_price or 0)
        out.append({
            "item_id": row.item_id,
            "title": row.title,
            "main_image": row.main_image or "",
            "price": price,
        })
    return out


def _goods_images(item: SupplierItem) -> list[str]:
    """主图 + 图组去重保序（详情页图墙）。"""
    seen: set[str] = set()
    out: list[str] = []
    for url in [item.main_image, *(item.images or [])]:
        url = (url or "").strip()
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _selling_point_list(item: SupplierItem) -> list[str]:
    """卖点拆成一条条展示（换行优先；一行里用逗号 / 分号隔开的也拆开）。"""
    out: list[str] = []
    for line in (item.selling_points or "").splitlines():
        for piece in re.split(r"[,，;；]+", line):
            piece = piece.strip()
            if piece and piece not in out:
                out.append(piece)
    return out


# =====================================================================
# 6.5 采购域：采购车 / 结算 / 收银台 / 采购单 / 店铺商品池
#     商品详情页三个按钮（立即下单 / 加采购车 / 店铺铺货）的落点
#     流程照 1688：加采购车 → 采购车 → 结算页（地址/明细/留言/交易与配送/价格明细）
#                 → 提交订单 → 收银台付款 → 采购单（待发货 → 已发货 → 已完成）
# =====================================================================
def _purchase_owner(request: Request) -> tuple[str, str]:
    """采购域归属账号：(owner_key, 显示名)。

    各人各车 —— 用渔枢系统登录用户 id 做 key；没登录态时落 anonymous（页面仍能看，
    只是所有匿名访问共用一个车，正式环境都有登录态）。
    """
    hub_user = hub_shops.viewer(request)
    uid = getattr(hub_user, "id", None)
    # 显示名优先（渔枢「用户管理」里的姓名），没有才回退登录名 —— 采购单上看到的是人名字
    name = str(getattr(hub_user, "display_name", "") or "").strip()
    name = name or str(getattr(hub_user, "username", "") or "").strip()
    if uid:
        return f"u{uid}", (name or f"用户{uid}")
    return (name or "anonymous"), (name or "游客")


# 「铺货到店铺」的前置条件（2026-10-11 用户拍板）：名下得有一家**审核通过**的店铺。
# 没店铺 / 店铺还在待审 —— 按钮置灰，点了就提示这句原话（前端提示与后端兜底用同一句）。
SHOP_REQUIRED_MSG = "未添加店铺或店铺未通过审核，请联系你的带教老师！"


def _shops_approved(request: Request) -> int | None:
    """当前账号名下「审核通过」的店铺数。

    **没登录态返回 None**（不是学员忘了建店铺，而是脚本 / 接口调用）——调用方
    拿到 None 就别拦，免得把接口调用一起挡掉。
    """
    try:
        hub_user = hub_shops.viewer(request)
        uid = getattr(hub_user, "id", None)
        if not uid:
            return None
        return int(hub_shops.my_shop_stats(int(uid)).get("approved", 0) or 0)
    except Exception as exc:  # noqa: BLE001  读不到店铺数按 0 算，宁可拦住
        logger.debug("读店铺审核数失败：%s", exc)
        return 0


def _purchase_item(db: Session, item_id: str):
    """取货源并挡下架：返回 (item, 错误文案)。"""
    item = (
        db.execute(select(SupplierItem).where(SupplierItem.item_id == str(item_id or "")))
        .scalars()
        .first()
    )
    if item is None:
        return None, f"商品不存在：{item_id}"
    if getattr(item, "unlisted_at", None) and not item.is_listed:
        return None, "该商品已由渔枢系统从渔货栈下架，不能再采购"
    return item, ""


def _money_text(value) -> str:
    return f"{Decimal(str(value or 0)).quantize(Decimal('0.01')):.2f}"


def _cart_preview(db: Session, request: Request, limit: int = 5) -> dict[str, Any]:
    """顶栏采购车悬停弹框的数据（照 1688「最近加入的货品」）。

    - 取最近加入的前 `limit` 行（`list_cart` 已经按加入时间倒序），一行给：图 / 标题 /
      规格 / 单价 × 数量 / 单行删除用的 id；
    - 「比加入时降价」= 加购那一刻的价格（车里存的那份）比现在的价格贵出来的钱，
      同一款货降了就累计（`saved`），页面只在 > 0 时显示那一行提示；
    - 任何异常（采购表还没建 / 桥断）都返回空结构 —— 顶栏不该把页面拖挂。
    """
    empty: dict[str, Any] = {
        "lines": [], "kinds": 0, "total_qty": 0, "saved": "0.00", "has_more": 0,
    }
    try:
        owner, _name = _purchase_owner(request)
        rows = list_cart(db, owner)
        if not rows:
            return empty

        items = {
            item.item_id: item
            for item in db.execute(
                select(SupplierItem).where(
                    SupplierItem.item_id.in_([row.item_id for row in rows])
                )
            )
            .scalars()
            .all()
        }

        saved = Decimal("0.00")
        lines: list[dict[str, Any]] = []
        for row in rows[:limit]:
            item = items.get(row.item_id)
            now_price = Decimal(str(row.price or 0))
            if item is not None:
                # 现在的单价：按规格重新算一遍（车里那份是加购时的价）
                now_price = line_of(db, item, sku_id=row.sku_id or "", qty=1).price
            drop = Decimal(str(row.price or 0)) - now_price
            if drop > 0:
                saved += drop * int(row.qty or 0)
            lines.append({
                "id": row.id,
                "item_id": row.item_id,
                "title": row.title or row.item_id,
                "image": row.image or "",
                "spec_text": row.spec_text or "默认规格",
                "price": _money_text(row.price),
                "qty": int(row.qty or 0),
                "drop": _money_text(drop) if drop > 0 else "",
                "url": self_url(f"/goods/detail/{row.item_id}"),
            })

        return {
            "lines": lines,
            "kinds": len(rows),
            "total_qty": sum(int(row.qty or 0) for row in rows),
            "saved": _money_text(saved),
            "has_more": max(0, len(rows) - len(lines)),
        }
    except Exception:  # noqa: BLE001  预览是锦上添花，出错就空着
        return empty


def _summary_json(summary: dict) -> dict:
    return {key: (_money_text(val) if isinstance(val, Decimal) else val) for key, val in summary.items()}


def _last_receiver(db: Session, owner: str) -> dict[str, str]:
    """结算页地址回填：用这个账号最近一单的收货信息（省得每次重填）。"""
    row = (
        db.execute(
            select(StockOrder).where(StockOrder.owner == owner).order_by(StockOrder.id.desc())
        )
        .scalars()
        .first()
    )
    if row is None:
        return {"name": "", "phone": "", "address": ""}
    return {
        "name": row.receiver_name or "",
        "phone": row.receiver_phone or "",
        "address": row.receiver_address or "",
    }


def _freight_of_lines(db: Session, lines: list) -> Decimal:
    """整单运费：同一款货只算一次（与结算页显示一致）。"""
    seen: set[str] = set()
    total = Decimal("0.00")
    for line in lines:
        if line.item_id in seen:
            continue
        seen.add(line.item_id)
        item = (
            db.execute(select(SupplierItem).where(SupplierItem.item_id == line.item_id))
            .scalars()
            .first()
        )
        if item is not None:
            total += freight_of(item)
    return total.quantize(Decimal("0.01"))


# ---------------------------- 采购车 ----------------------------
@router.get("/cart", response_class=HTMLResponse)
def cart_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """采购车（「加采购车」的结果页）。"""
    owner, name = _purchase_owner(request)
    lines = list_cart(db, owner)
    try:
        addresses = [address_as_dict(row) for row in list_addresses(db, owner)]
    except Exception:  # noqa: BLE001  地址簿表还没建出来：车里照样能手填
        addresses = []
    ctx = _common_ctx(
        request, "purchase", db=db,
        active_sub="purchase:cart",
        nav_crumb=["交易", "采购车"],
        lines=lines, summary=cart_summary(db, lines), owner_name=name,
        # 按收货地址拆单的预览：几组就是几张子单（2026-10-12）
        groups=group_lines(db, lines), addresses=addresses,
    )
    return templates.TemplateResponse("cart.html", ctx)


@router.post("/cart/add", response_class=JSONResponse)
async def cart_add(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """加采购车：同一款货 + 同一规格只有一行，重复加购累加数量。"""
    payload = await _request_json(request)
    owner, _name = _purchase_owner(request)
    item, error = _purchase_item(db, str(payload.get("item_id") or ""))
    if item is None:
        return JSONResponse({"detail": error}, status_code=404)
    try:
        row = add_to_cart(
            db, owner, item,
            sku_id=str(payload.get("sku_id") or ""),
            qty=int(payload.get("qty") or 1),
            # 详情页「加采购车」可以顺手带一个收货地址；没带就先用默认地址填上
            receiver_name=str(payload.get("receiver_name") or ""),
            receiver_phone=str(payload.get("receiver_phone") or ""),
            receiver_address=str(payload.get("receiver_address") or ""),
        )
    except (ValueError, TypeError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    lines = list_cart(db, owner)
    summary = cart_summary(db, lines)
    return JSONResponse({
        "ok": True,
        "message": f"已加入采购车：{row.spec_text} × {row.qty}",
        "count": len(lines),
        "cart_url": self_url("/cart"),
        "summary": _summary_json(summary),
    })


@router.post("/cart/update", response_class=JSONResponse)
async def cart_update(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """改数量（夹到 [1, 可售]）。"""
    payload = await _request_json(request)
    owner, _name = _purchase_owner(request)
    row = update_qty(db, owner, int(payload.get("line_id") or 0), int(payload.get("qty") or 1))
    if row is None:
        return JSONResponse({"detail": "采购车里没有这一行"}, status_code=404)
    lines = list_cart(db, owner)
    return JSONResponse({
        "ok": True,
        "line": {"id": row.id, "qty": row.qty, "amount": _money_text(row.price * row.qty)},
        "summary": _summary_json(cart_summary(db, lines)),
        "count": len(lines),
    })


@router.post("/cart/remove", response_class=JSONResponse)
async def cart_remove(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """删行（支持多选删除）。"""
    payload = await _request_json(request)
    owner, _name = _purchase_owner(request)
    raw = payload.get("line_ids")
    if raw is None:
        raw = [payload.get("line_id")]
    ids = [int(x) for x in (raw or []) if str(x or "").strip().isdigit()]
    removed = remove_lines(db, owner, ids)
    lines = list_cart(db, owner)
    return JSONResponse({
        "ok": True,
        "removed": removed,
        "count": len(lines),
        "summary": _summary_json(cart_summary(db, lines)),
    })


@router.post("/cart/address", response_class=JSONResponse)
async def cart_set_address(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """给采购车里**这一行**单独设收货地址（2026-10-12：可以给不同的人各带一件）。

    地址相同的行结算时会并成同一张子单；不同的各开一张，钱一起付、货分着发。
    """
    payload = await _request_json(request)
    owner, _name = _purchase_owner(request)
    row = set_line_address(
        db, owner, int(payload.get("line_id") or 0),
        receiver_name=str(payload.get("receiver_name") or ""),
        receiver_phone=str(payload.get("receiver_phone") or ""),
        receiver_address=str(payload.get("receiver_address") or ""),
    )
    if row is None:
        return JSONResponse({"detail": "采购车里没有这一行"}, status_code=404)
    lines = list_cart(db, owner)
    return JSONResponse({
        "ok": True,
        "message": f"已设置收货地址：{row.receiver_name or '—'}",
        "line": {
            "id": row.id,
            "receiver_name": row.receiver_name or "",
            "receiver_phone": row.receiver_phone or "",
            "receiver_address": row.receiver_address or "",
        },
        "groups": len(group_lines(db, lines)),
        "summary": _summary_json(cart_summary(db, lines)),
    })


@router.post("/cart/clear", response_class=JSONResponse)
async def cart_clear(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """清空采购车。"""
    owner, _name = _purchase_owner(request)
    removed = remove_lines(db, owner, [row.id for row in list_cart(db, owner)])
    return JSONResponse({"ok": True, "removed": removed, "count": 0})


# ---------------------------- 结算 ----------------------------
@router.get("/checkout", response_class=HTMLResponse)
def checkout_page(
    request: Request,
    item_id: str = "",
    sku_id: str = "",
    qty: int = 1,
    ids: str = "",
    db: Session = Depends(get_db),
):
    """确认订单页（照 1688 结算页）。

    两种进法：详情页「立即下单」带 `item_id/sku_id/qty`；采购车「去结算」带 `ids=1,2,3`。
    """
    owner, name = _purchase_owner(request)
    lines: list[Any] = []
    groups: list[dict] = []
    source = "buy_now"
    if item_id:
        item, _error = _purchase_item(db, item_id)
        if item is None:
            return RedirectResponse(self_url("/cart"), status_code=303)
        lines = [line_of(db, item, sku_id=sku_id, qty=qty)]
    else:
        source = "cart"
        picked_ids = [int(x) for x in str(ids or "").split(",") if x.strip().isdigit()]
        rows = list_cart(db, owner)
        picked = [row for row in rows if not picked_ids or row.id in picked_ids]
        lines = [to_buy_line(row) for row in picked]
        # 按「收货地址 + 供应商」分组：一组就是一张子单（合并支付、拆单履约）
        groups = group_lines(db, picked)
        # 组里没填地址的，用默认地址 / 最近一单的地址先兜上（页面上直接就能改）
        fallback = _last_receiver(db, owner)
        if not str(fallback.get("name") or "").strip():
            try:
                addr = default_address(db, owner)
            except Exception:
                addr = None
            if addr is not None:
                fallback = {
                    "name": addr.receiver_name or "",
                    "phone": addr.phone or "",
                    "address": f"{addr.region or ''} {addr.address or ''}".strip(),
                }
        for group in groups:
            if not str(group["receiver"].get("address") or "").strip():
                group["receiver"] = dict(fallback)

    if not lines:
        return RedirectResponse(self_url("/cart"), status_code=303)

    goods_amount = sum((line.amount for line in lines), Decimal("0.00")).quantize(Decimal("0.01"))
    freight = _freight_of_lines(db, lines)
    # 收货地址：上一次下单的收货信息优先，没有就用地址簿里的默认地址（省得每次重填）
    receiver = _last_receiver(db, owner)
    if not str(receiver.get("name") or "").strip():
        try:
            default_addr = default_address(db, owner)
        except Exception:
            default_addr = None
        if default_addr is not None:
            receiver = {
                "name": default_addr.receiver_name or "",
                "phone": default_addr.phone or "",
                "address": f"{default_addr.region or ''} {default_addr.address or ''}".strip(),
            }
    try:
        addresses = [address_as_dict(row) for row in list_addresses(db, owner)]
    except Exception:  # 地址簿表还没建出来：结算页照旧能手工填
        addresses = []
    ctx = _common_ctx(
        request, "purchase", db=db,
        active_sub="purchase:cart",
        nav_crumb=["交易", "确认订单"],
        lines=lines, source=source, owner_name=name,
        item_id=item_id, sku_id=sku_id, qty=qty, line_ids=str(ids or ""),
        goods_amount=goods_amount, freight=freight, discount=Decimal("0.00"),
        pay_amount=(goods_amount + freight).quantize(Decimal("0.01")),
        total_qty=sum(line.qty for line in lines),
        receiver=receiver,
        addresses=addresses,
        groups=groups, group_count=len(groups),
    )
    return templates.TemplateResponse("checkout.html", ctx)


@router.post("/checkout/submit")
async def checkout_submit(request: Request, db: Session = Depends(get_db)):
    """提交订单 → 落到采购单（待支付）→ 进收银台。价格一律服务端重算，不信前端。"""
    owner, _name = _purchase_owner(request)
    form = await request.form()
    source = str(form.get("source") or "buy_now")
    lines: list[Any] = []
    ordered_line_ids: list[int] = []
    if source == "cart":
        picked_ids = [
            int(x) for x in str(form.get("line_ids") or "").split(",") if x.strip().isdigit()
        ]
        rows = list_cart(db, owner)
        picked = [row for row in rows if not picked_ids or row.id in picked_ids]
        lines = [to_buy_line(row) for row in picked]
        ordered_line_ids = [row.id for row in picked]
    else:
        item, _error = _purchase_item(db, str(form.get("item_id") or ""))
        if item is not None:
            lines = [
                line_of(
                    db, item,
                    sku_id=str(form.get("sku_id") or ""),
                    qty=int(form.get("qty") or 1),
                )
            ]
    if not lines:
        return RedirectResponse(self_url("/cart"), status_code=303)

    # 收货信息：表单里填了就以表单为准；勾了地址簿里的某条（address_id）就按那条补
    receiver_name = str(form.get("receiver_name") or "")
    receiver_phone = str(form.get("receiver_phone") or "")
    receiver_address = str(form.get("receiver_address") or "")
    addr_id = str(form.get("address_id") or "").strip()
    if addr_id.isdigit():
        picked = get_address(db, owner, int(addr_id))
        if picked is not None:
            receiver_name = receiver_name or (picked.receiver_name or "")
            receiver_phone = receiver_phone or (picked.phone or "")
            receiver_address = receiver_address or (
                f"{picked.region or ''} {picked.address or ''}".strip()
            )

    # 采购车里给不同的人各带了一件 → 按收货地址拆成几张子单（钱一起付、货分着发）
    groups: list[dict] = []
    if source == "cart":
        for group in group_lines(db, picked):
            key = str(group.get("key") or "")
            name = str(form.get(f"receiver_name__{key}") or "")
            phone = str(form.get(f"receiver_phone__{key}") or "")
            address = str(form.get(f"receiver_address__{key}") or "")
            group["receiver"] = {
                "name": name or receiver_name,
                "phone": phone or receiver_phone,
                "address": address or receiver_address,
            }
            # 分组里存的是采购车行，下单要的是结算明细行
            group["lines"] = [to_buy_line(row) for row in group["lines"]]
            groups.append(group)

    try:
        if len(groups) > 1:
            order = create_split_order(
                db, owner, groups,
                remark=str(form.get("remark") or ""),
                # 默认「带教代收」（2026-10-12：担保交易已从结算页撤掉）
                trade_type=str(form.get("trade_type") or "agent"),
                delivery_type=str(form.get("delivery_type") or "express"),
                source=source,
            )
        else:
            order = create_order(
                db, owner, lines,
                receiver_name=receiver_name,
                receiver_phone=receiver_phone,
                receiver_address=receiver_address,
                remark=str(form.get("remark") or ""),
                # 默认「带教代收」（2026-10-12：担保交易已从结算页撤掉）
                trade_type=str(form.get("trade_type") or "agent"),
                delivery_type=str(form.get("delivery_type") or "express"),
                source=source,
                freight=_freight_of_lines(db, lines),
            )
    except ValueError:
        return RedirectResponse(self_url("/cart"), status_code=303)

    # 已经下单的行走采购单，采购车里把它们清掉（本次没勾的留着）
    if ordered_line_ids:
        remove_lines(db, owner, ordered_line_ids)
    return RedirectResponse(self_url(f"/pay/{order.order_no}"), status_code=303)


# ---------------------------- 收银台 ----------------------------
@router.get("/pay/{order_no}", response_class=HTMLResponse)
def pay_page(request: Request, order_no: str, db: Session = Depends(get_db)):
    """收银台（模拟支付通道：余额 / 支付宝 / 微信）。"""
    owner, _name = _purchase_owner(request)
    order = get_stock_order(db, order_no, owner)
    if order is None:
        return RedirectResponse(self_url("/purchases"), status_code=303)
    if order.status != "pending_pay":
        return RedirectResponse(self_url(f"/purchases/{order.order_no}"), status_code=303)
    subs = stock_sub_orders(db, order)
    ctx = _common_ctx(
        request, "purchase", db=db,
        active_sub="purchase:orders",
        nav_crumb=["交易", "采购单", "收银台"],
        order=order, items=get_stock_items(db, order), subs=subs,
        methods=PAY_METHODS_FOR_VIEW,
        owner_name=_name,
        headline="订单提交成功，请支付",
    )
    return templates.TemplateResponse("pay.html", ctx)


@router.post("/pay/{order_no}")
async def pay_submit(request: Request, order_no: str, db: Session = Depends(get_db)):
    """模拟支付：成功后订单转「待发货」。"""
    owner, _name = _purchase_owner(request)
    order = get_stock_order(db, order_no, owner)
    if order is None:
        return RedirectResponse(self_url("/purchases"), status_code=303)
    form = await request.form()
    ok, _message = pay_order(db, order, str(form.get("method") or ""))
    if not ok:
        return RedirectResponse(self_url(f"/pay/{order.order_no}"), status_code=303)
    return RedirectResponse(self_url(f"/purchases/{order.order_no}?paid=1"), status_code=303)


# ---------------------------- 采购单 ----------------------------
@router.get("/purchases", response_class=HTMLResponse)
def purchases_page(
    request: Request, status: str = "", db: Session = Depends(get_db)
) -> HTMLResponse:
    """采购单列表（渔货栈向平台采购的订单）。"""
    owner, name = _purchase_owner(request)
    orders = list_stock_orders(db, owner, status=status)
    # 合并单：把子单一起取出来算「还有几张在路上」，列表上只显示母单这一行
    subs_map = {row.order_no: stock_sub_orders(db, row) for row in orders}
    ctx = _common_ctx(
        request, "purchase", db=db,
        subs_map=subs_map,
        active_sub="purchase:orders",
        nav_crumb=["交易", "采购单"],
        orders=orders, status=status, by_status=stock_status_counts(db, owner),
        owner_name=name,
    )
    return templates.TemplateResponse("purchases.html", ctx)


@router.get("/purchases/{order_no}", response_class=HTMLResponse)
def purchase_detail(request: Request, order_no: str, db: Session = Depends(get_db)):
    """采购单详情：单据信息 + 商品明细 + 金额明细 + 收货信息 + 进度 + 动作。"""
    owner, name = _purchase_owner(request)
    order = get_stock_order(db, order_no, owner)
    if order is None:
        return RedirectResponse(self_url("/purchases"), status_code=303)
    subs = stock_sub_orders(db, order)
    parent = get_stock_order(db, str(order.parent_order_no or ""), owner) if order.parent_order_no else None
    ctx = _common_ctx(
        request, "purchase", db=db,
        active_sub="purchase:orders",
        nav_crumb=["交易", "采购单", order.order_no],
        order=order, items=get_stock_items(db, order),
        subs=subs, parent=parent,
        # 子单的商品明细（母单自己没有明细行，都挂在子单上）
        sub_items_map={sub.order_no: get_stock_items(db, sub) for sub in subs},
        progress=progress_of(order, subs), owner_name=name,
    )
    return templates.TemplateResponse("purchase_detail.html", ctx)


@router.post("/purchases/{order_no}/actions")
async def purchase_action(request: Request, order_no: str, db: Session = Depends(get_db)):
    """采购单动作：cancel 取消 / ship 标记发货 / complete 确认收货。"""
    owner, _name = _purchase_owner(request)
    order = get_stock_order(db, order_no, owner)
    if order is None:
        return RedirectResponse(self_url("/purchases"), status_code=303)
    form = await request.form()
    action = str(form.get("action") or "").strip().lower()
    if action == "cancel":
        cancel_order(db, order, str(form.get("reason") or ""))
    elif action == "confirm_pay":
        confirm_pay(db, order)
    elif action == "ship":
        mark_shipped(
            db, order,
            company=str(form.get("company") or ""),
            shipping_no=str(form.get("shipping_no") or ""),
        )
    elif action == "complete":
        complete_order(db, order)
    return RedirectResponse(self_url(f"/purchases/{order.order_no}"), status_code=303)


# ---------------------------- 店铺商品池 ----------------------------
@router.post("/goods/{item_id}/pool", response_class=JSONResponse)
async def goods_add_to_pool(request: Request, item_id: str, db: Session = Depends(get_db)) -> JSONResponse:
    """店铺铺货：把货源加进店铺商品池（个人闲置店铺没 PC 后台，用池子里的素材去手机端发）。"""
    payload = await _request_json(request)
    owner, _name = _purchase_owner(request)
    item, error = _purchase_item(db, item_id)
    if item is None:
        return JSONResponse({"detail": error}, status_code=404)

    # 没店铺 / 店铺没过审不给铺货（2026-10-11 用户拍板）：前端已经把按钮置灰了，
    # 这里是兜底（老页面 / 直接调接口）。没登录态（None）不拦，免得挡住脚本。
    approved = _shops_approved(request)
    if approved is not None and approved <= 0:
        return JSONResponse({"detail": SHOP_REQUIRED_MSG}, status_code=400)

    shop_id = str(payload.get("shop_id") or "").strip()
    shop_name = str(payload.get("shop_name") or "").strip()
    if not shop_id:
        default_shop = _default_shop(request)
        shop_id = default_shop.get("id", "")
        shop_name = shop_name or default_shop.get("name", "")

    row = add_to_pool(
        db, owner, item,
        shop_id=shop_id, shop_name=shop_name or None,
        suggested_price=_expected_price(db, item),
    )
    return JSONResponse({
        "ok": True,
        "message": f"已加入店铺商品池{'（' + shop_name + '）' if shop_name else ''}",
        "pool": {
            "id": row.id,
            "shop_id": row.shop_id,
            "shop_name": row.shop_name or "",
            "download_count": row.download_count or 0,
        },
        "pool_url": self_url("/shop-goods"),
        "materials_url": self_url(f"/shop-goods/{row.id}/materials.zip"),
    })


def _expected_price(db: Session, item) -> Decimal:
    """池子里显示的建议售价（与详情页同一个口径）。"""
    return _suggested_price_of(db, item)


def _pool_item_for_listing(db: Session, item, request: Request) -> dict:
    """「上架到店铺」顺带入店铺商品池（两套铺货记录互通）。

    返回池子那条记录的简要信息（失败返回 {}）—— 池子补不上不影响铺货记录本身。
    """
    try:
        owner, _name = _purchase_owner(request)
        default_shop = _default_shop(request)
        row = add_to_pool(
            db,
            owner,
            item,
            shop_id=default_shop.get("id", ""),
            shop_name=default_shop.get("name") or None,
            suggested_price=_expected_price(db, item),
        )
        return {"id": row.id, "shop_id": row.shop_id, "shop_name": row.shop_name or ""}
    except Exception as exc:  # noqa: BLE001  入池失败不影响铺货记录
        logger.warning("商品 %s 上架到店铺时入池失败：%s", getattr(item, "item_id", "-"), exc)
        return {}


def _shop_field(shop, key: str, default: str = "") -> str:
    if isinstance(shop, dict):
        return str(shop.get(key) or default)
    return str(getattr(shop, key, default) or default)


def _default_shop(request: Request) -> dict[str, str]:
    """当前账号名下第一个店铺（没填店铺时的默认落点）。"""
    try:
        hub_user = hub_shops.viewer(request)
        shops = hub_shops.my_shops(getattr(hub_user, "id", None)) or []
    except Exception:
        return {"id": "", "name": ""}
    if not shops:
        return {"id": "", "name": ""}
    first = shops[0]
    return {"id": _shop_field(first, "id"), "name": _shop_field(first, "name")}


def _my_shop_options(request: Request) -> list[dict[str, str]]:
    try:
        hub_user = hub_shops.viewer(request)
        shops = hub_shops.my_shops(getattr(hub_user, "id", None)) or []
    except Exception:
        return []
    return [{"id": _shop_field(s, "id"), "name": _shop_field(s, "name")} for s in shops]


def _pooled_of(request: Request, db: Session, item_id: str):
    """这款货在当前账号的店铺商品池里吗（详情页右列的标记用）。"""
    try:
        owner, _name = _purchase_owner(request)
        return (
            db.execute(
                select(ShopGoods).where(
                    ShopGoods.owner == owner,
                    ShopGoods.item_id == item_id,
                    ShopGoods.status == "pooled",
                )
            )
            .scalars()
            .first()
        )
    except Exception:
        return None


def _same_view(request: Request, view: str) -> str:
    """「我的店铺商品」的列表 / 卡片切换链接：当前筛选（店铺）不丢。"""
    params = {k: v for k, v in request.query_params.items() if str(v or "") != ""}
    params["view"] = view
    return f"{request.url.path}?{urlencode(params)}"


@router.get("/shop-goods", response_class=HTMLResponse)
def shop_goods_page(
    request: Request, shop_id: str = "", view: str = "", db: Session = Depends(get_db)
) -> HTMLResponse:
    """我的店铺商品（2026-10-11 由「店铺商品池」改名）：选品进来的货，可打包下载素材。

    view=list / grid 决定列表还是卡片；不传按卡片（JS 会按上次的喜好自动切一次）。
    """
    owner, name = _purchase_owner(request)
    shops = _my_shop_options(request)
    rows = list_pool(db, owner, shop_id=shop_id or None)
    ctx = _common_ctx(
        request, "purchase", db=db,
        active_sub="purchase:pool",
        nav_crumb=["店铺", "我的店铺商品"],
        rows=rows, shops=shops, current_shop=shop_id, owner_name=name,
        summary=pool_summary(db, owner),
        # 列表 / 卡片两视图
        view="list" if str(view or "").strip() == "list" else "grid",
        view_urls={
            "grid": _same_view(request, "grid"),
            "list": _same_view(request, "list"),
        },
    )
    return templates.TemplateResponse("shop_goods.html", ctx)


@router.post("/shop-goods/{pool_id}/remove", response_class=JSONResponse)
async def shop_goods_remove(request: Request, pool_id: int, db: Session = Depends(get_db)) -> JSONResponse:
    """从商品池移出。"""
    owner, _name = _purchase_owner(request)
    ok = remove_from_pool(db, owner, pool_id)
    if not ok:
        return JSONResponse({"detail": "池子里没有这条"}, status_code=404)
    return JSONResponse({"ok": True, "pool_url": self_url("/shop-goods")})


@router.get("/shop-goods/{pool_id}/materials.zip")
def shop_goods_materials(request: Request, pool_id: int, db: Session = Depends(get_db)):
    """下载素材包（主图 / 详情图 / 文案 / 规格价格表）。"""
    owner, _name = _purchase_owner(request)
    row = None
    try:
        row = (
            db.execute(
                select(ShopGoods).where(ShopGoods.id == int(pool_id), ShopGoods.owner == owner)
            )
            .scalars()
            .first()
        )
    except Exception:
        row = None
    if row is None:
        return RedirectResponse(self_url("/shop-goods"), status_code=303)
    try:
        data, filename = materials_zip(db, row)
    except ValueError as exc:
        return HTMLResponse(f"素材打包失败：{exc}", status_code=404)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"materials-{row.id}.zip\"; "
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


# =====================================================================
# 6.6 个人中心 · 地址管理 /addresses（2026-10-03）
#     收货地址簿：结算页一键带入，下单时落到采购单的 receiver_* 上。
# =====================================================================
@router.get("/addresses", response_class=HTMLResponse)
def addresses_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """地址管理：这个账号的收货地址（默认那条排最前）。"""
    owner, name = _purchase_owner(request)
    try:
        rows = list_addresses(db, owner)
    except Exception:  # 表还没建出来：给个空列表，页面照旧能开
        rows = []
    ctx = _common_ctx(
        request, "purchase", db=db,
        active_sub="me:addresses",
        nav_crumb=["我的", "地址管理"],
        addresses=[address_as_dict(r) for r in rows], owner_name=name,
    )
    return templates.TemplateResponse("addresses.html", ctx)


@router.post("/addresses")
async def addresses_create(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    """新增地址：收货人 / 电话 / 详细地址必填。"""
    owner, _name = _purchase_owner(request)
    form = await request.form()
    data = {key: str(form.get(key) or "").strip() for key in
            ("receiver_name", "phone", "region", "address", "tag")}
    missing = [k for k in ("receiver_name", "phone", "address") if not data[k]]
    if missing:
        label = {"receiver_name": "收货人", "phone": "手机号", "address": "详细地址"}[missing[0]]
        return _redirect("/addresses", f"{label}不能为空", "warn")
    add_address(
        db, owner,
        is_default=str(form.get("is_default") or "") in ("1", "on", "true"),
        **data,
    )
    return _redirect("/addresses", "地址已保存")


@router.post("/addresses/{address_id}/update")
async def addresses_update(
    request: Request, address_id: int, db: Session = Depends(get_db)
) -> RedirectResponse:
    """改一条地址。"""
    owner, _name = _purchase_owner(request)
    form = await request.form()
    data = {key: str(form.get(key) or "").strip() for key in
            ("receiver_name", "phone", "region", "address", "tag")}
    row = update_address(db, owner, address_id, **data)
    if row is None:
        return _redirect("/addresses", "地址不存在", "warn")
    return _redirect("/addresses", "地址已更新")


@router.post("/addresses/{address_id}/default")
async def addresses_set_default(
    request: Request, address_id: int, db: Session = Depends(get_db)
) -> RedirectResponse:
    """设为默认地址（其它条自动取消默认）。"""
    owner, _name = _purchase_owner(request)
    if set_default(db, owner, address_id) is None:
        return _redirect("/addresses", "地址不存在", "warn")
    return _redirect("/addresses", "已设为默认地址")


@router.post("/addresses/{address_id}/delete")
async def addresses_delete(
    request: Request, address_id: int, db: Session = Depends(get_db)
) -> RedirectResponse:
    """删一条地址（删掉默认时，剩下第一条顶上）。"""
    owner, _name = _purchase_owner(request)
    if not remove_address(db, owner, address_id):
        return _redirect("/addresses", "地址不存在", "warn")
    return _redirect("/addresses", "地址已删除")


@router.get("/addresses.json", response_class=JSONResponse)
def addresses_api(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """地址簿（JSON）：结算页「从地址簿选择」用。"""
    owner, _name = _purchase_owner(request)
    try:
        rows = list_addresses(db, owner)
    except Exception:
        rows = []
    return JSONResponse({"ok": True, "addresses": [address_as_dict(r) for r in rows]})


@router.post("/addresses/parse", response_class=JSONResponse)
async def addresses_parse(request: Request) -> JSONResponse:
    """粘贴一整段收货信息 → 拆成 收货人 / 电话 / 省市区 / 详细地址（只解析，不落库）。"""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001  —— 表单提交也认
        form = await request.form()
        payload = {"text": str(form.get("text") or "")}
    text = str((payload or {}).get("text") or "")
    return JSONResponse(parse_address_text(text))


# =====================================================================
# 7. 库存 /inventory
# =====================================================================
@router.get("/inventory", response_class=HTMLResponse)
def inventory(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    rows = db.execute(
        select(
            SkuMapping.id,
            SkuMapping.item_id,
            SkuMapping.ali_sku_id,
            SkuMapping.channel,
            SkuMapping.channel_sku_id,
            SkuMapping.reserved_stock,
            SupplierItem.title,
            SupplierItem.stock,
            SupplierItem.is_oversold,
        )
        .join(SupplierItem, SupplierItem.item_id == SkuMapping.item_id)
        .order_by(SkuMapping.id)
        .limit(200)
    ).all()

    mappings_view = []
    for r in rows:
        stock = r.stock or 0
        reserved = r.reserved_stock or 0
        available = max(0, stock - reserved)
        oversold_ratio = (reserved / stock) if stock else 0
        mappings_view.append({
            "id": r.id,
            "item_id": r.item_id,
            "ali_sku_id": r.ali_sku_id,
            "channel": r.channel,
            "channel_sku_id": r.channel_sku_id,
            "title": r.title,
            "stock": stock,
            "reserved": reserved,
            "available": available,
            "oversold": r.is_oversold,
            "oversold_ratio": oversold_ratio,
        })

    total_stock = sum(m["stock"] for m in mappings_view)
    total_reserved = sum(m["reserved"] for m in mappings_view)
    total_available = sum(m["available"] for m in mappings_view)
    oversold_count = sum(1 for m in mappings_view if m["oversold"])

    ctx = _common_ctx(
        request, "inventory", db=db,
        nav_crumb=["供应链", "库存预占"],
        mappings=mappings_view,
        kpi={
            "total_stock": total_stock,
            "total_reserved": total_reserved,
            "total_available": total_available,
            "oversold_count": oversold_count,
            "sku_count": len(mappings_view),
        },
    )
    return templates.TemplateResponse("inventory.html", ctx)


# =====================================================================
# 8. 利润 /pricing
# =====================================================================
@router.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    by_status = db.execute(
        select(
            PurchaseOrder.status,
            sql_func.coalesce(sql_func.sum(_profit_expr()), 0).label("profit"),
            sql_func.count(PurchaseOrder.id).label("n"),
            sql_func.coalesce(sql_func.sum(PurchaseOrder.qty), 0).label("qty"),
        )
        .select_from(PurchaseOrder)
        .join(
            DownstreamOrder,
            DownstreamOrder.downstream_order_id == PurchaseOrder.downstream_order_id,
        )
        .outerjoin(SupplierItem, SupplierItem.item_id == PurchaseOrder.ali_item_id)
        .group_by(PurchaseOrder.status)
        .order_by(PurchaseOrder.status)
    ).all()
    by_status_view = [
        {"status": s, "profit": float(p), "n": n, "qty": q}
        for s, p, n, q in by_status
    ]

    top_skus = db.execute(
        select(
            SupplierItem.item_id,
            SupplierItem.title,
            sql_func.coalesce(sql_func.sum(_profit_expr()), 0).label("profit"),
            sql_func.count(PurchaseOrder.id).label("n"),
        )
        .select_from(PurchaseOrder)
        .join(
            DownstreamOrder,
            DownstreamOrder.downstream_order_id == PurchaseOrder.downstream_order_id,
        )
        .outerjoin(SupplierItem, SupplierItem.item_id == PurchaseOrder.ali_item_id)
        .group_by(SupplierItem.item_id, SupplierItem.title)
        .order_by(sql_func.sum(_profit_expr()).desc())
        .limit(10)
    ).all()
    top_skus_view = [
        {"item_id": i, "title": t, "profit": float(p), "n": n}
        for i, t, p, n in top_skus
    ]

    total_profit = sum(x["profit"] for x in by_status_view)
    rules = db.execute(
        select(PriceRule).order_by(PriceRule.priority).limit(20)
    ).scalars().all()

    ctx = _common_ctx(
        request, "pricing", db=db,
        nav_crumb=["供应链", "利润报表"],
        by_status=by_status_view, top_skus=top_skus_view,
        rules=rules, total_profit=total_profit,
    )
    return templates.TemplateResponse("pricing.html", ctx)


# =====================================================================
# 10. 日志 /logs
# =====================================================================
@router.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    type: str | None = None,
    resolved: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    stmt = select(ExceptionLog).order_by(ExceptionLog.id.desc()).limit(200)
    if type:
        stmt = stmt.where(ExceptionLog.type == type)
    if resolved == "true":
        stmt = stmt.where(ExceptionLog.resolved.is_(True))
    elif resolved == "false":
        stmt = stmt.where(ExceptionLog.resolved.is_(False))
    rows = db.execute(stmt).scalars().all()
    type_options = [
        r[0] for r in db.execute(
            select(ExceptionLog.type).distinct().order_by(ExceptionLog.type)
        ).all()
    ]
    counts = dict(
        db.execute(
            select(ExceptionLog.resolved, sql_func.count(ExceptionLog.id)).group_by(
                ExceptionLog.resolved
            )
        ).all()
    )
    ctx = _common_ctx(
        request, "logs", db=db,
        nav_crumb=["系统", "异常日志"],
        logs=rows, type_options=type_options,
        type_filter=type, resolved_filter=resolved,
        counts={"resolved": counts.get(True, 0), "unresolved": counts.get(False, 0)},
    )
    return templates.TemplateResponse("logs.html", ctx)


# =====================================================================
# 10b. 店铺管理 /shops（我自己的闲鱼自营账号）
#
# 店铺档案只有一份，存在渔枢系统的 shops 表：这里填完直接写过去，
# 渔枢系统端「店铺管理」能看到同一条记录并审核，只有「已通过」的才在渔货栈生效。
# 见 src/web/hub_shops.py（渔货栈单独部署时那里会自动降级，页面给提示）。
# =====================================================================
# 表单字段：(字段名, 中文标签, 占位提示) —— 与渔枢系统端店铺档案保持一致
_SHOP_FORM_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("name", "账号名称 *", "闲鱼昵称，与订单的「出货闲鱼号」一致"),
    ("code", "账号唯一编号", "如 XYSK0001（留空自动生成）"),
    ("operator", "账号运营人", "飞书里的发送人"),
    ("owner", "账号所属人", "如：久哥"),
    ("device", "绑定手机设备", "如：iPhone 13 / 红米 K60"),
    ("category", "经营细分类目", "家电 / 鞋类 / 数码…"),
    ("phone", "绑定手机号", ""),
    ("status", "账号状态", "正常运营"),
    ("supply", "类目 / 供应链", "如：京东家电类"),
    ("alipay", "支付宝绑定账号", ""),
    ("payee_name", "回款归属人", ""),
    ("credit_score", "芝麻信用分", ""),
    ("real_name", "实名认证", "认证人姓名"),
    ("xianqi", "闲气值", ""),
    ("yuli", "鱼力值", ""),
    ("violation", "有无违规", "无 / 有：xxx"),
    ("wechat", "微信账号", ""),
    ("account_age", "新老账号", "新账号 / 老账号"),
    ("note", "描述", "备注信息"),
    # 这两个跟渔枢系统店铺档案保持一致：
    # 店铺类型是下拉（模板特判渲染 select，只认三个固定值），平台技术服务费是百分比
    ("shop_type", "店铺类型", "个人闲置店铺"),
    ("service_fee_rate", "平台技术服务费（%）", "0.6"),
    # 学员建档也要填的：闲鱼会员名、本人联系电话、实名认证是否完成（勾选框）
    ("member_name", "会员名", "闲鱼「账号与安全」里的会员名"),
    ("contact_phone", "联系电话", "联系运营人本人用的号（不是闲鱼绑定手机号）"),
    ("real_name_verified", "实名认证是否完成", ""),
)

# 学员账号建档只填这 8 项（2026-10-05）：自营账号组才填上面那份完整档案
_SHOP_STUDENT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("name", "店铺名称 *", "闲鱼上的店铺名，与订单的「出货闲鱼号」一致"),
    ("member_name", "会员名 *", "闲鱼「账号与安全」里的会员名"),
    ("phone", "手机号 *", "闲鱼账号绑定的手机号"),
    ("real_name_verified", "实名认证是否完成", ""),
    ("operator", "实际运营人姓名 *", "谁在实际运营这个号"),
    ("contact_phone", "联系电话", "联系运营人本人用的号"),
    ("wechat", "微信", ""),
    ("category", "希望经营类目", "家电 / 鞋类 / 数码…"),
)

_SHOP_FORM_GROUPS_STUDENT: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "店铺与账号", "bi-shop", "店铺名称、闲鱼会员名，以及这个闲鱼号绑定的手机号",
        ("name", "member_name", "phone", "real_name_verified"),
    ),
    (
        "运营人与联系", "bi-person-lines-fill", "谁在实际运营这个号、怎么联系上他、想做哪个类目",
        ("operator", "contact_phone", "wechat", "category"),
    ),
)

# 新建 / 编辑店铺页的字段分组（2026-10-03：从「一页塞 21 个框」改成按业务分组）
# 每项：(分组标题, 图标, 一句话说明, 字段名…)
_SHOP_FORM_GROUPS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "账号与店铺", "bi-shop", "闲鱼账号的身份信息，账号名称要与订单里的「出货闲鱼号」一致",
        ("name", "code", "shop_type", "status"),
    ),
    (
        "运营与人员", "bi-people", "谁在运营这个号、绑的什么设备、新号还是老号",
        ("operator", "owner", "device", "account_age", "real_name"),
    ),
    (
        "经营与信用", "bi-graph-up-arrow", "卖什么、货从哪来，以及闲鱼侧的信用与违规情况",
        ("category", "supply", "credit_score", "xianqi", "yuli", "violation"),
    ),
    (
        "资金与回款", "bi-cash-coin", "支付宝绑定、回款归属人，以及平台技术服务费费率（订单毛利按它扣）",
        ("alipay", "payee_name", "service_fee_rate"),
    ),
    (
        "联系与备注", "bi-chat-dots", "手机号 / 微信便于对接，描述留给运营备注",
        ("phone", "wechat", "note"),
    ),
)

# 新建店铺（2026-10-11 用户定的口径）：只填这 6 项 ——
#   姓名 / 手机号码 / 店铺名称（闲鱼昵称）/ 会员名 / 闲鱼手机号 / 店铺类型（个人店铺 · 鱼小铺）
# 其余那十几项资料（设备 / 支付宝 / 信用分…）留给渔枢系统端在审核时补，不拦着人开店。
_SHOP_NEW_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("name", "店铺名称（闲鱼昵称）*", "闲鱼上的昵称，与订单的「出货闲鱼号」一致"),
    ("member_name", "会员名 *", "闲鱼「我的 - 设置 - 账号与安全」里的会员名"),
    ("phone", "闲鱼手机号 *", "此手机为闲鱼App的我的-设置-账号与安全的手机号"),
    ("operator", "姓名 *", "谁在实际运营这个号（真名）"),
    ("contact_phone", "手机号码 *", "联系你本人用的手机号"),
    ("shop_type", "店铺类型", ""),
)

_SHOP_FORM_GROUPS_NEW: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "店铺与账号", "bi-shop", "闲鱼昵称、会员名，以及这个闲鱼号绑的手机号",
        ("name", "member_name", "phone"),
    ),
    (
        "运营与人员", "bi-person-lines-fill",
        "谁在实际运营、怎么联系上他；填了手机号会自动带进上面那格（可以再改）",
        ("operator", "contact_phone"),
    ),
    ("店铺类型", "bi-award", "个人店铺 / 鱼小铺", ("shop_type",)),
)

_SHOP_AUDIT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("", "全部"),
    ("pending", "待审核"),
    ("approved", "已通过（生效）"),
    ("rejected", "已驳回"),
)


def _shop_form_ctx(
    request, db, *, editing=None, msg: str = "", kind: str = "", student: bool = False
) -> dict:
    """新建 / 编辑店铺页的上下文（同一份分组表单，靠 editing 决定预填与提交地址）。

    student=True（学员账号）时只摆那 8 个字段；自营账号组 / 管理员用完整档案那份。
    """
    if editing is None:
        # 新建：只填那 6 项（2026-10-11），店铺类型也只给「个人店铺 / 鱼小铺」
        fields, groups = _SHOP_NEW_FIELDS, _SHOP_FORM_GROUPS_NEW
        type_options = tuple(name for name, _hub in hub_shops.SELF_SHOP_TYPES)
        type_default = type_options[0] if type_options else hub_shops.SHOP_TYPE_DEFAULT
    else:
        fields = _SHOP_STUDENT_FIELDS if student else _SHOP_FORM_FIELDS
        groups = _SHOP_FORM_GROUPS_STUDENT if student else _SHOP_FORM_GROUPS
        # 编辑页是完整档案：店铺类型用渔枢系统那三个固定值
        type_options, type_default = hub_shops.SHOP_TYPE_CHOICES, hub_shops.SHOP_TYPE_DEFAULT
    return _common_ctx(
        request, "shops", db=db,
        nav_crumb=["店铺", "店铺管理"],
        editing=editing,
        msg=msg, kind=kind,
        student=student,
        shop_groups=groups,
        shop_field_map={field: (label, ph) for field, label, ph in fields},
        # 店铺类型下拉的选项：新建只有「个人店铺 / 鱼小铺」，编辑页是渔枢系统那三个固定值
        shop_type_options=type_options,
        service_fee_rate_default=hub_shops.SERVICE_FEE_RATE_DEFAULT,
        shop_type_default=type_default,
        hub_available=hub_shops.available(),
        linked=hub_shops.viewer(request) is not None,
    )


def _shop_link_note(kind: str, user_id: int, shop_id: int, claimed: bool = False) -> None:
    """手动添加 / 认领 / 解除关联后补一条台账（见 `web/shop_sync.py`）。

    台账只用于「自动同步认得出来这家店是怎么来的」，写失败不影响主流程 ——
    店铺本身已经在渔枢系统的 `shops` 表里了，这里只是留个痕。
    """
    try:
        from . import shop_sync  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    try:
        if kind == "unlink":
            shop_sync.note_unlink(user_id, shop_id)
        else:
            shop_sync.note_manual(user_id, shop_id, claimed)
    except Exception as exc:  # noqa: BLE001
        logger.debug("店铺关联台账写入失败：%s", exc)


@router.get("/shops", response_class=HTMLResponse)
def shops(
    request: Request,
    q: str = "",
    audit: str = "",
    edit: int = 0,
    msg: str = "",
    kind: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """我的店铺（列表页）：只看自己提交的（渔枢系统端审核通过的才生效）。

    2026-10-03 起新建 / 编辑各自独立成页（/shops/new、/shops/{id}/edit），
    列表页只剩筛选 + 表格；老链接 /shops?edit=N 301 到编辑页。
    """
    if edit:
        return RedirectResponse(url=self_url(f"/shops/{edit}/edit"), status_code=301)
    hub = hub_shops.viewer(request)
    owner_id = getattr(hub, "id", 0) or 0
    # 先同步再查列表：渔枢系统里新带过来的店铺，这一屏就能看到（见 web/shop_sync.py）
    _ensure_shop_synced(hub)
    rows = hub_shops.my_shops(owner_id, q=q, audit=audit) if owner_id else []
    ctx = _common_ctx(
        request, "shops", db=db,
        nav_crumb=["店铺", "店铺管理"],
        rows=rows,
        stats=hub_shops.my_shop_stats(owner_id),
        q=q, audit=audit, msg=msg, kind=kind,
        linked=hub is not None,
        hub_available=hub_shops.available(),
        audit_options=_SHOP_AUDIT_OPTIONS,
    )
    return templates.TemplateResponse("shops.html", ctx)


@router.get("/shops/new", response_class=HTMLResponse)
def shop_new(
    request: Request, msg: str = "", kind: str = "", db: Session = Depends(get_db)
) -> HTMLResponse:
    """新建店铺页：自营账号组填完整档案，学员只填那 8 项，都按业务分组摆。"""
    student = hub_shops.is_student_form(hub_shops.viewer(request))
    ctx = _shop_form_ctx(request, db, editing=None, msg=msg, kind=kind, student=student)
    return templates.TemplateResponse("shop_form.html", ctx)


@router.get("/shops/{shop_id}/edit", response_class=HTMLResponse)
def shop_edit_page(
    shop_id: int, request: Request, msg: str = "", kind: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """编辑店铺页：同一份分组表单，预填这条店铺现在的内容。"""
    hub = hub_shops.viewer(request)
    owner_id = getattr(hub, "id", 0) or 0
    editing = hub_shops.get_mine(owner_id, shop_id) if owner_id else None
    if editing is None:
        return _redirect("/shops", "店铺不存在或不属于当前账号", "warn")
    ctx = _shop_form_ctx(
        request, db, editing=editing, msg=msg, kind=kind,
        student=hub_shops.is_student_form(hub),
    )
    return templates.TemplateResponse("shop_form.html", ctx)


@router.post("/shops")
async def shop_create(request: Request) -> RedirectResponse:
    """新增店铺：渔枢系统里已有这条档案就带入 / 同步过来，没有才新建（待审核）。"""
    hub = hub_shops.viewer(request)
    if hub is None:
        return _redirect("/shops", "没识别到登录账号：请从渔枢系统登录后再添加店铺", "err")
    form = await request.form()
    try:
        row, action = hub_shops.create_mine(
            hub.id, str(getattr(hub, "username", "") or ""), form,
            student=hub_shops.is_student_form(hub),
        )
    except ValueError as exc:
        return _redirect("/shops", str(exc), "err")
    name = row["name"]
    # 手动添加 / 认领的补一条台账：自动同步再扫到这家店时认得出来源，也不会重复关联
    _shop_link_note("add", hub.id, int(row.get("id") or 0), action == "claimed")
    if action == "claimed":
        return _redirect("/shops", f"店铺「{name}」在渔枢系统已存在，已带入你的账号", "success")
    if action == "linked":
        return _redirect("/shops", f"店铺「{name}」已按运营人从渔枢系统同步到你的账号", "success")
    if action.startswith("synced:"):
        count = action.split(":", 1)[1]
        return _redirect(
            "/shops",
            f"已按运营人从渔枢系统同步 {count} 家店铺到你的账号（含「{name}」）",
            "success",
        )
    return _redirect("/shops", f"已提交店铺「{name}」，渔枢系统端审核通过后生效")


@router.post("/shops/{shop_id}/edit")
async def shop_edit(shop_id: int, request: Request) -> RedirectResponse:
    """改我的店铺：改完回到「待审核」，等渔枢系统端重新审核。"""
    hub = hub_shops.viewer(request)
    if hub is None:
        return _redirect(f"/shops/{shop_id}/edit", "没识别到登录账号：请从渔枢系统登录后再改", "err")
    form = await request.form()
    try:
        hub_shops.update_mine(
            hub.id, shop_id, form, student=hub_shops.is_student_form(hub)
        )
    except ValueError as exc:
        return _redirect(f"/shops/{shop_id}/edit", str(exc), "err")
    return _redirect("/shops", "已保存：改动需渔枢系统端重新审核通过后生效")


@router.post("/shops/{shop_id}/delete")
def shop_delete(shop_id: int, request: Request) -> RedirectResponse:
    """删我的店铺：自己提交的删档案，从渔枢系统同步过来的只解除关联。"""
    hub = hub_shops.viewer(request)
    if hub is None:
        return _redirect("/shops", "没识别到登录账号：请从渔枢系统登录后再删", "err")
    try:
        msg = hub_shops.delete_mine(hub.id, shop_id)
    except ValueError as exc:
        return _redirect("/shops", str(exc), "err")
    _shop_link_note("unlink", hub.id, shop_id)
    return _redirect("/shops", msg, "warn")


@router.post("/shops/sync")
def shop_sync_now(request: Request) -> RedirectResponse:
    """立即同步：不等节流，把渔枢系统里属于当前账号的店铺一次补齐。

    平时进渔货栈会自动同步（第一次全量、之后每 5 分钟增量补一次），这个按钮
    是给「我刚在渔枢系统建了店，想马上看到」的场合用的。
    """
    hub = hub_shops.viewer(request)
    if hub is None:
        return _redirect("/shops", "没识别到登录账号：请从渔枢系统登录后再同步", "err")
    try:
        from .shop_sync import sync_user  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return _redirect("/shops", f"当前部署未接入渔枢系统，没法同步（{exc}）", "err")
    result = sync_user(int(hub.id), force=True) or {}
    if not result.get("ok"):
        return _redirect("/shops", "没有同步到店铺：确认渔枢系统里已有你的店铺档案", "warn")
    new = int(result.get("new") or 0)
    total = int(result.get("total") or 0)
    if new:
        return _redirect("/shops", f"本次新增 {new} 家店铺，现在共 {total} 家", "success")
    return _redirect("/shops", f"渔枢系统里你的店铺已经在列表里了（共 {total} 家）")


# =====================================================================
# 12. 联合 SSO /sso/login（闲参谋 → 渔货栈）
# =====================================================================
@router.get("/sso/login", response_class=HTMLResponse)
def sso_login(
    request: Request,
    token: str | None = None,
    next: str = "/harbor",
) -> RedirectResponse:
    """联合 SSO 登录入口：SaaS 版「闲参谋」跳转 / iframe 内嵌进入渔货栈。

    - 正式环境：校验闲参谋下发的 ticket（HMAC 验签 + 一次性 + 时效），换取分销商账号
    - DEVELOPMENT：跳过验签，`token` 直接当作演示用户名写会话 cookie
    - 登录成功后直接落在货源广场
    """
    from urllib.parse import urlparse

    settings = get_settings()
    prefix = settings.self_prefix

    def _hub(path: str) -> str:
        """站内路径统一带上渔货栈前缀（页面挂在 /self 下）。"""
        return f"{prefix}{path}" if prefix and path.startswith("/") else path

    user = token or "demo-reseller"
    if settings.app_env != "development" and not token:
        # 生产缺 token：拒绝并提示从闲参谋入口进入
        return RedirectResponse(_hub("/harbor") + "?sso=missing_token", status_code=303)

    # 仅允许站内跳转，防开放重定向
    target = urlparse(next).path or "/harbor"
    if not target.startswith("/"):
        target = "/harbor"

    resp = RedirectResponse(_hub(target), status_code=303)
    resp.set_cookie(_SSO_USER_COOKIE, user, max_age=7 * 24 * 3600, httponly=True)
    return resp


# =====================================================================
# 13. 更新日志 /changelog
# =====================================================================
@router.get("/changelog", response_class=HTMLResponse)
def changelog(request: Request) -> HTMLResponse:
    ctx = _common_ctx(
        request, "changelog",
        nav_crumb=["系统", "更新日志"],
        changelog=CHANGELOG,
        app_name=APP_NAME,
        app_tagline=APP_TAGLINE,
    )
    return templates.TemplateResponse("changelog.html", ctx)


# =====================================================================
# 13.5 新手指引：勾了「以后不再弹出」就记到服务端
# =====================================================================
@router.post("/guide/hide", response_class=JSONResponse)
def guide_hide(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """记下「以后不再弹出」（用户 2026-10-11 拍板）。

    弹窗里勾了那个框再关掉（点步骤 / ✕ / 跳过 / 遮罩都算）就会调这里 ——
    落服务端而不是 localStorage，换浏览器、换设备都不再弹他。
    没勾就什么都不做：只是关掉这一次，下次登录照旧提醒。
    """
    try:
        from ..self_purchase.guide import hide  # noqa: PLC0415

        uid = getattr(hub_shops.viewer(request), "id", None)
        if not uid:
            return JSONResponse({"ok": False, "detail": "没有登录"}, status_code=401)
        return JSONResponse({"ok": bool(hide(db, int(uid)))})
    except Exception as exc:  # noqa: BLE001  记偏好失败不该弹一堆错给学员
        logger.warning("记录「不再弹出」失败：%s", exc)
        return JSONResponse({"ok": False}, status_code=200)


# =====================================================================
# 14. 退出登录 /logout（顶栏「我的」下拉里的那一行）
# =====================================================================
@router.post("/logout")
def logout() -> RedirectResponse:
    """退出登录：清掉会话 Cookie，回渔货栈首页。

    渔货栈与渔枢系统共用同一份登录态（合并部署时都是 `oh_session` Cookie），
    所以这里直接复用渔枢系统的清 Cookie 逻辑；独立部署（没有 app 包）时只做跳转。
    用 POST 而不是 GET：退出是会改状态的动作，别让浏览器预取 / 爬虫点到。
    """
    resp = _redirect("/", "已退出登录")
    try:
        from app.web.auth import clear_session_cookie
    except Exception:  # noqa: BLE001  独立部署没有 app 包，只跳转
        return resp
    clear_session_cookie(resp)
    return resp


__all__ = ["router"]
