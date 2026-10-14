"""角色权限（菜单级）。

一个账号能进哪些菜单，由三处决定（优先级从高到低）：

  1. 管理员（`users.role == 'admin'`）：全部菜单 —— 与升级前完全一致；
  2. 挂了角色（`users.role_id`）：按角色勾选的菜单；
  3. 没挂角色的子账号：默认菜单（即非 `admin_only` 的那些，也与升级前一致）。

粒度定在**菜单**上 —— 菜单就是运营实际感知的「能进哪些页面」，也正是
nav.py 里每个 NavItem 的 key（orders / products / users …）。二级菜单跟父项走：
再细到「订单归档下的某个状态」配起来没人愿意维护。

两个内置角色是**动态**的（不在表里存菜单快照）：
  · `admin` → 全部菜单。以后新增菜单，管理员自动就有，不会漏；
  · `staff` → 代码里的默认菜单。以后新增的普通菜单，普通成员也自动有。
其它角色是运营自己勾的，存 `Role.menus`。

数据级范围（只看某些供应商 / 闲鱼号）在 `Role.data_scope` 里预留，当前只存不生效：
真要用时在 `menu_keys_for` 旁边加一层过滤即可，不用再动表结构。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Role, User
from . import nav

logger = logging.getLogger(__name__)

# 内置角色 key
ROLE_KEY_ADMIN = "admin"
ROLE_KEY_STAFF = "staff"
ROLE_DEFS = (
    (ROLE_KEY_ADMIN, "管理员", "全部菜单（含解析规则、系统设置、操作日志、账号管理）"),
    (ROLE_KEY_STAFF, "普通成员", "默认菜单：除解析规则 / 系统设置 / 操作日志 / 账号管理外都能进"),
)

# 角色 → 菜单集合的缓存：权限改完即时失效（见 invalidate_cache），最差 60 秒生效
_CACHE: dict[int, tuple[float, frozenset[str] | None]] = {}
_CACHE_TTL = 60.0


def all_menu_keys() -> frozenset[str]:
    """全部可授权菜单（一级菜单，不含二级）。"""
    return frozenset(item.key for section in nav.NAV for item in section.items)


def default_menu_keys() -> frozenset[str]:
    """默认菜单 = 非 admin_only 的那些（升级前子账号能看到的菜单）+ 全部页面区块。

    页面内的区块（推单信息、合并订单）默认都能看 —— 与加这套权限之前一致；
    要收紧就建一个角色、在角色里取消勾选，再把账号挂上去（见 BLOCKS）。
    """
    return frozenset(
        item.key for section in nav.NAV for item in section.items if not item.admin_only
    ) | all_block_keys()


def menu_label_map() -> dict[str, str]:
    return {item.key: item.label for section in nav.NAV for item in section.items}


def menu_tree() -> list[dict]:
    """给页面的菜单勾选树：按板块分组，标注哪些是「管理员专属」。"""
    return [
        {
            "title": section.title,
            "admin_only": section.admin_only,
            # 字段名用 menus 而不是 items：模板里 group.items 会被 Jinja 当成
            # dict.items() 这个方法（踩过一次，报 'builtin_function_or_method' not iterable）
            "menus": [
                {"key": item.key, "label": item.label, "admin_only": item.admin_only}
                for item in section.items
            ],
        }
        for section in nav.NAV
    ]


def invalidate_cache() -> None:
    """角色菜单改动后调一次，权限立刻生效（不用等缓存过期）。"""
    _CACHE.clear()
    _SYS_CACHE.clear()


# ---------------------------------------------------------------- 三套子系统的菜单级权限
# 2026-10-11：权限管理页把渔枢 / 渔集 / 渔货栈三套菜单级权限一块儿摆出来，
# 一个角色一次勾完。存的 key 带前缀区分：hub: / wms: / self:
# 渔枢的老数据没有前缀 —— 一律当 hub: 处理，升级不用重勾一遍。
HUB_PREFIX = "hub:"
WMS_PREFIX = "wms:"
SELF_PREFIX = "self:"


def _wms_groups() -> tuple:
    """渔集（检测仓）的权限分组；没挂检测仓（或它还没装好）就给空，不连累这一页。"""
    try:
        from wms.permissions import PERM_GROUPS  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.debug("读不到渔集权限清单：%s", exc)
        return ()
    return PERM_GROUPS


def _self_groups() -> tuple:
    """渔货栈（自营端）的菜单分组（src/nav.py）。"""
    try:
        from src.nav import SELF_MENU  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.debug("读不到渔货栈菜单清单：%s", exc)
        return ()
    return SELF_MENU


def _as_groups(raw) -> list[dict]:
    return [
        {"title": title, "menus": [{"key": key, "label": label} for key, label in items]}
        for title, items in raw
    ]


def wms_keys() -> frozenset[str]:
    """渔集全部权限码。"""
    return frozenset(code for _g, items in _wms_groups() for code, _l in items)


def self_keys() -> frozenset[str]:
    """渔货栈全部菜单 key。"""
    return frozenset(key for _g, items in _self_groups() for key, _l in items)


def permission_tree() -> list[dict]:
    """三套子系统各自的菜单级权限（权限管理页按它一块儿摆出来）。"""
    return [
        {
            "key": "hub",
            "prefix": HUB_PREFIX,
            "title": "渔枢系统",
            "note": "后台菜单：粒度到一级菜单（二级跟父项走）",
            "groups": menu_tree(),
        },
        {
            "key": "wms",
            "prefix": WMS_PREFIX,
            "title": "渔集（检测仓）",
            "note": "检测仓自己的权限码（看 / 做分开）：这里勾的与检测仓「人员授权」给的权限取并集",
            "groups": _as_groups(_wms_groups()),
        },
        {
            "key": "self",
            "prefix": SELF_PREFIX,
            "title": "渔货栈（自营端）",
            "note": "自营端顶栏「自营服务」里的菜单",
            "groups": _as_groups(_self_groups()),
        },
    ]


def all_permission_keys() -> frozenset[str]:
    """全部可授权的 key：带前缀的 + 渔枢的老裸 key（见 _split_menus）。"""
    return (
        all_menu_keys()
        | frozenset(HUB_PREFIX + k for k in all_menu_keys())
        | frozenset(WMS_PREFIX + k for k in wms_keys())
        | frozenset(SELF_PREFIX + k for k in self_keys())
    )


def _split_menus(raw) -> dict[str, set[str]]:
    """把角色里存的 key 按系统拆开（前缀剥掉）。"""
    out = {"hub": set(), "wms": set(), "self": set()}
    for key in raw or []:
        text = str(key)
        if text.startswith(WMS_PREFIX):
            out["wms"].add(text[len(WMS_PREFIX):])
        elif text.startswith(SELF_PREFIX):
            out["self"].add(text[len(SELF_PREFIX):])
        elif text.startswith(HUB_PREFIX):
            out["hub"].add(text[len(HUB_PREFIX):])
        else:
            out["hub"].add(text)  # 老数据：没前缀 = 渔枢菜单
    return out


def _full_perms() -> dict[str, set[str]]:
    """三个系统全都给（管理员 / 没挂角色的账号）。"""
    return {
        "hub": set(all_menu_keys()) | all_block_keys(),
        "wms": set(wms_keys()),
        "self": set(self_keys()),
    }


def role_system_perms(role: Role | None) -> dict[str, set[str]]:
    """某个角色在三套子系统里的权限（内置角色跟着系统走）。"""
    if role is None:
        return _full_perms()
    if role.key == ROLE_KEY_ADMIN:
        return _full_perms()
    if role.key == ROLE_KEY_STAFF:
        return {
            "hub": set(default_menu_keys()),
            "wms": set(wms_keys()),
            "self": set(self_keys()),
        }
    split = _split_menus(role.menus)
    # 一个带前缀的 key 都没有 = 这个角色是加三套权限之前配的：
    #   · 渔货栈以前压根没管过菜单权限，照旧全给（不让升级把老账号的菜单弄没）；
    #   · 渔集一直有自己的「人员授权」，这里**不**放宽 —— 该在检测仓授权的还是得在那儿授权。
    raw = [str(k) for k in (role.menus or [])]
    if not any(k.startswith((HUB_PREFIX, WMS_PREFIX, SELF_PREFIX)) for k in raw):
        split["self"] = set(self_keys())
    return split


_SYS_CACHE: dict[int, tuple[float, dict[str, set[str]]]] = {}


def _role_of(session: Session | None, role_id: int) -> Role | None:
    own_session = session is None
    if own_session:
        from ..db import SessionLocal  # noqa: PLC0415

        session = SessionLocal()
    try:
        return session.get(Role, int(role_id))
    finally:
        if own_session:
            session.close()


def system_perms_for(user: User | None, session: Session | None = None) -> dict[str, set[str]]:
    """这个账号在三套子系统里的权限（带 60 秒缓存）。

    渔集与渔货栈也走同一张角色表：勾了才给，没勾就进不去（管理员与「没挂角色」
    的账号不限制 —— 与加这套权限之前一致）。
    """
    if user is None:
        return {"hub": set(), "wms": set(), "self": set()}
    if getattr(user, "is_admin", False):
        return _full_perms()
    role_id = getattr(user, "role_id", None)
    if not role_id:
        # 没挂角色：渔枢按默认的来；渔集要看它自己的「人员授权」（没授权就进不去，
        # 这一直是检测仓的规矩）；渔货栈以前没管过菜单，照旧全给。
        return {
            "hub": set(default_menu_keys()),
            "wms": set(),
            "self": set(self_keys()),
        }
    cached = _SYS_CACHE.get(int(role_id))
    now = time.monotonic()
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    perms = role_system_perms(_role_of(session, int(role_id)))
    _SYS_CACHE[int(role_id)] = (now, perms)
    return perms


def perms_of_user(user_id: int, system: str) -> set[str]:
    """给渔集 / 渔货栈用：这个账号在指定子系统里勾了哪些权限码。

    管理员与「没挂角色」的账号返回全集（等于不限制），调用方用 `code in perms` 判即可。
    """
    from ..db import SessionLocal  # noqa: PLC0415

    with SessionLocal() as session:
        user = session.get(User, int(user_id))
        return set(system_perms_for(user, session).get(system, set()))


def menu_keys_for(user: User | None, session: Session | None = None) -> frozenset[str] | None:
    """账号可见的菜单集合；返回 None 表示不限制（管理员）。"""
    if user is None:
        return frozenset()
    if getattr(user, "is_admin", False):
        return None
    role_id = getattr(user, "role_id", None)
    if not role_id:
        return default_menu_keys()
    cached = _CACHE.get(int(role_id))
    now = time.monotonic()
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    keys = _load_role_menus(session, int(role_id))
    _CACHE[int(role_id)] = (now, keys)
    return keys


def _load_role_menus(session: Session | None, role_id: int) -> frozenset[str] | None:
    own_session = session is None
    if own_session:
        from ..db import SessionLocal  # noqa: PLC0415  避免与 db 循环导入

        session = SessionLocal()
    try:
        role = session.get(Role, role_id)
        if role is None:
            logger.warning("账号挂的角色 %s 不存在，按默认菜单处理", role_id)
            return default_menu_keys()
        if role.key == ROLE_KEY_ADMIN:
            return None
        if role.key == ROLE_KEY_STAFF:
            return default_menu_keys()
        # 只取渔枢那一份（带 hub: 前缀的剥掉前缀；wms: / self: 是别的系统的，不算进来）
        return frozenset(_split_menus(role.menus)["hub"])
    finally:
        if own_session:
            session.close()


def menu_key_for_path(path: str) -> str | None:
    """路径属于哪个菜单项（按 nav 的 url / match 前缀匹配，最长前缀优先）。"""
    best: tuple[int, str] | None = None
    for section in nav.NAV:
        for item in section.items:
            for prefix in nav.item_paths(item):
                if nav._path_matches(path, prefix):  # noqa: SLF001  与菜单高亮同一套判断
                    if best is None or len(prefix) > best[0]:
                        best = (len(prefix), item.key)
    return best[1] if best else None


def can_access_path(user: User | None, path: str) -> bool:
    """这个路径能不能进。

    认得出菜单项的（列表页）按菜单权限判；认不出的（首页、详情页、接口）一律放行 ——
    详情页跟着列表页的权限走，不然点了链接才被拦会很难用。
    """
    allowed = menu_keys_for(user)
    if allowed is None:
        return True
    key = menu_key_for_path(path)
    # 认不出的路径（首页、详情页、接口）一律放行；流程说明是给人看的操作指引，谁都能看
    if key is None or key == "help":
        return True
    return key in allowed


def first_allowed_path(user: User | None) -> str:
    """这个账号能进的第一个页面。

    无权访问某页时退回它 —— 不能一律退回首页：首页本身也可能没权限，那就成死循环了。
    一个菜单都没分配时返回空字符串，由调用方给用户一句明确的提示。
    """
    allowed = menu_keys_for(user)
    if allowed is None:
        return "/"
    for section in nav.NAV:
        for item in section.items:
            if item.key in allowed and item.url:
                return item.url
    return ""


# ---------------------------------------------------------------- 页面内区块权限
# 菜单级权限管「能进哪些页面」，区块级管「同一个页面里能看到哪些内容」：
# 订单详情对运营 / 客服 / 采购是同一个页面，但推单信息（含成本、供应商、转账）和
# 合并订单是有后果的动作 —— 只给指定的人看。配的地方就是「角色权限」页，与管理菜单同一张表
# （Role.menus），不额外加表；勾了才有，没勾的账号看到的是一句「没有权限」的提示卡。
@dataclass(frozen=True)
class BlockItem:
    """一个需要单独授权的页面区块。"""

    key: str
    label: str
    hint: str = ""


BLOCKS: tuple[BlockItem, ...] = (
    # 订单详情页从上到下 8 个板块，谁看什么就在这儿勾（2026-10-01 补全）：
    # 页面上的每个板块都能单独授权 —— 运营、客服、采购看同一张单，看到的可以不一样。
    BlockItem(
        "order_tips",
        "订单提示",
        "订单详情顶部：需要人工核对、已撤单、成本未维护等提示条",
    ),
    BlockItem(
        "order_refund",
        "退单处理",
        "订单详情：退单警示、退单理由、同步供应商、是否退款、强制关闭",
    ),
    BlockItem("order_push", "推单信息", "订单详情：推单文案、是否转账、物流单号（含成本与供应商）"),
    BlockItem("order_logs", "订单处理日志", "订单详情：谁在什么时候把单子怎么了（操作留痕）"),
    BlockItem("order_info", "订单信息", "订单详情：收货信息、商品、金额、毛利等订单本体内容"),
    BlockItem("order_images", "订单截图", "订单详情：群里的订单截图与自动裁出的商品图"),
    BlockItem("order_edit", "编辑订单", "订单详情：改订单字段（会写进订单数据，改错有影响）"),
    BlockItem("order_merge", "合并订单", "订单详情：把另一笔订单并入本单并删除对方（不可撤销）"),
)


def all_block_keys() -> frozenset[str]:
    """全部可授权的页面区块 key。"""
    return frozenset(block.key for block in BLOCKS)


def block_tree() -> list[dict]:
    """给角色权限页的区块勾选区（跟菜单勾选并列展示）。"""
    return [
        {"key": block.key, "label": block.label, "hint": block.hint} for block in BLOCKS
    ]


def block_keys_for(user: User | None, session: Session | None = None) -> frozenset[str] | None:
    """账号所属角色单独配的页面区块；None = 这个角色没配过区块（按默认给）。

    内置角色（管理员 / 普通成员）与「没挂角色」都返回 None —— 它们的区块跟着代码走。
    """
    if user is None:
        return None
    own_session = session is None
    if own_session:
        from ..db import SessionLocal  # noqa: PLC0415  避免与 db 循环导入

        session = SessionLocal()
    try:
        role_id = getattr(user, "role_id", None)
        if not role_id and getattr(user, "id", None):
            # 页面上的 current_user 是脱敏对象（可能不带 role_id）：按 id 回库补一次
            fresh = session.get(User, int(user.id))
            role_id = getattr(fresh, "role_id", None) if fresh else None
        if not role_id:
            return None
        role = session.get(Role, int(role_id))
        if role is None or role.key in (ROLE_KEY_ADMIN, ROLE_KEY_STAFF):
            return None
        raw = getattr(role, "blocks", None)
        if raw is None:
            return None  # 老角色：升级前没有这个概念，沿用默认（都能看）
        return frozenset(str(key) for key in raw)
    finally:
        if own_session:
            session.close()


def can_view_block(user: User | None, key: str, session: Session | None = None) -> bool:
    """这个账号能不能看某个页面区块（管理员全看；其余按角色里勾的区块）。

    三档：管理员 / 没挂角色与「普通成员」→ 都能看（与加这套权限之前一致）；
    自己建的角色 → 按角色里勾的区块，没配过（blocks 为 None）也按默认给。
    """
    allowed = menu_keys_for(user, session)
    if allowed is None:
        return True  # 管理员
    if key in allowed:
        return True  # 默认菜单里带了全部区块
    blocks = block_keys_for(user, session)
    if blocks is None:
        return True  # 这个角色没单独配过区块
    return key in blocks


# ---------------------------------------------------------------- 角色数据
def ensure_builtin_roles(session: Session) -> int:
    """播种内置角色（幂等，启动时调）。

    内置角色的菜单是**动态**的（见模块说明），所以这里只保证记录存在、
    补一下名字与说明，不去覆盖运营可能改过的菜单。
    """
    created = 0
    for key, name, description in ROLE_DEFS:
        role = session.scalar(select(Role).where(Role.key == key))
        if role is None:
            session.add(Role(key=key, name=name, description=description, is_system=True))
            created += 1
        elif role.is_system and (role.name != name or role.description != description):
            role.name, role.description = name, description
    session.commit()
    invalidate_cache()
    return created


def role_choices(session: Session) -> list[Role]:
    """下拉用的角色列表（内置在前）。"""
    return list(session.scalars(select(Role).order_by(Role.is_system.desc(), Role.id)))


def user_count(session: Session, role_id: int) -> int:
    return len(
        list(session.scalars(select(User.id).where(User.role_id == role_id)))
    )


def rows(session: Session) -> list[dict]:
    """角色列表页的行数据：补上账号数、菜单名、是不是「全部菜单」。"""
    labels = menu_label_map()
    total = len(all_menu_keys())
    out: list[dict] = []
    for role in role_choices(session):
        systems = role_system_perms(role)
        if role.key == ROLE_KEY_ADMIN:
            keys, is_dynamic = sorted(all_menu_keys()), True
        elif role.key == ROLE_KEY_STAFF:
            keys, is_dynamic = sorted(default_menu_keys()), True
        else:
            keys, is_dynamic = sorted(str(k) for k in (role.menus or [])), False
        # 页面内区块：内置角色 / 没配过的角色都按默认（全给）回显，配过的按配的
        raw_blocks = getattr(role, "blocks", None)
        if role.key in (ROLE_KEY_ADMIN, ROLE_KEY_STAFF) or raw_blocks is None:
            block_keys, blocks_dynamic = sorted(all_block_keys()), True
        else:
            block_keys, blocks_dynamic = sorted(str(k) for k in raw_blocks), False
        out.append(
            {
                "role": role,
                # 同理不能叫 keys：模板里 row.keys 是 dict.keys() 方法
                "menu_keys": keys,
                "is_dynamic": is_dynamic,
                "block_keys": block_keys,
                "blocks_dynamic": blocks_dynamic,
                "is_all": len(keys) >= total,
                "missing_labels": [labels.get(k, k) for k in keys if k not in labels],
                "user_count": user_count(session, int(role.id)),
                # 渔集 / 渔货栈勾了哪些（权限页第二、三块用它回显，带上前缀方便模板直接比对）
                "wms_keys": [WMS_PREFIX + k for k in sorted(systems["wms"])],
                "self_keys": [SELF_PREFIX + k for k in sorted(systems["self"])],
            }
        )
    return out


def save_role(
    session: Session,
    *,
    name: str,
    menus: list[str],
    blocks: list[str] | None = None,
    description: str = "",
    role: Role | None = None,
    key: str = "",
) -> Role:
    """新建或更新一个自定义角色（内置角色的菜单由代码决定，不允许改）。

    blocks 传 None = 不动这个角色的区块配置；传列表（含空列表）= 按勾选设置，
    这样「一个都不勾」才能表达出来（与「没配过」区分开，见 Role.blocks）。
    """
    # 三套子系统的 key 都认（带前缀）；渔枢的老裸 key 也认，见 _split_menus
    valid = all_permission_keys()
    picked = [str(k) for k in menus if str(k) in valid]
    if role is None:
        role = Role(key=(key or "").strip() or _next_key(session, name))
        session.add(role)
    if role.is_system:
        raise ValueError(f"「{role.name}」是内置角色，菜单由系统决定，不能修改")
    role.name = (name or "").strip() or role.name
    role.description = (description or "").strip()
    role.menus = picked
    if blocks is not None:
        valid_blocks = all_block_keys()
        role.blocks = [str(k) for k in blocks if str(k) in valid_blocks]
    session.commit()
    invalidate_cache()
    return role


def _next_key(session: Session, name: str) -> str:
    base = "role"
    index = 1
    while session.scalar(select(Role).where(Role.key == f"{base}{index}")):
        index += 1
    return f"{base}{index}"


def delete_role(session: Session, role: Role) -> None:
    """删角色：先把挂着的账号摘回「没有角色」（按默认菜单走），再删。"""
    if role.is_system:
        raise ValueError(f"「{role.name}」是内置角色，不能删除")
    for user in session.scalars(select(User).where(User.role_id == role.id)):
        user.role_id = None
    session.delete(role)
    session.commit()
    invalidate_cache()
