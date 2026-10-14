"""后台导航结构：业务板块 → 一级菜单 → 二级菜单，以及由它反查出来的面包屑。

以前侧边栏是 base.html 里手写的一串 <a>：页面一多就分不清谁跟谁同级、谁属于哪块，
面包屑也只能显示「品牌 / 当前页」。这里把导航变成一份数据：

  · 侧边栏按板块分组渲染，一级菜单可展开出二级（原生 <details>，不依赖 JS）；
  · 面包屑由当前请求路径在这份数据里反查得到，**每一级都带链接**（板块 / 一级 / 二级都能点回去）；
  · 以后加页面只改这里的 NAV，不用再动模板。

徽标（菜单右侧小数字）按需从库里数：商品款数、待人工核对的订单数，带 30 秒缓存，不拖慢页面。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import func, or_, select

from ..db import session_scope
from ..models import (
    ORDER_STATE_CLOSED,
    SHOP_AUDIT_PENDING,
    Order,
    Product,
    Settlement,
    Shop,
    awaiting_tracking,
    is_refund_order,
    is_refund_pending,
    not_recalled,
    pending_push_order,
)
from ..services.product import AUDIT_PENDING

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NavItem:
    """一个菜单项：有 children 时是一级菜单（可展开），否则是平铺项或二级菜单。"""

    key: str
    label: str
    url: str = ""
    icon: str = ""
    # 额外的路径前缀：把详情页也归到这一项（如 /orders/123 归到订单归档）
    match: tuple[str, ...] = ()
    admin_only: bool = False
    # 徽标：见 BADGE_HINTS / badge_counts() 的键，填了就在菜单右侧显示数字（为 0 时不显示）。
    # 一级、二级菜单都能用：一级看「有几件事要处理」，二级看「这个状态有几单」。
    badge: str = ""
    wip: bool = False
    children: tuple["NavItem", ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class NavSection:
    """业务板块：工作台 / 交易 / 货源 / 供应链 / 商家 / 消息 / 系统。"""

    title: str
    items: tuple[NavItem, ...]
    admin_only: bool = False


# ---------------------------------------------------------------- 导航定义
NAV: tuple[NavSection, ...] = (
    NavSection(
        "工作台",
        (
            NavItem("dashboard", "数据总览", "/", "bi-grid-1x2", match=("/",)),
            NavItem("stats_products", "热销榜单", "/stats/products", "bi-bar-chart", match=("/stats/products",)),
        ),
    ),
    NavSection(
        "交易",
        (
            NavItem(
                "orders",
                "订单管理",
                "/orders",
                "bi-file-earmark-text",
                match=("/orders",),
                # 一级徽标看「这个菜单共有几单要处理」：待人工核对 + 待推单发货 + 待拦截退单（同一单只算一次）。
                # 拆开的三项在展开后的二级菜单里各自都有数字，所以一级不重复某一个子项。
                badge="orders_todo",
                children=(
                    # 二级菜单右侧的数字 = 这个状态下的单数（与点进去的列表同一个筛选口径）
                    NavItem("orders_all", "全部订单", "/orders", match=("/orders",)),
                    NavItem("orders_review", "待人工核对", "/orders?review=1", badge="orders_review"),
                    # 待推单发货：还没推给供应商的单子（以前只在首页有个数字，菜单里没有入口）
                    NavItem(
                        "orders_pending_push",
                        "待推单发货",
                        "/orders?supplier_status=pending",
                        badge="orders_pending_push",
                    ),
                    # refund=pending / refund=all = 退单订单（老的 refund=1 已经不是有效值，页面会当没筛）
                    # 待拦截在前：这是要动手处理的（7 天内拦不住就得发），已退单是处理完的结果
                    NavItem(
                        "orders_refund_pending",
                        "待拦截退单",
                        "/orders?refund=pending",
                        badge="orders_refund_pending",
                    ),
                    NavItem("orders_refund", "已退单", "/orders?refund=all", badge="orders_refund"),
                    # 关闭完成 / 已完成：钱货都处理完的终态单（逆向流程走完 + 推单已完成），
                    # 不再参与推单与统计，单独一页便于事后回查对账
                    NavItem(
                        "orders_closed",
                        "关闭完成",
                        "/orders?closed=1",
                        badge="orders_closed",
                    ),
                    NavItem(
                        "orders_recycle",
                        "撤单回收站",
                        "/orders/recycle",
                        match=("/orders/recycle",),
                        badge="orders_recycle",
                    ),
                ),
            ),
        ),
    ),
    NavSection(
        "供应链",
        (
            NavItem(
                "batches",
                "供应链发货",
                "/batches",
                "bi-truck",
                match=("/batches",),
                # 数字看的是「还有几笔回单没填回系统」：已推给供应商、供应商还没给物流单号的订单
                badge="batches_tracking",
            ),
            NavItem("suppliers", "供应商管理", "/suppliers", "bi-shop", match=("/suppliers",)),
        ),
    ),
    NavSection(
        "货源",
        (
            NavItem(
                "products",
                "商品管理",
                "/products",
                "bi-box-seam",
                match=("/products",),
                badge="products_audit",
                # 还在开发中的菜单：右侧挂「Beta 开发中」标签（见 base.html 的 .nav-wip），
                # 提醒用的人这块功能还没定稿
                wip=True,
                children=(
                    NavItem("products_supply", "供应链商品", "/products?source=third_party"),
                    NavItem("products_self", "自营商品", "/products?source=self_operated"),
                    NavItem("products_new", "新增商品", "/products/new", match=("/products/new",)),
                    NavItem("products_stats", "商品统计", "/products/stats", match=("/products/stats",)),
                ),
            ),
            # 渔货栈发布管理：商品内容在渔枢系统端维护，在这里一键上架到渔货栈
            # （直接落到渔货栈商品池的对应类目下；闲鱼发布由渔货栈自己做）
            NavItem(
                "sourcing",
                "渔货栈发布",
                "/sourcing",
                "bi-shop-window",
                match=("/sourcing",),
            ),
            # 采购节点：渔货栈「货源广场」顶部那份活动日历（日期窗口 + 关键词匹配），
            # 以前写死在代码里，现在运营自己在页面上配
            NavItem(
                "purchase_nodes",
                "采购节点",
                "/purchase-nodes",
                "bi-calendar-week",
                match=("/purchase-nodes",),
            ),
            # 展示基数（2026-10-13 用户拍板）：渔货栈货源卡片「X 家上架 / 已售 N」
            # 垫的展示基数 —— 只撑场面，真实数量一个不动
            NavItem(
                "sourcing_display",
                "展示基数",
                "/sourcing-display",
                "bi-graph-up-arrow",
                match=("/sourcing-display",),
            ),
            # 类目（一级 / 二级 / 三级）与商品管理同级：它是发布商品、
            # 渔货栈货源广场共用的同一份类目口径，单独一个入口更好找
            NavItem(
                "products_categories",
                "类目管理",
                "/products/categories",
                "bi-diagram-3",
                match=("/products/categories",),
            ),
            # 商品标签：发布商品时勾选的固定标签（源头工厂 / 现货速发 …），
            # 勾了以后随「上架到渔货栈」一起同步过去，在货源广场与商品详情页展示
            NavItem(
                "product_tags",
                "商品标签",
                "/product-tags",
                "bi-tags",
                match=("/product-tags",),
            ),
            # 运费模板 / 闲鱼发货方式：上架商品时默认套用的规则（全局默认 + 关联供应商 + 关联类目）
            NavItem(
                "shipping_rules",
                "运费模板",
                "/shipping-rules",
                "bi-truck-front",
                match=("/shipping-rules",),
            ),
            # 地址管理：退货地址（买家寄回）/ 发货地址（我们寄出）
            # 与运费模板同一套管理（全局默认 + 供应商 / 类目细分），多一层「关联指定商品」
            NavItem(
                "return_addresses",
                "地址管理",
                "/addresses",
                "bi-geo-alt",
                match=("/addresses", "/return-addresses"),
            ),
            # 注：渔枢系统的货源页（搜索商品 / 货源热销榜 / 我的上架）不放渔枢系统菜单里，
            # 那些是渔货栈的功能（页面在 /hub 下），要用就切到渔货栈去
        ),
    ),
    # 注意：检测仓（/wms）**不放在平台菜单里** —— 它是独立子系统，有自己的菜单
    # 与角色权限（见 wms/nav.py、wms/permissions.py）。入口在左侧品牌行的
    # 「子系统切换」面板里（app/portals.py 的 PORTAL_WMS），能看什么由检测仓自己判断。
    # 资金排在供应链后面（2026-10-01 按日常使用顺序对调）：一天的动作是「催发货 → 结货款」，
    # 菜单顺序跟着走，两块整体换位，不是把「资金结算」单独挪出来
    NavSection(
        "资金",
        (
            NavItem("finance", "资金结算", "/finance", "bi-cash-coin", match=("/finance",), badge="finance_pending"),
        ),
    ),
    NavSection(
        "商家",
        (
            # 数字看的是「还有几个渔货栈提交的店铺没审」：只有审核通过的店铺才在渔货栈生效
            NavItem(
                "shops",
                "店铺管理",
                "/shops",
                "bi-bag-heart",
                match=("/shops",),
                badge="shops_audit",
            ),
        ),
    ),
    NavSection(
        "消息",
        (
            NavItem("messages", "飞书消息", "/messages", "bi-chat-dots", match=("/messages",), admin_only=True),
            # 路径独立于系统设置：它是自己的一级菜单，不该挂在 /settings 下面
            NavItem(
                "message_center",
                "消息中心",
                "/message-center",
                "bi-broadcast",
                match=("/message-center",),
                admin_only=True,
            ),
        ),
        admin_only=True,
    ),
    NavSection(
        "系统",
        (
            # 公司资源：公司手里能直接干活的资产（手机号 → 它开出来的闲鱼 / 支付宝 /
            # 微信 / 小红书账号）。现在只有手机号一类，以后加「店铺账号」「设备」就挂在
            # 这一级下面，不用再开一个板块
            NavItem(
                "company_assets",
                "公司资源",
                "/phone-numbers",
                "bi-phone",
                match=("/phone-numbers",),
                admin_only=True,
                children=(
                    NavItem(
                        "phone_numbers",
                        "手机号管理",
                        "/phone-numbers",
                        match=("/phone-numbers",),
                    ),
                ),
            ),
            NavItem("users", "用户管理", "/users", "bi-people", match=("/users",), admin_only=True),
            # 角色权限：给账号配「能进哪些菜单」（见 web/permissions.py）
            NavItem("roles", "角色权限", "/roles", "bi-shield-lock", match=("/roles",), admin_only=True),
            NavItem("rules", "解析规则", "/rules", "bi-sliders", match=("/rules",), admin_only=True),
            # 引流名片（2026-10-09）：填一次生成一张带 OG 标签的 H5 长链接，
            # 贴到抖音群里由爬虫渲染成卡片（封面图 + 标题 + 简介）
            NavItem(
                "share_cards",
                "引流名片",
                "/share-cards",
                "bi-person-badge",
                match=("/share-cards",),
                admin_only=True,
            ),
            NavItem("logs", "操作日志", "/logs", "bi-clock-history", match=("/logs",), admin_only=True),
            # 异常日志（2026-10-09）：线上报错自动落库（含堆栈），三个子系统共用一张表
            NavItem("errors", "异常日志", "/errors", "bi-bug", match=("/errors",), admin_only=True),
            NavItem("settings", "系统设置", "/settings", "bi-gear", match=("/settings",), admin_only=True),
            NavItem("backups", "数据备份", "/backups", "bi-database-down", match=("/backups",), admin_only=True),
            NavItem("changelog", "更新日志", "/changelog", "bi-journal-check", match=("/changelog",)),
            # 流程说明：推单 / 退单 / 订单从头到尾怎么走、状态怎么变（各页面里也有弹窗入口）
            NavItem("help", "流程说明", "/help", "bi-question-circle", match=("/help",)),
        ),
    ),
)


# ---------------------------------------------------------------- 徽标计数
# 菜单右侧的小数字只表示「这个功能现在有几件事要处理」，不是数据总量：
# 商品=待审核、供应链推单=待填回单、资金结算=待结算。为 0 就不显示。
# 订单归档：一级是「待处理合计」（待人工核对 + 待推单发货 + 待拦截退单，同一单只算一次），
# 二级则是「这个状态有几单」（待人工核对 / 待推单发货 / 已退单 / 待拦截退单 / 撤单回收站）。
BADGE_HINTS: dict[str, str] = {
    "orders_todo": "待处理订单合计",
    "orders_review": "待人工核对的订单",
    "orders_pending_push": "待推单发货（还没推给供应商的订单）",
    "orders_refund": "已退单（退单订单总数）",
    "orders_refund_pending": "待拦截的退单",
    "orders_recycle": "撤单回收站（群里撤回、只留档的订单）",
    "orders_closed": "关闭完成 / 已完成的订单（不再参与推单与统计）",
    "products_audit": "待审核商品",
    "batches_tracking": "待填回单（已推单、还没回填物流单号）",
    "finance_pending": "待结算流水",
    "shops_audit": "待审核店铺（渔货栈账号提交的）",
    # 检测仓的徽标（待检货品数 / 超时报修单）不在这里：待检与超时是检测仓自己的事，
    # 数字写在它自己的侧栏里（wms/nav.py::badge_counts），平台侧不再重复一份。
}

_BADGE_TTL = 30.0
_badge_cache: dict[str, tuple[float, dict[str, int]]] = {}


def badge_counts() -> dict[str, int]:
    """菜单徽标：各菜单「待处理」的数量（带 30 秒缓存，只保留大于 0 的）。"""
    cached = _badge_cache.get("v")
    stamp = time.time()
    if cached and stamp - cached[0] < _BADGE_TTL:
        return cached[1]

    counts: dict[str, int] = {}
    try:
        with session_scope() as session:
            # 逐个算：某一条查不出来（比如老库缺字段）只影响它自己，不连累别的菜单
            queries = {
                # 订单归档的一级徽标：待处理合计（下面三项的并集，同一订单只算一次）——
                # 它和「待人工核对」不是一回事：后者只是其中一类
                "orders_todo": select(func.count())
                .select_from(Order)
                .where(
                    not_recalled(),
                    or_(
                        Order.needs_review.is_(True),
                        pending_push_order(),
                        is_refund_pending(),
                    ),
                ),
                "orders_review": select(func.count())
                .select_from(Order)
                .where(Order.needs_review.is_(True), not_recalled()),
                # 待推单发货：口径与首页「待推单发货」、订单页 ?supplier_status=pending 完全一致
                "orders_pending_push": select(func.count())
                .select_from(Order)
                .where(pending_push_order()),
                # 退单三项：与订单页 ?refund=all / ?refund=pending、撤单回收站同一个口径，
                # 点进去条数必然对得上（撤回的留档单不参与业务，两边都不算）
                "orders_refund": select(func.count())
                .select_from(Order)
                .where(not_recalled(), is_refund_order()),
                "orders_refund_pending": select(func.count())
                .select_from(Order)
                .where(not_recalled(), is_refund_pending()),
                "orders_recycle": select(func.count())
                .select_from(Order)
                .where(Order.is_recalled.is_(True)),
                # 关闭完成 / 已完成：与订单归档子页面 ?closed=1 同一个口径（逆向终态 + 推单已完成）
                "orders_closed": select(func.count())
                .select_from(Order)
                .where(
                    not_recalled(),
                    or_(
                        Order.order_state == ORDER_STATE_CLOSED,
                        Order.supplier_status == "done",
                    ),
                ),
                "products_audit": select(func.count())
                .select_from(Product)
                .where(Product.audit_status == AUDIT_PENDING),
                # 待填回单：已推给供应商、供应商还没回物流单号的订单（与首页「待供应商回单」同一个数）
                "batches_tracking": select(func.count())
                .select_from(Order)
                .where(awaiting_tracking()),
                "finance_pending": select(func.count())
                .select_from(Settlement)
                .where(Settlement.status == "pending"),
                # 渔货栈账号提交、还没审的店铺
                "shops_audit": select(func.count())
                .select_from(Shop)
                .where(Shop.audit_status == SHOP_AUDIT_PENDING),
            }
            for key, stmt in queries.items():
                try:
                    counts[key] = int(session.scalar(stmt) or 0)
                except Exception as exc:  # noqa: BLE001
                    # 失败后立刻回滚，否则会话停在异常状态，后面几条也一起查不出来
                    session.rollback()
                    logger.warning("菜单徽标 %s 统计失败：%s", key, exc)
    except Exception as exc:  # noqa: BLE001 徽标数不出来不该影响页面
        logger.warning("菜单徽标统计失败：%s", exc)
        counts = cached[1] if cached else {}
    counts = {key: int(value or 0) for key, value in counts.items() if int(value or 0) > 0}
    _badge_cache["v"] = (stamp, counts)
    return counts


def badge_hint(key: str, counts: dict[str, int]) -> str:
    """徽标的鼠标提示：文案 + 数字（数字已经写在里面，模板不用再拼一遍）。

    只有订单归档的一级徽标是「几项合并出来的合计」，光看数字不知道由什么组成，
    所以把拆开的三项也一并写出来。
    """
    total = int(counts.get(key, 0))
    if total <= 0 or not key:
        return ""
    if key == "orders_todo":
        return "待处理订单 合计 %d 单：待人工核对 %d · 待推单发货 %d · 待拦截退单 %d（同一订单只算一次）" % (
            total,
            int(counts.get("orders_review", 0)),
            int(counts.get("orders_pending_push", 0)),
            int(counts.get("orders_refund_pending", 0)),
        )
    return f"{BADGE_HINTS.get(key, '')} {total}".strip()


# ---------------------------------------------------------------- 匹配
def split_url(url: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(url or "")
    query = {k: v[0] for k, v in parse_qs(parts.query).items() if v}
    return parts.path or "/", query


def item_paths(item: NavItem) -> tuple[str, ...]:
    """这一项能匹配的路径前缀：优先用显式 match，否则用 url 的路径部分。"""
    if item.match:
        return item.match
    return (split_url(item.url)[0],)


def _path_matches(path: str, prefix: str) -> bool:
    if prefix == "/":
        return path == "/"
    return path == prefix or path.startswith(prefix + "/")


def children_query_keys(item: NavItem) -> set[str]:
    """一级菜单下所有二级项用到的查询参数（用于区分「全部订单」与「待人工核对」）。"""
    keys: set[str] = set()
    for child in item.children:
        keys |= set(split_url(child.url)[1].keys())
    return keys


def child_active(child: NavItem, path: str, query: dict[str, str], sibling_keys: set[str]) -> bool:
    """二级项是否命中当前请求。

    带参数的二级项（/orders?review=1）要求参数对得上；
    不带参数的二级项（/orders）在请求带了兄弟项的参数时不算命中，避免两个同时高亮。
    """
    child_path, child_query = split_url(child.url)
    if child_path != path:
        return False
    if child_query:
        return all(query.get(k) == v for k, v in child_query.items())
    return not (sibling_keys & set(query.keys()))


def _norm_allowed(allowed) -> set[str] | frozenset[str] | None:
    """把「可见菜单」参数归一。

    兼容老写法：以前传的 is_admin（True / False）。True = 不限制，
    False = 默认菜单（非 admin_only 的那些）—— 行为与升级前一模一样。
    新写法直接传菜单 key 集合（见 web/permissions.menu_keys_for）。
    """
    if allowed is None or allowed is True:
        return None
    if allowed is False:
        return frozenset(item.key for section in NAV for item in section.items if not item.admin_only)
    return allowed


def _visible(item: NavItem, allowed: set[str] | frozenset[str] | None) -> bool:
    """allowed = None 表示不限制（管理员）；否则按菜单 key 判。

    授权粒度只到一级菜单：二级菜单（订单归档下的各状态等）跟父项走 ——
    再细下去配起来没人愿意维护，而运营真正关心的就是「进不进得了这个页面」。
    """
    return allowed is None or item.key in allowed


# ---------------------------------------------------------------- 侧边栏
def build(path: str, query: dict[str, str], allowed: set[str] | frozenset[str] | None = None) -> list[dict]:
    """侧边栏数据：每项带 active（当前项）与 open（展开二级）。

    allowed 传 None = 不限制（管理员）。
    """
    allowed = _norm_allowed(allowed)
    counts = badge_counts()
    sections: list[dict] = []
    for section in NAV:
        if allowed is not None and not any(item.key in allowed for item in section.items):
            continue
        items: list[dict] = []
        for item in section.items:
            if not _visible(item, allowed):
                continue
            keys = children_query_keys(item)
            children: list[dict] = []
            child_hit = False
            for child in item.children:
                hit = child_active(child, path, query, keys)
                child_hit = child_hit or hit
                children.append(
                    {
                        "item": child,
                        "active": hit,
                        # 二级菜单也能带数字：订单归档下那几个状态各显示自己的单数
                        "count": counts.get(child.badge, 0) if child.badge else 0,
                        "count_hint": badge_hint(child.badge, counts) if child.badge else "",
                    }
                )
            self_hit = any(_path_matches(path, p) for p in item_paths(item))
            # 命中深度：/products 与 /products/categories 会同时命中，取更长的那个（见下方去重）
            hit_depth = max((len(p) for p in item_paths(item) if _path_matches(path, p)), default=0)
            items.append(
                {
                    "item": item,
                    "children": children,
                    # 二级命中时一级只展开、不上底色：避免一级二级两层同时高亮
                    "active": self_hit and not child_hit,
                    "hit_depth": hit_depth if self_hit and not child_hit else 0,
                    "open": child_hit,
                    "count": counts.get(item.badge, 0) if item.badge else 0,
                    "count_hint": badge_hint(item.badge, counts) if item.badge else "",
                }
            )
        if items:
            # 路径前缀重叠时只高亮更具体的那一项：如 /products/categories
            # 同时命中「商品管理」（/products）与「类目管理」，只给类目管理上底色
            depths = [row["hit_depth"] for row in items if row["hit_depth"]]
            deepest = max(depths) if depths else 0
            for row in items:
                if row["hit_depth"] and row["hit_depth"] < deepest:
                    row["active"] = False
            # 注意键名不叫 items：Jinja 里 group.items 会命中 dict 的 items 方法
            sections.append({"section": section, "nav_items": items})
    return sections


# ---------------------------------------------------------------- 面包屑
def crumbs(
    path: str,
    query: dict[str, str],
    allowed: set[str] | frozenset[str] | None = None,
    leaf: str = "",
) -> list[dict]:
    """面包屑：板块 → 一级 → 二级 → 当前页；每一级除最后一级外都带链接，点得回去。

    allowed 传 None = 不限制（管理员）。

    leaf = 当前页的名字。页面如果挂在别的菜单下面（发货详情 /orders/{id}/shipping 属于
    「供应链发货」，光看路径会指到「订单管理」去），就由页面指定 crumb_path + leaf：
    面包屑变成「供应链 / 供应链发货 / 发货详情」，点得回列表。
    """
    allowed = _norm_allowed(allowed)
    child_hits: list[tuple[int, NavSection, NavItem, NavItem]] = []
    item_hits: list[tuple[int, NavSection, NavItem]] = []
    for section in NAV:
        if allowed is not None and not any(item.key in allowed for item in section.items):
            continue
        for item in section.items:
            if not _visible(item, allowed):
                continue
            keys = children_query_keys(item)
            for child in item.children:
                if child_active(child, path, query, keys):
                    child_hits.append((len(split_url(child.url)[0]), section, item, child))
            for prefix in item_paths(item):
                if _path_matches(path, prefix):
                    item_hits.append((len(prefix), section, item))
                    break

    if child_hits:  # 二级页：板块 / 一级 / 二级 三级面包屑
        _, section, item, child = max(child_hits, key=lambda row: row[0])
        trail = [
            {"label": section.title, "url": item.url, "current": False},
            {"label": item.label, "url": item.url, "current": False},
            {"label": child.label, "url": child.url, "current": not leaf},
        ]
        if leaf:
            trail.append({"label": leaf, "url": "", "current": True})
        return trail
    if item_hits:  # 一级页：板块 / 一级（挂细页时再补一级当前页名）
        _, section, item = max(item_hits, key=lambda row: row[0])
        trail = [
            {"label": section.title, "url": item.url, "current": False},
            {"label": item.label, "url": item.url if leaf else "", "current": not leaf},
        ]
        if leaf:
            trail.append({"label": leaf, "url": "", "current": True})
        return trail
    return []
