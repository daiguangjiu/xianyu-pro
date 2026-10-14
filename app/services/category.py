"""商品类目（三级）：一份可维护的原始数据 + 发布页三级级联用的树。

业务口径（2026-09-28 起）：
  · 类目口径以渔货栈货源广场那套三级结构为准（10 一级 / 64 二级 / 375 三级）；
  · 这份数据在渔枢系统「货源 · 类目管理」里维护（这张表就是原始数据），
    新增商品的级联、商品详情里编辑类目的候选项，全都读它 —— 不再写死在代码里；
  · 商品上的 `Product.category` 仍是文本（"一级 / 二级 / 三级"），列表 / 统计照旧按文本走。

播种：表为空时按渔货栈常量自动导入（见 `ensure_seeded`）；之后可以在管理页增删改，
也可以再点「从渔货栈同步」补齐渔货栈新增的类目（只补不删，本地改过的名字不覆盖）。
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Product, ProductCategory, ShippingRule

logger = logging.getLogger(__name__)

# 类目数据来源：管理页上标出来，让人知道这份数据是照哪套口径来的
SEED_SOURCE = "渔货栈货源广场三级类目"
MAX_LEVEL = 3
PATH_SEP = " / "


# ------------------------------------------------------------------ 播种
def _seed_from_sourcing() -> list[dict]:
    """从渔货栈的类目常量取三级结构（一级 → 二级 → 三级名）。"""
    from src.goods.catalog import SOURCING_CATEGORY_TREE, SOURCING_SUB_LEAVES  # noqa: PLC0415

    out: list[dict] = []
    for top in SOURCING_CATEGORY_TREE:
        children = []
        for sub in top.get("children") or []:
            leaves = SOURCING_SUB_LEAVES.get(sub["id"]) or ()
            children.append(
                {
                    "id": sub["id"],
                    "name": sub["name"],
                    "children": [
                        {"id": f"{sub['id']}_l3_{i}", "name": name}
                        for i, name in enumerate(leaves)
                    ],
                }
            )
        out.append({"id": top["id"], "name": top["name"], "children": children})
    return out


def _seed_fallback() -> list[dict]:
    """渔货栈不可用时的兜底：用渔枢系统原有的二级类目（保证功能不空）。"""
    from .product import CATEGORY_TREE  # noqa: PLC0415

    return [
        {"id": "", "name": top, "children": [{"id": "", "name": sub, "children": []} for sub in subs]}
        for top, subs in CATEGORY_TREE.items()
    ]


def _sourcing_seed() -> list[dict]:
    try:
        seed = _seed_from_sourcing()
        if seed:
            return seed
    except Exception as exc:  # noqa: BLE001  渔货栈代码不在时不能把类目页搞崩
        logger.warning("读取渔货栈类目失败，改用渔枢系统内置二级类目：%s", exc)
    return _seed_fallback()


def _write_seed(session: Session, seed: Iterable[dict]) -> int:
    """把种子结构写进表（一级 → 二级 → 三级），返回写入节点数。"""
    written = 0
    for top_order, top in enumerate(seed):
        parent = ProductCategory(
            parent_id=None, level=1, name=top["name"], sort_order=top_order,
            source_id=top.get("id") or "",
        )
        session.add(parent)
        session.flush()
        written += 1
        for sub_order, sub in enumerate(top.get("children") or []):
            child = ProductCategory(
                parent_id=parent.id, level=2, name=sub["name"], sort_order=sub_order,
                source_id=sub.get("id") or "",
            )
            session.add(child)
            session.flush()
            written += 1
            for leaf_order, leaf in enumerate(sub.get("children") or []):
                session.add(
                    ProductCategory(
                        parent_id=child.id, level=3, name=leaf["name"], sort_order=leaf_order,
                        source_id=leaf.get("id") or "",
                    )
                )
                written += 1
    return written


def ensure_seeded(session: Session) -> int:
    """表为空时按渔货栈那套类目播种；已有数据就什么都不做。返回播种节点数。"""
    exists = session.scalar(select(func.count()).select_from(ProductCategory))
    if exists:
        return 0
    written = _write_seed(session, _sourcing_seed())
    session.commit()
    logger.info("商品类目首次播种：%s 个节点（来源：%s）", written, SEED_SOURCE)
    return written


def sync_from_sourcing(session: Session) -> dict:
    """按渔货栈当前常量补齐 / 改名（只补不删：本地自建的类目保留）。

    以 `source_id` 认领同一个节点：
      · 渔货栈有、本地没有 → 新增；
      · 两边都有 → 名称以渔货栈为准（保留本地排序与停用状态）；
      · 本地有、渔货栈没有 → 原样保留（可能是本地自建的）。
    """
    seed = _sourcing_seed()
    by_source = {
        node.source_id: node
        for node in session.scalars(select(ProductCategory))
        if node.source_id
    }
    added = renamed = 0
    # 新增的节点排在同级末尾：运营调过的一级类目顺序不被同步打乱
    tail_order: dict[int | None, int] = {}

    def next_order(parent_id: int | None) -> int:
        if parent_id not in tail_order:
            current = session.scalar(
                select(func.coalesce(func.max(ProductCategory.sort_order), -1)).where(
                    ProductCategory.parent_id.is_(None)
                    if parent_id is None
                    else ProductCategory.parent_id == parent_id
                )
            )
            tail_order[parent_id] = int(current or -1)
        tail_order[parent_id] += 1
        return tail_order[parent_id]

    def walk(items: list[dict], parent: ProductCategory | None, level: int) -> None:
        nonlocal added, renamed
        for item in items:
            source_id = str(item.get("id") or "")
            node = by_source.get(source_id) if source_id else None
            if node is None:
                node = ProductCategory(
                    parent_id=parent.id if parent else None,
                    level=level,
                    name=item["name"],
                    sort_order=next_order(parent.id if parent else None),
                    source_id=source_id,
                )
                session.add(node)
                session.flush()
                by_source[source_id] = node
                added += 1
            else:
                if node.name != item["name"]:
                    node.name = item["name"]
                    renamed += 1
                node.parent_id = parent.id if parent else None
                node.level = level
            walk(item.get("children") or [], node, level + 1)

    walk(seed, None, 1)
    session.commit()
    return {"added": added, "renamed": renamed}


# ------------------------------------------------------------------ 读
def _sorted_nodes(session: Session, enabled_only: bool = False) -> list[ProductCategory]:
    stmt = select(ProductCategory).order_by(ProductCategory.sort_order, ProductCategory.id)
    if enabled_only:
        stmt = stmt.where(ProductCategory.enabled.is_(True))
    return list(session.scalars(stmt))


def tree(session: Session, enabled_only: bool = False) -> list[dict]:
    """三级树（管理页与发布页共用）：`[{id,name,level,enabled,children:[...]}]`。

    enabled_only=True 时把停用节点连同其子树一起摘掉 —— 发布页要的就是能选的项。
    """
    nodes = _sorted_nodes(session)
    by_parent: dict[int | None, list[ProductCategory]] = {}
    for node in nodes:
        by_parent.setdefault(node.parent_id, []).append(node)
    if enabled_only:
        keep: set[int] = set()

        def mark(parent_id: int | None) -> None:
            for node in by_parent.get(parent_id, []):
                if not node.enabled:
                    continue
                keep.add(node.id)
                mark(node.id)

        mark(None)
        nodes = [n for n in nodes if n.id in keep]
        by_parent = {}
        for node in nodes:
            by_parent.setdefault(node.parent_id, []).append(node)

    def build(parent_id: int | None) -> list[dict]:
        out = []
        for node in by_parent.get(parent_id, []):
            out.append(
                {
                    "id": node.id,
                    "name": node.name,
                    "level": node.level,
                    "enabled": bool(node.enabled),
                    "source_id": node.source_id or "",
                    # 类目特性规则（一级类目上维护的经验规则；不影响类目显示）
                    "rules": node.rules or "",
                    "children": build(node.id),
                }
            )
        return out

    return build(None)


def options(session: Session) -> list[dict]:
    """发布页级联用的精简树（只含启用的节点，字段压到最短，直接喂给前端 JSON）。"""
    def slim(items: list[dict]) -> list[dict]:
        return [
            {"n": item["name"], "c": slim(item["children"])}
            for item in items
        ]

    return slim(tree(session, enabled_only=True))


def stats(session: Session) -> dict:
    """管理页顶部的概览：各级数量 + 商品用到的类目路径数。"""
    rows = list(session.scalars(select(ProductCategory)))
    used = session.scalar(
        select(func.count(func.distinct(Product.category))).where(Product.category != "")
    )
    return {
        "level1": sum(1 for r in rows if r.level == 1),
        "level2": sum(1 for r in rows if r.level == 2),
        "level3": sum(1 for r in rows if r.level == 3),
        "disabled": sum(1 for r in rows if not r.enabled),
        "used": int(used or 0),
    }


def usage_counts(session: Session) -> dict[int, int]:
    """每个类目节点被多少商品用到（按该节点的完整路径文本匹配，删除前提示用）。"""
    paths = {node.id: path_text(session, node.id) for node in session.scalars(select(ProductCategory))}
    existing: dict[str, int] = {}
    for value, count in session.execute(
        select(Product.category, func.count()).where(Product.category != "").group_by(Product.category)
    ):
        existing[str(value)] = int(count)
    return {node_id: existing.get(path, 0) for node_id, path in paths.items()}


def path_text(session: Session, node_id: int) -> str:
    """节点的完整路径文本："一级 / 二级 / 三级"（与 Product.category 同一口径）。"""
    names: list[str] = []
    node = session.get(ProductCategory, node_id)
    guard = 0
    while node is not None and guard < MAX_LEVEL + 1:
        names.append(node.name)
        node = session.get(ProductCategory, node.parent_id) if node.parent_id else None
        guard += 1
    return PATH_SEP.join(reversed(names))


def path_texts(session: Session, enabled_only: bool = True) -> list[str]:
    """全部类目路径（含一级、二级、三级各自的完整路径），给「编辑商品」的类目候选。"""
    out: list[str] = []

    def walk(items: list[dict], prefix: list[str]) -> None:
        for item in items:
            names = prefix + [item["name"]]
            out.append(PATH_SEP.join(names))
            walk(item["children"], names)

    walk(tree(session, enabled_only=enabled_only), [])
    return out


def suggestions(session: Session) -> list[str]:
    """类目候选（编辑商品时的下拉）：类目表里的全部路径 + 库里商品已经在用的写法。

    老数据（两级文本，如「数码配件 / 耳机音响」）也要留着 —— 不然编辑老商品时
    它的类目在下拉里找不到，一保存就被改没了。
    """
    values = path_texts(session)
    for existing in session.scalars(
        select(Product.category).where(Product.category != "").distinct()
    ):
        text_value = str(existing or "")
        if text_value and text_value not in values:
            values.append(text_value)
    return values


def text(cat1: str, cat2: str = "", cat3: str = "") -> str:
    """三级拼成商品上的类目文本（空层级自动跳过，超长截断到字段长度上限）。"""
    parts = [p.strip() for p in (cat1, cat2, cat3) if (p or "").strip()]
    return PATH_SEP.join(parts)[:128]


def split_text(value: str) -> tuple[str, str, str]:
    """把商品上的类目文本拆回（一级, 二级, 三级），编辑页的回填用。

    老数据写法五花八门（只有一级、多一段、用 / 或 ＞ 分隔），统一按分隔符切开、
    取前三段，缺的补空串 —— 前端联动下拉能对上就选中，对不上就落回「不细分」。
    """
    raw = str(value or "").replace("＞", PATH_SEP).replace(">", PATH_SEP)
    parts = [p.strip() for p in raw.split(PATH_SEP)]
    return (parts + ["", "", ""])[:3]


# ------------------------------------------------------------------ 写
def sibling_names(session: Session, parent_id: int | None, exclude_id: int | None = None) -> set[str]:
    stmt = select(ProductCategory).where(
        ProductCategory.parent_id.is_(None) if parent_id is None else ProductCategory.parent_id == parent_id
    )
    return {
        node.name
        for node in session.scalars(stmt)
        if exclude_id is None or node.id != exclude_id
    }


def create_node(session: Session, parent_id: int | None, name: str) -> ProductCategory | None:
    """在某个父节点下新增类目；重名或层级超过三级时返回 None。"""
    name = (name or "").strip()[:64]
    if not name:
        return None
    parent = session.get(ProductCategory, parent_id) if parent_id else None
    level = (parent.level + 1) if parent else 1
    if level > MAX_LEVEL:
        return None
    if name in sibling_names(session, parent.id if parent else None):
        return None
    order = session.scalar(
        select(func.coalesce(func.max(ProductCategory.sort_order), -1)).where(
            ProductCategory.parent_id.is_(None) if parent is None else ProductCategory.parent_id == parent.id
        )
    )
    node = ProductCategory(parent_id=parent.id if parent else None, level=level, name=name,
                           sort_order=int(order or -1) + 1)
    session.add(node)
    session.commit()
    return node


def rename_node(session: Session, node_id: int, name: str) -> bool:
    node = session.get(ProductCategory, node_id)
    name = (name or "").strip()[:64]
    if node is None or not name:
        return False
    if name != node.name and name in sibling_names(session, node.parent_id, exclude_id=node.id):
        return False
    node.name = name
    session.commit()
    refresh_front_cache()
    return True


def toggle_node(session: Session, node_id: int, enabled: bool) -> bool:
    node = session.get(ProductCategory, node_id)
    if node is None:
        return False
    node.enabled = bool(enabled)
    session.commit()
    refresh_front_cache()
    return True


def refresh_front_cache() -> None:
    """类目改完（排序 / 改名 / 停用）立刻刷新渔货栈那份类目缓存。

    渔货栈的类目有 60 秒缓存，不然「调完顺序前台没变」会让人以为没生效。
    这里主动刷一次，改完即所见。
    """
    try:
        from src.goods.catalog import hub_category_rows  # noqa: PLC0415  渔货栈模块
    except Exception:  # noqa: BLE001  独立部署没有渔货栈代码就算了
        return
    try:
        hub_category_rows(force=True)
    except Exception as exc:  # noqa: BLE001  刷缓存失败不影响类目本身
        logger.debug("刷新渔货栈类目缓存失败：%s", exc)


def move_node(session: Session, node_id: int, delta: int) -> bool:
    """同级排序：delta=-1 上移一位，+1 下移一位（管理页的箭头）。

    一级类目的顺序就是前台（发布商品类目级联、渔货栈货源广场）的展示与选择顺序。
    """
    node = session.get(ProductCategory, node_id)
    if node is None:
        return False
    siblings = [
        n for n in _sorted_nodes(session)
        if n.parent_id == node.parent_id
    ]
    index = next((i for i, n in enumerate(siblings) if n.id == node.id), None)
    if index is None:
        return False
    target = index + delta
    if not 0 <= target < len(siblings):
        return False
    siblings[index], siblings[target] = siblings[target], siblings[index]
    for order, item in enumerate(siblings):
        item.sort_order = order
    session.commit()
    refresh_front_cache()
    return True


def reorder_nodes(session: Session, node_ids: list[int]) -> bool:
    """按传进来的顺序重排同级类目（管理页拖拽排序）。

    只接受同一层的类目（跨层拖拽不认）；没在列表里的同级类目保持原相对顺序排在后面。
    一级类目的顺序就是前台（发布商品类目级联、渔货栈货源广场）的展示与选择顺序。
    """
    wanted: list[ProductCategory] = []
    for raw in node_ids:
        node = session.get(ProductCategory, int(raw))
        if node is not None and node not in wanted:
            wanted.append(node)
    if not wanted:
        return False
    if len({n.parent_id for n in wanted}) > 1:
        return False  # 跨层拖拽：不猜用户意图
    parent_id = wanted[0].parent_id
    placed = {id(n) for n in wanted}
    siblings = [n for n in _sorted_nodes(session) if n.parent_id == parent_id]
    for order, node in enumerate(wanted + [n for n in siblings if id(n) not in placed]):
        node.sort_order = order
    session.commit()
    refresh_front_cache()
    return True


def delete_node(session: Session, node_id: int) -> int:
    """删除节点（连同其下所有子节点），返回删除数量；节点不存在返回 0。"""
    node = session.get(ProductCategory, node_id)
    if node is None:
        return 0
    doomed: list[ProductCategory] = []

    def collect(item: ProductCategory) -> None:
        doomed.append(item)
        for child in session.scalars(
            select(ProductCategory).where(ProductCategory.parent_id == item.id)
        ):
            collect(child)

    collect(node)
    ids = [int(item.id) for item in doomed]
    for item in doomed:
        session.delete(item)
    # 运费模板 / 发货方式里关联到这个类目的规则：把关联解掉（规则本身留着当全局规则用）
    for rule in session.scalars(select(ShippingRule).where(ShippingRule.category_id.in_(ids))):
        rule.category_id = None
        rule.category_text = ""
    session.commit()
    return len(doomed)


# ------------------------------------------------------------------ 类目特性规则
# 一级类目上维护的「经验规则」：不影响类目显示，只用于
#   1) 生成商品标题（品牌词 + 产品词 + 修饰词 + 热搜词 + 长尾词）；
#   2) 推荐商品别名；
#   3) 上架商品时把经营经验提示给运营（商品详情页的「类目经营提示」）。
RULES_TEMPLATE = """产品词：无线蓝牙耳机
修饰词：全新未拆封、正品保障、官方标配
热搜词：降噪、长续航、低延迟
长尾词：送朋友、学生党、通勤、打游戏
经营提示：客单价 100-300 元；主图用白底；买家最关心续航与音质"""

# 系统默认规则：按一级类目名给一套「够用」的词库与经验，运营在类目管理里随时改 / 删
_DEFAULT_TIP = "系统默认规则（可改）：标题按「品牌词 + 产品词 + 修饰词 + 热搜词 + 长尾词」填满 30 字更好卖"
DEFAULT_RULES_BY_CATEGORY: dict[str, str] = {
    "家用电器": """产品词：家用电器、小家电
