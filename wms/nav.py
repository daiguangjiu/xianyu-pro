"""检测仓自己的菜单与权限守卫表。

**这里和渔枢系统的 ``app.web.nav`` 是两套东西**：平台侧边栏里不显示检测仓，
检测仓侧边栏里也不显示订单 / 商品 / 资金。菜单能否看见只由 ``wms.permissions``
说的算，跟平台角色无关。

菜单项除了「显示什么」，还负责回答「访问这条路至少要什么权限」—— 路由守卫
（``web.router`` 的 ``_enforce``）查的就是这张表，所以**新页面只要登记在这里就自带
权限保护**，不用在每个路由里写一遍判断。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config as cfg
from .models import REPAIR_CLOSED, STATUS_PENDING, WmsItem, WmsRepairOrder


@dataclass
class NavItem:
    """一项菜单。

    ``view`` / ``write`` 是权限码：GET 进来要 ``view``，POST / 写接口要 ``write``。
    没有权限的人既看不见入口，也打不开页面。
    """

    label: str
    path: str
    icon: str = "bi-dot"
    view: str = ""
    write: str = ""
    badge: str = ""
    exact: bool = False
    # 入库分拣的**第几步**（2026-10-08）：1 批次与建档 → 2 扫码检测 → 3 标签打印与贴标复核。
    # 菜单上把序号摆在最前面，新人一眼就知道该按什么顺序干活；0 = 不属于这条主线（总览等）。
    # 注意 label 里**不带**序号（面包屑、页面标题直接用 label，不能被序号污染）。
    step: int = 0


@dataclass
class NavSection:
    title: str
    items: list[NavItem] = field(default_factory=list)


NAV_SECTIONS: tuple[NavSection, ...] = (
    NavSection(
        "检测作业",
        [
            NavItem("检测仓总览", "/wms", icon="bi-house-door", view="wms.overview.view", exact=True),
            # 下面三步就是「入库分拣」的主线，序号必须连号且挨着排 ——
            # 新人照着 1 → 2 → 3 走一遍，货就入库上架了（2026-10-08 用户要求）
            NavItem("批次与建档", "/wms/batches", icon="bi-box-arrow-in-down",
                    view="wms.batch.view", write="wms.batch.manage", step=1),
            NavItem("扫码检测", "/wms/inspect", icon="bi-upc-scan",
                    view="wms.inspect.view", write="wms.inspect.manage",
                    badge="pending", step=2),
            NavItem("标签打印与贴标复核", "/wms/labels", icon="bi-tag",
                    view="wms.label.view", write="wms.label.manage", step=3),
        ],
    ),
    NavSection(
        "库存与出库",
        [
            # 菜单顺序（2026-10-16 用户拍板）：货品台账（按款）→ 库存台账（按台）
            # → 发货管理 → 出库管理 → 库存账本
            # 货品台账（2026-10-14）：以渔货号为维度看「这一款货」，条码台账看的是「这一台货」
            NavItem("货品台账", "/wms/goods", icon="bi-collection", view="wms.item.view", write="wms.item.manage"),
            NavItem("库存台账", "/wms/items", icon="bi-boxes", view="wms.item.view", write="wms.item.manage"),
            # 发货管理（2026-10-15）：渔货栈自营商品的发货单（渔枢中台 → 渔集）
            NavItem("发货管理", "/wms/deliveries", icon="bi-truck", view="wms.outbound.view", write="wms.outbound.manage"),
            NavItem("出库管理", "/wms/outbound", icon="bi-box-arrow-up", view="wms.outbound.view", write="wms.outbound.manage"),
            NavItem("库存账本", "/wms/ledger", icon="bi-journal-text", view="wms.ledger.view"),
        ],
    ),
    NavSection(
        "异常与盘点",
        [
            NavItem("报修工单", "/wms/repairs", icon="bi-wrench", view="wms.repair.view", write="wms.repair.manage", badge="repair_timeout"),
            NavItem("拆机件残值", "/wms/parts", icon="bi-cpu", view="wms.part.view", write="wms.part.manage"),
            NavItem("盘点与差异", "/wms/stocktake", icon="bi-clipboard-check", view="wms.stocktake.view", write="wms.stocktake.manage"),
        ],
    ),
    NavSection(
        "规则 · 报表 · 系统",
        [
            # 商品字段：Excel 导入时沉淀下来的列（属于建档配置，权限跟着批次走）
            NavItem("商品字段", "/wms/fields", icon="bi-card-list", view="wms.batch.view", write="wms.batch.manage"),
            NavItem("分级规则", "/wms/rules", icon="bi-sliders", view="wms.rule.view", write="wms.rule.manage"),
            NavItem("报表中心", "/wms/reports", icon="bi-bar-chart", view="wms.report.view"),
            NavItem("人员与授权", "/wms/members", icon="bi-people", view="wms.member.manage", write="wms.member.manage"),
            # 仓库与货架：仓库 / 区 / 货架 / 层 / 位五级的增删改（其他页面里的仓库与库位都从它出）
            NavItem("仓库与货架", "/wms/warehouses", icon="bi-building-gear",
                    view="wms.warehouse.view", write="wms.warehouse.manage"),
            # 打印设置：芯烨条码云的账号 / 打印机 / 标签模板（云上部署靠它打现场的标签）
            NavItem("打印设置", "/wms/print-settings", icon="bi-printer",
                    view="wms.print.view", write="wms.print.manage"),
            # 更新日志与系统设计方案是**给人看的**：不涉及数据，登录就能看，不需要任何权限。
            NavItem("更新日志", "/wms/changelog", icon="bi-journal-text"),
            # 系统设计方案：流程怎么串、分级引擎为什么这么判、一码到底的边界在哪
            NavItem("系统设计方案", "/wms/design", icon="bi-journal-bookmark"),
        ],
    ),
)

# 例外规则：写操作里更敏感的那几个，不跟着「域 + manage」走，单独一把钥匙。
# (路径后缀, 权限码)
_WRITE_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("/approve", "wms.stocktake.approve"),          # 盘点核销审批
    ("/price", "wms.item.price"),                    # 改价（结算 / 底价）
    ("/members", "wms.member.manage"),               # 人员授权
    ("/members/role", "wms.member.manage"),
    ("/rules/grade", "wms.rule.manage"),
    ("/rules/price", "wms.rule.manage"),
)

# 扫码工作台的局部刷新接口：它们不在菜单里，权限按「干的是谁的活」挂到对应菜单上
_API_PERMS: dict[str, str] = {
    "/wms/api/item.json": "wms.inspect.view",
    "/wms/api/inspect/preview": "wms.inspect.manage",
    "/wms/api/inspect/submit": "wms.inspect.manage",
    "/wms/api/upload": "wms.inspect.manage",
    # 检测台里打标签（2026-10-10）：干的是检测员的活，权限跟着检测台走
    "/wms/api/label/preview": "wms.inspect.view",
    "/wms/api/label/print": "wms.inspect.manage",
    "/wms/api/label/check-code": "wms.inspect.view",
    # 检测台里「并进这一款」（2026-10-14）：改的是货品的商品归属，按货品管理收口
    "/wms/api/item/fish-code": "wms.item.manage",
    # 检测台里「独立渔货号重新建档」（2026-10-15）：同样是改商品归属，同一把钥匙
    "/wms/api/item/fish-standalone": "wms.item.manage",
    # 报修开单的货品模糊搜索（2026-10-15）：只读查询，货品查看权限即可
    "/wms/api/item/search": "wms.item.view",
}

# 谁都能进的路径（不需要任何检测仓权限，登录即可）
_PUBLIC: tuple[str, ...] = ("/wms/denied",)


def items() -> list[NavItem]:
    return [it for sec in NAV_SECTIONS for it in sec.items]


def visible_sections(perms: set[str]) -> list[NavSection]:
    """按权限过滤后的菜单（没有权限的菜单直接不出现，不做灰色占位）。"""
    out: list[NavSection] = []
    for sec in NAV_SECTIONS:
        shown = [it for it in sec.items if (not it.view or it.view in perms)]
        if shown:
            out.append(NavSection(sec.title, shown))
    return out


def crumbs(path: str) -> list[NavItem]:
    """当前位置的面包屑（最长前缀匹配）。"""
    matched = sorted(
        (it for it in items() if path == it.path or path.startswith(it.path.rstrip("/") + "/")),
        key=lambda it: len(it.path),
        reverse=True,
    )
    return matched[:1]


def needed_perm(path: str, method: str = "GET") -> str | None:
    """访问某路径所需的权限码；``None`` = 登录即可。

    GET → 菜单的 ``view`` 权限；写方法 → ``write``（命中格外敏感的支部时用专属权限）。
    """
    if any(path.startswith(p) for p in _PUBLIC):
        return None
    api_perm = _API_PERMS.get(path)
    if api_perm:
        return api_perm
    # 详情页 / 明细页归到它的入口目录上：/wms/items/12/move → /wms/items
    base_it = None
    for it in sorted(items(), key=lambda i: len(i.path), reverse=True):
        if it.path == "/wms":
            continue
        if path == it.path or path.startswith(it.path + "/") or path.startswith(it.path + "?"):
            base_it = it
            break
    if base_it is None:
        return None
    if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
        for suffix, perm in _WRITE_OVERRIDES:
            if path.endswith(suffix):
                return perm
        return base_it.write or base_it.view or None
    return base_it.view or None


def is_active(item: NavItem, path: str) -> bool:
    if item.path == "/wms":
        return path == "/wms"
    return path == item.path or path.startswith(item.path + "/")


# ---------------------------------------------------------------- 徽标（按事实算）
def badge_counts(db: Session) -> dict[str, int]:
    """两个提醒数：待检货品 / 报修超期未回。按事实实时算，不等定时任务。"""
    # 台数口径（2026-10-10）：一台货品可以带 N 台，徽标数的是「多少台」不是「多少条」
    pending = int(
        db.scalar(
            select(func.coalesce(func.sum(WmsItem.qty), 0)).where(
                WmsItem.status == STATUS_PENDING
            )
        )
        or 0
    )
    timeout = int(
        db.scalar(
            select(func.count())
            .select_from(WmsRepairOrder)
            .where(
                WmsRepairOrder.status.notin_((REPAIR_CLOSED, "cancelled")),
                WmsRepairOrder.timeout_flag.is_(True),
            )
        )
        or 0
    )
    return {"pending": pending, "repair_timeout": timeout}


def system_label() -> str:
    return cfg.config.system_name
