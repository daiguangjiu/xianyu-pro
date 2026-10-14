"""子系统（Portal）定义：一个账号能进哪些子系统。

键名与页面路径一一对应，不搞历史包袱：

- `hub`  = 渔枢系统（小渔数科内部商品运营管理平台）：订单中台 + 商品 / 货源运营，
           页面在 `/`（代码在 `app/`）
- `self` = 渔货栈（学员和自营团队的选货平台）：选货 / 采购 / 店铺运营全功能工作台，
           页面挂在 `/self` 下（代码在仓库根 `src/`）
- `wms`  = 渔集（货品检测与库存出入库管理，对外系统名「渔集」）：独立子系统，
           页面挂在 `/wms` 下（代码在仓库根 `wms/`），权限由 `wms.permissions` 自己管

账号表 `users.portals` 存逗号分隔的子系统键（如 `hub,self`）：

- 管理员默认全子系统（不用单独配，也不允许改没了）；
- 其余账号由管理员在「用户管理」里勾选，子系统切换面板只显示有权限的那些；
- 老账号没有这个值时按「渔枢系统」处理，不会因为升级就进不去系统。

历史上 `self` 指渔枢系统、`hub` 指渔货栈（两者是反的），v1.20 起换正，
库里的旧值由 `scripts/migrate_portal_keys.py` 一次性对调。
"""
from __future__ import annotations

PORTAL_HUB = "hub"  # 渔枢系统：页面在 /
PORTAL_SELF = "self"  # 渔货栈：页面挂在 /self 下
PORTAL_WMS = "wms"  # 渔集（检测仓）：页面挂在 /wms 下（2026-10-04 起对外叫渔集）

# 顺序即展示顺序（子系统切换面板、用户管理的勾选框都按这个顺序）
PORTALS: tuple[dict[str, str], ...] = (
    {
        "key": PORTAL_HUB,
        "name": "渔枢系统",
        "short": "渔枢系统",
        "desc": "小渔数科内部商品运营管理平台",
        "icon": "bi-diagram-3",
        "home": "/",
    },
    {
        "key": PORTAL_SELF,
        "name": "渔货栈",
        "short": "渔货栈",
        "desc": "学员和自营团队的选货平台",
        "icon": "bi-shop",
        # 首页就是货源广场（2026-10-03 用户拍板）：概览挪到 /self/overview。
        # 登录后的落点、子系统切换都按这个值走。
        "home": "/self/harbor",
    },
    {
        "key": PORTAL_WMS,
        "name": "渔集",
        "short": "渔集",
        "desc": "货品检测入库出库管理",
        "icon": "bi-box-seam",
        "home": "/wms",
    },
)

PORTAL_KEYS: tuple[str, ...] = tuple(p["key"] for p in PORTALS)
DEFAULT_PORTAL = PORTAL_HUB
# 全端：管理员的默认授权，也是建号时的兜底值
ALL_PORTALS = ",".join(PORTAL_KEYS)


def portal(key: str) -> dict[str, str]:
    """按 key 取端定义；未知 key 一律回落到默认端（不让脏数据把页面搞崩）。"""
    for item in PORTALS:
        if item["key"] == key:
            return item
    return PORTALS[0]


def normalize_portals(value: str | None) -> tuple[str, ...]:
    """把 `hub,self` 这类值整理成有序、去重的端键元组。

    空值 / 全是无效值时返回默认端，保证任何账号至少能进一个端。
    """
    raw = str(value or "").replace("，", ",").strip()
    if not raw:
        return (DEFAULT_PORTAL,)
    picked = tuple(k for k in PORTAL_KEYS if k in [p.strip().lower() for p in raw.split(",")])
    return picked or (DEFAULT_PORTAL,)


def join_portals(keys) -> str:
    """把端键序列 / 逗号字符串整理成入库用的字符串（空 → 默认端）。"""
    if isinstance(keys, str):
        return ",".join(normalize_portals(keys))
    return ",".join(normalize_portals(",".join(str(k) for k in keys)))


def portal_names(value: str | None) -> str:
    """给用户列表 / 提示语显示：`渔枢系统 · 渔货栈`。"""
    return " · ".join(portal(k)["name"] for k in normalize_portals(value))


# 端切换记住的选择（cookie）：下次登录直接落到上次用的端
PORTAL_COOKIE = "oh_portal"


def portal_of_path(path: str) -> str:
    """当前路径属于哪个子系统（渔货栈挂在 /self 下，检测仓挂在 /wms 下）。"""
    text = str(path or "")
    if text.startswith("/self"):
        return PORTAL_SELF
    if text == "/wms" or text.startswith("/wms/"):
        return PORTAL_WMS
    return PORTAL_HUB


def allowed_portals(user) -> tuple[dict[str, str], ...]:
    """这个账号能进的端（管理员全端）；没登录时返回空。"""
    if user is None:
        return ()
    keys = getattr(user, "portal_keys", None) or normalize_portals(getattr(user, "portals", ""))
    return tuple(p for p in PORTALS if p["key"] in keys)


def switch_options(user, current_path: str = "") -> list[dict[str, object]]:
    """子系统切换面板的数据：只含有权限的子系统，并标出当前所在的那个。

    特例：**人已经在的那个子系统永远列出来**（哪怕账号的 subsystems 里没勾它）——
    检测仓这类子系统自己管权限（``wms.permissions``），进来的人不一定在平台的
    授权表里留过痕迹；不列的话面板上就会出现「人在 A，A 却不在列表里」的怪状。
    """
    current = portal_of_path(current_path)
    options = list(allowed_portals(user))
    if not any(p["key"] == current for p in options):
        options = [p for p in PORTALS if p["key"] == current] + options
        options.sort(key=lambda p: PORTAL_KEYS.index(p["key"]))
    return [{**p, "active": p["key"] == current} for p in options]


def current_of(user, current_path: str = "") -> dict[str, str]:
    """当前所在的子系统定义（面板上显示「当前」用）。"""
    return portal(portal_of_path(current_path))