修饰词：全新未拆封、正品保障、官方标配
热搜词：爆款、性价比、家用
长尾词：租房、送父母、搬家必备
经营提示：标品多比价，先查同款别家价；主图要有尺寸与插头细节；大家电优先自提或按距离付费""",
    "女装男装": """产品词：外套、T恤、卫衣
修饰词：新款、显瘦、百搭
热搜词：爆款、气质、ins风
长尾词：通勤、约会、学生党
经营提示：尺码表必填（退货多在这一项）；成色与瑕疵照实写；主图优先真人上身图""",
    "家纺家饰": """产品词：四件套、被芯、窗帘
修饰词：全棉、加厚、亲肤
热搜词：爆款、北欧风、家用
长尾词：婚房、租房、送长辈
经营提示：尺寸与材质（全棉 / 涤棉）必写；重货运费高，优先包邮门槛或按距离付费""",
    "食品生鲜": """产品词：零食、特产、生鲜
修饰词：新日期、产地直发、整箱
热搜词：爆款、好吃、囤货
长尾词：追剧、办公室、送礼
经营提示：保质期与生产日期必写，临期要在标题里说清；生鲜不支持七天无理由的要写明""",
    "个护家清": """产品词：洗护、清洁用品、纸品
修饰词：大容量、家庭装、温和
热搜词：爆款、囤货、平价
长尾词：家用、宿舍、母婴可用
经营提示：规格（ml / 抽数）写清，避免到手嫌小；整箱重货优先按距离付费""",
    "餐饮饮具": """产品词：水杯、餐具、厨具
