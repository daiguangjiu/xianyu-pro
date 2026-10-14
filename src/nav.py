"""渔货栈（自营端）的菜单清单（2026-10-11）。

以前菜单是硬编码在 `templates/base.html` 顶栏「自营服务」下拉里的一串 <a>：
渔枢系统的「权限管理」要按菜单配权限，得先有一份能读的清单。这里就是那份清单：

  1. 渔枢「权限管理」页按它把渔货栈的菜单一块儿摆出来，角色里勾了才能进；
  2. 顶栏菜单按「这个账号勾了哪些」渲染（见 src/web/router.py 的 menu_allowed）。

粒度到**一级菜单**：订单下面那一排状态（待确认 / 待推单 …）跟「全部订单」走，
再细下去没人愿意维护，运营真正关心的是「进不进得了这个页面」。
"""
from __future__ import annotations

# (分组, ((key, 名称), ...))
# key 就是权限码：渔枢侧存的时候带 self: 前缀（见 app/web/permissions.py）
SELF_MENU: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("工作台", (("dashboard", "概览"),)),
    (
        "货源 · 渔货栈",
        (
            ("harbor", "货源广场"),
            ("harbor_rank", "热销榜单"),
        ),
    ),
    # 订单只给一个入口：下面那排状态是同一个页面的不同筛选
    ("交易 · 订单", (("orders", "全部订单"),)),
    (
        "交易 · 采购",
        (
            ("cart", "采购车"),
            ("purchases", "采购单"),
        ),
    ),
    (
        "店铺",
        (
            ("shops", "店铺管理"),
            # 2026-10-11 改名：以前叫「店铺商品池」（/shop-goods）
            ("shop_goods", "我的店铺商品"),
            # 收货地址：以前只写在「我的」弹窗里，不在这份清单上 → menu_can 判无权，
            # 两处菜单合并成一份后它就整个消失了（补回来，权限页也能勾）。
            ("addresses", "地址管理"),
        ),
    ),
    (
        "系统",
        (
            ("self_logs", "异常日志"),
            ("self_changelog", "更新日志"),
        ),
    ),
)

ALL_KEYS: tuple[str, ...] = tuple(
    key for _group, items in SELF_MENU for key, _label in items
)


def label_of(key: str) -> str:
    for _group, items in SELF_MENU:
        for code, label in items:
            if code == key:
                return label
    return key


def menu_tree() -> list[dict]:
    """给权限页的分组树。

    键名一律叫 menus：Jinja 里 `group.items` 会命中 dict 的 items 方法
    （踩过一次，报 'builtin_function_or_method' not iterable）。
    """
    return [
        {
            "title": group,
            "menus": [{"key": key, "label": label} for key, label in items],
        }
        for group, items in SELF_MENU
    ]