修饰词：高颜值、加厚、食品级
热搜词：爆款、ins风、家用
长尾词：办公室、露营、送朋友
经营提示：材质（玻璃 / 陶瓷 / 不锈钢）必写；易碎品写明包装与破损赔付""",
    "办公文化": """产品词：文具、笔记本、办公用品
修饰词：全套、加厚、高颜值
热搜词：爆款、开学季、平价
长尾词：学生党、办公室、送礼
经营提示：套装写清件数；走量的先看库存深度，避免超卖""",
    "母婴玩具": """产品词：玩具、母婴用品、益智
修饰词：安全材质、正版、亲子
热搜词：爆款、益智、耐用
长尾词：送孩子、幼儿园、亲子互动
经营提示：适用年龄与安全材质必写；入口类（奶瓶 / 牙胶）谨慎，照实说明来源""",
    "五金工具": """产品词：工具、五金配件、电动工具
修饰词：加厚、耐用、全套
热搜词：爆款、专业、性价比
长尾词：家用、装修、汽修
经营提示：型号规格必写（买家按型号搜）；重货运费高，优先自提或按距离付费""",
    "汽车用品": """产品词：车载、汽车装饰、车用电器
修饰词：专车专用、高清、耐用
热搜词：爆款、改装、实用
长尾词：新手、长途、送车主
经营提示：适配车型 / 年份必写；大件优先自提或按距离付费""",
}
_DEFAULT_RULES_GENERIC = """产品词：{name}
修饰词：全新、正品保障、现货速发
热搜词：爆款、性价比
长尾词：家用、送朋友
经营提示：主图 5 张以上（含细节与包装）；重货优先自提或按距离付费"""


def default_rules_text(category_name: str) -> str:
    """一级类目名 → 系统默认的类目特性规则文本。"""
    name = (category_name or "").strip()
    body = DEFAULT_RULES_BY_CATEGORY.get(name)
    if body:
        return f"{body.strip()}\n{_DEFAULT_TIP}"
    return f"{_DEFAULT_RULES_GENERIC.strip().replace('{name}', name or '本类目')}\n{_DEFAULT_TIP}"


def ensure_default_rules(session: Session) -> int:
    """给「还没维护规则」的一级类目写一套系统默认规则（幂等，可每次启动调）。"""
    rows = list(
        session.scalars(select(ProductCategory).where(ProductCategory.parent_id.is_(None)))
    )
    written = 0
    for node in rows:
        if (node.rules or "").strip():
            continue
        node.rules = default_rules_text(node.name)
        written += 1
    if written:
        session.commit()
    return written


_RULE_KEYS: dict[str, str] = {
    "产品词": "product", "产品": "product", "核心词": "product", "品类词": "product",
    "修饰词": "modifier", "卖点词": "modifier", "形容词": "modifier",
    "热搜词": "hot", "热词": "hot", "搜索词": "hot", "流量词": "hot",
    "长尾词": "longtail", "场景词": "longtail", "人群词": "longtail",
    "经营提示": "tips", "提示": "tips", "经验": "tips", "经营经验": "tips", "规则": "tips",
}
_VALUE_SPLIT = re.compile(r"[、,，;；/|\s]+")


def parse_rules(text: str | None) -> dict[str, list[str]]:
    """把类目特性规则文本解析成词库。

    每行一条「键：值」，值用顿号 / 逗号 / 空格分隔；认不出的键（或没有冒号的行）
    一律当作「经营提示」，所以随便写一段经验也能用，不会白填。
    """
    out: dict[str, list[str]] = {"product": [], "modifier": [], "hot": [], "longtail": [], "tips": []}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        head, sep, tail = line.partition("：")
        if not sep:
            head, sep, tail = line.partition(":")
        key = _RULE_KEYS.get(head.strip()) if sep else None
        body = tail.strip() if key else line
        if key is None:
            out["tips"].append(line)
            continue
        if key == "tips":
            out["tips"].append(body or head.strip())
            continue
        for word in _VALUE_SPLIT.split(body):
            word = word.strip()
            if word and word not in out[key]:
                out[key].append(word)
    return out


def set_rules(session: Session, node_id: int, text: str) -> bool:
    """保存类目特性规则。"""
    node = session.get(ProductCategory, node_id)
    if node is None:
        return False
    node.rules = str(text or "").strip()
    session.commit()
    return True


def rules_for(session: Session, category_text: str | None) -> dict:
    """按商品类目文本取类目特性规则（一级类目优先，其次二级）。

    返回 `{"category": 命中的类目名, "rules": {词库}, "raw": 原文}`；
    没维护过就返回空词库，调用方自己兜底。
    """
    empty = {"product": [], "modifier": [], "hot": [], "longtail": [], "tips": []}
    names = [part.strip() for part in str(category_text or "").replace("＞", PATH_SEP).split(PATH_SEP) if part.strip()]
    if not names:
        return {"category": "", "rules": empty, "raw": ""}
    rows = list(session.scalars(select(ProductCategory)))
    by_parent: dict[int | None, list[ProductCategory]] = {}
    for row in rows:
        by_parent.setdefault(row.parent_id, []).append(row)
    chain: list[ProductCategory] = []
    parent: int | None = None
    for name in names[:MAX_LEVEL]:
        node = next((r for r in by_parent.get(parent, []) if r.name == name), None)
        if node is None:
            break
        chain.append(node)
        parent = int(node.id)
    for node in chain:  # 一级优先
        if (node.rules or "").strip():
            return {"category": node.name, "rules": parse_rules(node.rules), "raw": node.rules, "node_id": int(node.id)}
    return {"category": chain[0].name if chain else "", "rules": empty, "raw": ""}
