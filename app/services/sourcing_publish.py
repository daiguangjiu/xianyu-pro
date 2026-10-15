"""把渔枢系统商品「上架到渔货栈」（= 渔货栈商品池 supplier_items）。

口径（2026-10-02 定）：
    渔枢系统是**商品内容的生产方**（商品档案在这里维护），
    **上架到渔货栈 = 直接把商品写进渔货栈商品池，并按类目落到渔货栈对应的二级类目下** ——
    渔货栈打开渔货栈就能看到、搜到，自己去自己的店铺发布。

    闲鱼发布动作**不在渔枢系统端做**（闲鱼没开放卖家发布接口）：渔枢系统端不再生成「闲鱼发布
    预填包」、不再打开闲鱼发布页，那套只跟渔货栈分销商自己有关。

幂等：同款商品按 `item_id = hub-prd-<商品 id>` 认领，已存在就更新资料，不重复建。
类目对不上（商品没填类目 / 只填到一级 / 类目在渔货栈里不存在）时**拒绝上架并说明原因**，
不让商品悄悄落到「其他」里。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from ..models import Product, ProductCategory
from ..timeutil import now

logger = logging.getLogger(__name__)

# 渔货栈商品池里的 item_id 前缀：渔枢系统商品上架过来的都用它，便于认领与下架
ITEM_ID_PREFIX = "hub-prd-"

# 类目文本的分隔符：渔枢系统端是 "一级 / 二级 / 三级"
_LEVEL_SEP = re.compile(r"[/｜|>]+")
# 三级类目的 source_id 形如 ap_women_l3_0 → 二级 id 是 ap_women
_L3_SUFFIX = re.compile(r"_l3_\d+$")
# 类目名里的分隔符 / 标点（比较名字前先抹掉，见 _norm_name）
_PUNCT_SEP = re.compile(r"[\s/、，,·\-—_｜|>]+")

# 老类目名 / 常见叫法 → (渔货栈一级类目, 渔货栈二级类目)。
# 渔枢系统端历史商品的类目是自由文本（「数码配件」「家居家纺」「服饰鞋包」…），
# 跟渔货栈那套「家用电器 / 家纺家饰 / 女装男装」对不上名 —— 上架时按这张表就近认领，
# 并在提示里让运营核对。命中规则：取文本里命中的**最长键**（越具体越优先）。
_LEGACY_CATEGORY_MAP: dict[str, tuple[str, str]] = {
    # 数码 / 电器类
    "数码配件": ("家用电器", "手机配件"),
    "数码": ("家用电器", "手机配件"),
    "手机": ("家用电器", "手机配件"),
    "电脑": ("家用电器", "电脑外设"),
    "耳机": ("家用电器", "耳机音响"),
    "音箱": ("家用电器", "耳机音响"),
    "充电": ("家用电器", "手机充电设备"),
    "智能": ("家用电器", "智能设备"),
    "小家电": ("家用电器", "生活电器"),
    "家电": ("家用电器", "生活电器"),
    "电器": ("家用电器", "生活电器"),
    # 家居家纺
    "家居家纺": ("家纺家饰", "家居饰品"),
    "家纺": ("家纺家饰", "床上用品"),
    "床品": ("家纺家饰", "床上用品"),
    "家居": ("家纺家饰", "家居饰品"),
    "灯具": ("家纺家饰", "灯具照明"),
    # 服饰鞋包
    "服饰鞋包": ("女装男装", "服饰配件"),
    "服饰配件": ("女装男装", "服饰配件"),
    "女装": ("女装男装", "女装"),
    "男装": ("女装男装", "男装"),
    "服饰": ("女装男装", "服饰配件"),
    "箱包": ("女装男装", "箱包皮具"),
    "鞋": ("女装男装", "鞋靴"),
    # 个护家清 / 美妆
    "美妆": ("个护家清", "美容护肤"),
    "护肤": ("个护家清", "美容护肤"),
    "彩妆": ("个护家清", "彩妆香氛"),
    "个护": ("个护家清", "身体护理"),
    "洗发": ("个护家清", "洗发护发"),
    # 食品 / 母婴 / 办公 / 五金 / 汽车 / 餐饮
    "食品": ("食品生鲜", "休闲零食"),
    "零食": ("食品生鲜", "休闲零食"),
    "生鲜": ("食品生鲜", "生鲜果蔬"),
    "母婴": ("母婴玩具", "母婴用品"),
    "玩具": ("母婴玩具", "玩具"),
    "童装": ("母婴玩具", "童装童鞋"),
    "文具": ("办公文化", "学习文具"),
    "办公": ("办公文化", "办公设备"),
    "图书": ("办公文化", "图书文娱"),
    "五金": ("五金工具", "五金工具"),
    "工具": ("五金工具", "五金工具"),
    "锁": ("五金工具", "锁具门配"),
    "汽车": ("汽车用品", "汽车配件"),
    "车品": ("汽车用品", "车载电器"),
    "宠物": ("汽车用品", "宠物用品"),
    "厨房": ("餐饮饮具", "厨房工具"),
    "餐具": ("餐饮饮具", "餐具"),
    "杯壶": ("餐饮饮具", "杯壶水具"),
    "清洁": ("餐饮饮具", "清洁用品"),
}


def item_id_for(product: Product) -> str:
    """渔枢系统商品在渔货栈商品池里的 item_id（稳定、可反查）。"""
    return f"{ITEM_ID_PREFIX}{product.id}"


@dataclass
class CategoryHit:
    """类目解析结果。"""

    category_id: str = ""      # 渔货栈二级类目 id（渔货栈 SupplierItem.category_id 用这个）
    top_name: str = ""         # 一级类目名
    sub_name: str = ""         # 二级类目名
    leaf_name: str = ""        # 三级类目名（认到了才有，写进 supplier_items.category_l3）
    note: str = ""             # 解析说明 / 失败原因（页面直接展示）

    @property
    def ok(self) -> bool:
        return bool(self.category_id)

    @property
    def text(self) -> str:
        parts = [p for p in (self.top_name, self.sub_name, self.leaf_name) if p]
        return " / ".join(parts)


def _sub_id_of(source_id: str) -> str:
    """类目的 source_id → 渔货栈二级类目 id（三级 source_id 去掉 `_l3_N` 后缀）。"""
    return _L3_SUFFIX.sub("", (source_id or "").strip())


def _norm_name(name: str) -> str:
    """类目名归一化：只留文字本身，去掉分隔符与空白。

    渔枢系统商品档案里的类目是自由文本（「个护家清」「服饰配件饰品」），渔货栈那套
    1688 类目名里带分隔符（「个护/家清」「服饰配件、饰品」）—— 只差一个标点就认不到，
    商品会被拒在门外。比较前统一把 / 、 ， · - _ 这类东西抹掉，两边才对得上。
    """
    return _PUNCT_SEP.sub("", (name or "").strip())


def _pick_by_name(rows: list[ProductCategory], want: str) -> ProductCategory | None:
    """按名字认领：精确 → 归一化后相等 → 归一化后互相包含。"""
    if not want:
        return None
    hit = next((r for r in rows if (r.name or "") == want), None)
    if hit is not None:
        return hit
    norm_want = _norm_name(want)
    if not norm_want:
        return None
    hit = next((r for r in rows if _norm_name(r.name) == norm_want), None)
    if hit is not None:
        return hit
    return next(
        (
            r
            for r in rows
            if _norm_name(r.name) and (_norm_name(r.name) in norm_want or norm_want in _norm_name(r.name))
        ),
        None,
    )


def _guess_leaf_by_text(leaves: list[ProductCategory], text: str) -> ProductCategory | None:
    """联想扩展：标题 / 别名里出现了哪个三级类目名（**最长的那个**优先）。

    渔货栈的三级类目是「移动电源」「蓝牙耳机」这种具体到品类的词，商品标题里
    多半就写着它；标题认得到的三级比「该二级下第一个三级」准得多，货源广场按
    三级筛选时这件货也才捞得出来（过滤先认 category_l3，认不到才回落标题匹配）。
    只认两个字以上的名字 —— 单字（「灯」「包」）会在标题里到处误命中。
    """
    norm_text = _norm_name(text)
    if not norm_text:
        return None
    best = None
    best_len = 0
    for row in leaves:
        name = _norm_name(row.name)
        if len(name) < 2:
            continue
        if name in norm_text and len(name) > best_len:
            best, best_len = row, len(name)
    return best


def _resolve_leaf(
    rows: list[ProductCategory],
    by_parent: dict[int, list[ProductCategory]],
    tops: list[ProductCategory],
    top: ProductCategory,
    sub: ProductCategory,
    parts: list[str],
    hint_text: str,
    blocked: str = "",
) -> tuple[ProductCategory | None, ProductCategory, ProductCategory, str]:
    """三级类目：填了按名认领，没填就按标题 / 别名联想（必要时连带换一级 / 二级）。

    返回 `(三级, 生效的一级, 生效的二级, 提示语)`。认不到三级时三级是 None ——
    商品照旧挂在二级下（与改动前一致），不会因此被拒绝上架。
    """
    want = parts[2] if len(parts) > 2 else ""
    leaves = sorted(by_parent.get(sub.id, []), key=lambda r: (r.sort_order, r.id))
    if want:
        hit = _pick_by_name(leaves, want)
        if hit is not None:
            return hit, top, sub, ""

    guess = _guess_leaf_by_text(leaves, hint_text)
    if guess is not None:
        return guess, top, sub, f"按标题里的「{guess.name}」认到三级类目"

    # 二级是「该一级下第一个二级」这种兜底结果时，真正的三级常常挂在同级的别的
    # 二级下面（充电宝标题里写着「移动电源」，而它挂在别的二级下）→ 换过去更准
    for other in sorted(by_parent.get(top.id, []), key=lambda r: (r.sort_order, r.id)):
        if other.id == sub.id:
            continue
        moved = _guess_leaf_by_text(
            sorted(by_parent.get(other.id, []), key=lambda r: (r.sort_order, r.id)), hint_text
        )
        if moved is not None:
            return (
                moved,
                top,
                other,
                f"按标题里的「{moved.name}」认到三级类目（挂在「{other.name}」下）",
            )

    # 全树兜底：商品类目常填得很粗（只填到一级）甚至填错，这时标题里写的品类词才是
    # 更可靠的信号 —— 全树找一遍（最长命中优先），认到就一级 / 二级 / 三级一起换过去。
    # 1688 那套三级一万多个，一次遍历是几十万次子串判断，上架时跑一次可以接受。
    #
    # 这一步会跨一级改挂，误挂的代价最大，所以两条护栏：
    #   · 只认**三个字以上**的三级名 —— 两字太容易是品牌 / 尺寸 / 形容词
    #     （「苹果三代笔」撞上水果「苹果」、「19cm高×66cm」撞上乐器「大号」、
    #      「花生企鹅」撞上坚果「花生」）；当前一级内的联想没有这条限制。
    #   · 命中词正好是商品品牌时跳过（品牌名常常也是某个品类的名字）。
    norm_hint = _norm_name(hint_text)
    blocked_norm = _norm_name(blocked)
    best = None
    best_len = 0
    for cand in rows:
        if cand.level != 3:
            continue
        name = _norm_name(cand.name)
        if len(name) < 3 or name not in norm_hint:
            continue
        if blocked_norm and name == blocked_norm:
            continue
        if len(name) > best_len:
            best, best_len = cand, len(name)
    if best is not None:
        moved_sub = next((r for r in rows if r.id == best.parent_id), None)
        moved_top = (
            next((t for t in tops if t.id == moved_sub.parent_id), None)
            if moved_sub is not None
            else None
        )
        if moved_sub is not None and moved_top is not None:
            return (
                best,
                moved_top,
                moved_sub,
                f"按标题里的「{best.name}」认到三级类目"
                f"（从「{top.name}」改挂到「{moved_top.name} / {moved_sub.name}」下）",
            )
    return None, top, sub, ""


def load_categories(db) -> list[ProductCategory]:
    """启用的类目（列表页批量解析时预取一次，避免每条商品都查一遍）。"""
    return list(
        db.scalars(
            select(ProductCategory).where(ProductCategory.enabled == True)  # noqa: E712
        ).all()
    )


def resolve_category(db, product: Product, rows: list[ProductCategory] | None = None) -> CategoryHit:
    """渔枢系统商品类目文本 → 渔货栈类目（两级）。

    认领顺序（越靠后提示越明确）：
      1. 一级 / 二级名精确对上渔货栈类目（新商品在渔枢系统端就是按渔货栈三级类目选的，能对上）；
      2. 一级名互相包含（如「小家电」对上「家用电器」）；
      3. 老类目名 / 常见叫法兜底（见 `_LEGACY_CATEGORY_MAP`）—— 渔枢系统端历史商品用的是
         自由文本（「数码配件」「家居家纺」「服饰鞋包」），按这张表认领最近的类目，
         并在提示里让运营核对（不静默塞进「其他」）。
    二级认不到就用该一级下第一个二级，同样在提示里说明。

    三级（2026-10-07）：填了按名认领，**没填就按标题 / 别名联想**——渔货栈的三级是
    「移动电源」「蓝牙耳机」这种具体到品类的词，商品标题里多半就写着它。认到三级会
    写进 `supplier_items.category_l3`，货源广场按三级筛货时才捞得到（见 `_resolve_leaf`）。
    """
    text = (product.category or "").strip()
    if not text:
        return CategoryHit(note="商品还没填类目：请在商品档案里把类目选到二级或三级")

    parts = [p.strip() for p in _LEVEL_SEP.split(text) if p.strip()]
    rows = load_categories(db) if rows is None else rows
    by_parent: dict[int, list[ProductCategory]] = {}
    for row in rows:
        by_parent.setdefault(row.parent_id or 0, []).append(row)
    tops = by_parent.get(0, [])

    # —— 一级：精确 / 归一 → 包含 → 老类目名兜底 ——
    top = _pick_by_name(tops, parts[0])
    if top is None:
        top = next(
            (r for r in tops if _norm_name(r.name) and _norm_name(r.name) in _norm_name(parts[0])),
            None,
        )
    legacy_sub = ""
    top_via_alias = False
    alias_key = _longest_alias(text)
    if top is None and alias_key:
        legacy_top, legacy_sub = _LEGACY_CATEGORY_MAP[alias_key]
        top = _pick_by_name(tops, legacy_top)
        top_via_alias = top is not None
    if top is None:
        return CategoryHit(
            note=f"类目「{parts[0]}」在渔货栈类目里找不到：请按渔货栈的类目名重新填（或到「货源 · 类目管理」看看叫什么）"
        )

    # —— 二级：精确 / 归一 → 包含 → 老类目名兜底 ——
    children = sorted(by_parent.get(top.id, []), key=lambda r: (r.sort_order, r.id))
    want = parts[1] if len(parts) > 1 else ""
    sub = _pick_by_name(children, want)
    # 二级名挪到别的一级下了（类目表换成 1688 那套之后很常见：以前「手机配件」挂在
    # 「家用电器」下，现在归在「数码、电脑」下）→ 全树按名字找一次，
    # 别让商品稀里糊涂落到「该一级下第一个二级」
    cross_top = ""
    cross_from = ""
    if sub is None and want:
        for cand in rows:
            if cand.level == 2 and _norm_name(cand.name) == _norm_name(want) and cand.parent_id != top.id:
                # 一级跟着换过去：不然提示里会出现「家用电器 / 手机配件」这种
                # 上一级和下级对不上的说法
                cross_from = top.name
                moved_top = next((t for t in tops if t.id == cand.parent_id), None)
                if moved_top is not None:
                    top = moved_top
                sub = cand
                cross_top = top.name
                break

    sub_via_alias = False
    if sub is None and legacy_sub:
        sub = _pick_by_name(children, legacy_sub)
        sub_via_alias = sub is not None

    note = ""
    if sub is None:
        if not children:
            return CategoryHit(note=f"一级类目「{top.name}」下还没有二级类目：请先到「货源 · 类目管理」补上")
        sub = children[0]
        note = f"类目只填到「{top.name}」，已按该一级下的第一个二级类目「{sub.name}」上架"

    # —— 三级：填了按名认领，没填就按标题 / 卖点联想（可能连带把二级换到更贴切的那个）——
    hint_text = " ".join(
        str(x or "") for x in (product.name, product.selling_points)
    )
    leaf, top, sub, leaf_note = _resolve_leaf(
        rows, by_parent, tops, top, sub, parts, hint_text, blocked=product.brand or ""
    )

    category_id = _sub_id_of(sub.source_id)
    if not category_id:
        # 二级类目是手工新增的（没有渔货栈 source_id）→ 看它下面的三级能不能反推
        grands = sorted(by_parent.get(sub.id, []), key=lambda r: (r.sort_order, r.id))
        for g in grands:
            category_id = _sub_id_of(g.source_id)
            if category_id:
                break
    if not category_id:
        return CategoryHit(
            top_name=top.name,
            sub_name=sub.name,
            note=f"二级类目「{sub.name}」在渔货栈里没有对应类目：请到「货源 · 类目管理」同步一次类目",
        )
    notes: list[str] = [note] if note else []
    if cross_top:
        notes.append(
            f"二级类目「{want}」不在「{cross_from}」下，已按「{cross_top} / {want}」上架"
            f"（类目口径换了），建议核对商品档案里的类目"
        )
    if top_via_alias or sub_via_alias:
        notes.append(
            f"类目「{text}」是老写法，已按「{top.name} / {sub.name}」近似上架，建议核对商品档案里的类目"
        )
    if leaf_note:
        notes.append(leaf_note)
    return CategoryHit(
        category_id=category_id,
        top_name=top.name,
        sub_name=sub.name,
        leaf_name=leaf.name if leaf is not None else "",
        note="；".join(n for n in notes if n),
    )


def _longest_alias(text: str) -> str:
    """老类目名兜底：文本里命中的最长别名键（越具体越优先）。

    只认两个汉字以上的键 —— 单字（如「鞋」）放进去会到处误命中（鞋架、鞋柜…）。
    """
    best = ""
    for key in _LEGACY_CATEGORY_MAP:
        if len(key) >= 2 and key in text and len(key) > len(best):
            best = key
    return best


def published_item(db, product: Product):
    """这笔渔枢系统商品在渔货栈商品池里的那条记录（没上架过就是 None）。"""
    from src.models import SupplierItem  # noqa: PLC0415  渔货栈模型（同一份 metadata）

    item_id = item_id_for(product)
    item = db.scalars(select(SupplierItem).where(SupplierItem.item_id == item_id)).first()
    if item is not None:
        return item
    # 兼容：早期可能按商家编码认领过（两边编码同规则）
    code = (product.code or "").strip()
    if code:
        return db.scalars(select(SupplierItem).where(SupplierItem.item_code == code)).first()
    return None


def status_of(db, product: Product) -> dict:
    """上架状态：页面用它决定显示「上架到渔货栈」还是「更新资料 / 下架」。"""
    item = published_item(db, product)
    hit = resolve_category(db, product)
    return {
        "listed": bool(item is not None and item.is_listed),
        "exists": item is not None,
        "item_id": getattr(item, "item_id", "") or item_id_for(product),
        "category_id": getattr(item, "category_id", "") or "",
        "listed_at": getattr(item, "listed_at", None),
        "updated_at": getattr(item, "last_synced_at", None),
        # 平台下架（渔货栈会看到下架提示 + 下架前的资料快照）
        "unlisted_at": getattr(item, "unlisted_at", None),
        "unlisted_reason": getattr(item, "unlisted_reason", "") or "",
        "category": hit,
        "category_ok": hit.ok,
        # 资料是否过期（2026-10-03）：渔枢系统改完资料还没回传，渔货栈那边的资料就是旧的
        "stale": _is_stale(product, item),
    }


def _is_stale(product: Product, item) -> bool:
    """渔枢系统商品档案改过之后没回传 → 渔货栈那份资料已过期。

    判据是「商品档案的更新时间」晚于「上次同步时间」：改标题 / 改价 / 改库存都会推高
    updated_at，回传一次（publish）就把 last_synced_at 追平。差 1 秒以上才算，避免
    同一秒内保存 + 回传被判成过期。
    """
    if item is None or getattr(item, "last_synced_at", None) is None:
        return False
    changed_at = getattr(product, "updated_at", None)
    if changed_at is None:
        return False
    try:
        return (changed_at - item.last_synced_at).total_seconds() > 1
    except TypeError:
        return False


def snapshot_of(item, category_text: str = "") -> dict:
    """下架前的资料快照：渔货栈看到的提示就是「上架前预览」那一份。

    存的是当时渔货栈里的展示口径（标题 / 主图 / 渔货价 / 建议售价 / 类目 / 编码 / 品牌），
    下架后商品档案可能再被改动，快照保证渔货栈看到的是"下架那一刻"的样子。
    """
    return {
        "category": category_text or "",
        "title": (getattr(item, "title", "") or "").strip(),
        "main_image": getattr(item, "main_image", "") or "",
        "fish_price": float(getattr(item, "fish_price", 0) or 0),
        "sale_price": float(getattr(item, "sale_price", 0) or 0),
        "market_price": float(getattr(item, "market_price", 0) or 0),
        "category_id": getattr(item, "category_id", "") or "",
        "item_code": getattr(item, "item_code", "") or "",
        "brand": getattr(item, "brand", "") or "",
        "stock": int(getattr(item, "stock", 0) or 0),
        "listed_at": getattr(item, "listed_at", None).isoformat() if getattr(item, "listed_at", None) else "",
    }


def _json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _int_or_none(value) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def _fish_price_of(product: Product, sku=None) -> float:
    """这一款的渔货价（渔货栈的成本价）。

    优先级：SKU 行渔货价 → SKU 行成本价 → 商品渔货价 → 商品成本价；都没有返回 0
    （渔货栈页面对 0 显示「待定」，不会显示假的价格）。
    注意**不能取建议售价**：渔货栈 sku_list 里的 price 是进货价（采购车 / 结算页按它算钱），
    写成售价会让采购车把售价当进价，账全错。
    """
    if sku is not None:
        if float(getattr(sku, "fish_price", 0) or 0) > 0:
            return float(sku.fish_price)
        if float(getattr(sku, "cost_amount", 0) or 0) > 0:
            return float(sku.cost_amount)
    if float(product.fish_price or 0) > 0:
        return float(product.fish_price)
    return float(product.cost_amount or 0)


def _sku_rows(db, product: Product, item_id: str, default_stock: int) -> list[dict]:
    """把渔枢系统商品档案的规格（product_skus）搬成渔货栈的 sku_list。

    以前只造一条「默认」SKU：多规格商品上架后规格全丢，渔货栈只能按单规格卖。
    现在按渔枢系统的规格行逐条搬（编码 / 规格名 / 渔货价 / 库存），渔枢系统没建规格才兜一条默认。
    """
    from ..models import ProductSku  # noqa: PLC0415

    rows = []
    try:
        rows = list(
            db.scalars(
                select(ProductSku)
                .where(ProductSku.product_id == product.id, ProductSku.enabled.is_(True))
                .order_by(ProductSku.id)
            ).all()
        )
    except Exception as exc:  # noqa: BLE001  规格表读不到就按单规格兜底
        logger.warning("读取商品规格失败（product=%s）：%s", product.id, exc)

    from .product import sku_code_of  # noqa: PLC0415  SKU 编码由商家编码派生

    skus: list[dict] = []
    for idx, row in enumerate(rows):
        stock = int(row.stock or 0)
        if stock < 0:  # -1 = 不单独管库存，跟着商品总库存走
            stock = default_stock
        # SKU 编码口径：商家编码（XYSK…）+ 两位序号，与渔枢系统页面一致
        code = (row.sku_code or "").strip() or sku_code_of(product, idx + 1)
        skus.append(
            {
                "sku_id": code,
                "spec_text": (row.spec or "").strip() or "默认",
                # 渔货价口径（不是建议售价）：采购车 / 采购单按这个价算钱
                # 价格跟着 SKU 走：SKU 行渔货价优先，其次 SKU 成本、商品渔货价
                "price": round(_fish_price_of(product, row), 2),
                # 建议售价 / 市场价（划线价）：留空带 0，渔货栈页面不显示
                "sale_price": round(float(getattr(row, "price", 0) or 0) or float(product.price or 0), 2),
                "market_price": round(
                    float(getattr(row, "market_price", 0) or 0) or float(product.original_price or 0), 2
                ),
                "stock": max(stock, 0),
                "code": code,
            }
        )
    if not skus:
        skus = [
            {
                "sku_id": f"{item_id}-default",
                "spec_text": "默认",
                "price": round(_fish_price_of(product), 2),
                "sale_price": round(float(product.price or 0), 2),
                "market_price": round(float(product.original_price or 0), 2),
                "stock": max(default_stock, 0),
                "code": (product.code or "").strip(),
            }
        ]
    return skus


def publish(db, product: Product, actor: str = "") -> tuple[bool, str]:
    """上架到渔货栈：写 / 更新渔货栈商品池，并落到对应类目。

    返回 `(ok, 提示语)`。类目对不上会拒绝（提示语说明怎么改），不落库。
    """
    from src.models import SupplierItem  # noqa: PLC0415

    hit = resolve_category(db, product)
    if not hit.ok:
        return False, hit.note

    item = published_item(db, product)
    is_new = item is None
    if item is None:
        item = SupplierItem(item_id=item_id_for(product))

    # —— 与渔枢系统商品档案同一套内容（商品内容在渔枢系统端维护，这里同步过去）——
    item.title = (product.name or "").strip()
    item.category_id = hit.category_id
    item.main_image = product.image_path or None
    images = [img for img in (product.image_list or []) if img]
    item.images = images or None
    item.brand = (product.brand or "").strip() or None
    # 型号 / 规格（2026-10-15）：渔集建档带过来的商品档案字段，一并同步给渔货栈详情页
    item.model = (product.model or "").strip() or None
    item.spec = (product.spec or "").strip() or None
    item.selling_points = (product.selling_points or "").strip() or None
    item.detail_html = (product.detail_html or "").strip() or None
    item.supplier_tip = (product.supplier_tip or "").strip() or None
    # 商品标签（固定标签）：渔枢系统发布时勾选的那几个（源头工厂 / 现货速发 …），
    # 原样同步到渔货栈；渔货栈的动态标签（直降 x 元 / 高毛利 x%）由那边按数据算，不走这里。
    item.tags = (product.tags or "").strip() or None
    item.spec_defs = _json_list(product.spec_defs)
    item.freight_template = (product.freight_template or "").strip() or None
    item.shipping_mode = (product.shipping_mode or "").strip() or None
    # 发货时效 / 售后：渔货栈这两列不能为空，渔枢系统没填就用默认值（与货源广场口径一致）
    item.delivery_days = _int_or_none(product.delivery_days) or 2
    item.after_sale = (product.after_sale or "").strip() or "7天无理由退换"
    # —— 价格口径（与渔货栈一致）——
    # cost_price = 采购成本（内部，不对外展示）；fish_price = 渔货价（渔货栈看到的成本价）；
    # sale_price = 建议零售价（渔枢系统端售价）；market_price = 划线价
    item.cost_price = Decimal(str(product.cost_amount or 0))
    item.fish_price = Decimal(str(product.fish_price or 0))
    item.sale_price = Decimal(str(product.price or 0))
    item.market_price = Decimal(str(product.original_price or 0))
    item.shipping_fee = Decimal(str(product.shipping_fee or 0))
    item.item_code = (product.code or "").strip() or None
    # —— 库存与规格 ——
    # 规格按渔枢系统商品档案的 product_skus 逐条搬（多规格不再被压成一条「默认」），
    # 价格一律是渔货价（渔货栈进货价），不是建议售价。
    skus = _sku_rows(db, product, item.item_id, max(int(product.stock or 0), 0))
    stock = max(int(product.stock or 0), 0) or sum(int(s.get("stock") or 0) for s in skus)
    item.stock = stock
    item.prev_stock = stock
    item.return_rate = item.return_rate or 0
    item.sku_list = {"skus": skus}
    # —— 上架与来源 ——
    item.source_type = (
        product.source_type if product.source_type in ("self_operated", "third_party") else "self_operated"
    )
    item.audit_status = "approved"
    # 三级类目（认到才写）：货源广场按三级筛货时先认这一列，HUB 上架的货
    # 才不会因为「标题里没写三级名」被筛掉
    item.category_l3 = hit.leaf_name or None
    item.audit_note = f"由渔枢系统商品 #{product.id} 上架到渔货栈"
    item.audited_at = now()
    item.is_listed = True
    item.listed_at = now()
    item.last_synced_at = now()
    item.last_stock_change_at = now()
    # 重新上架：清掉平台下架标记与快照（渔货栈不再显示下架提示）
    item.unlisted_at = None
    item.unlisted_reason = None
    item.unlisted_by = None
    item.unlisted_snapshot = {}
    item.raw_json = {
        "origin": "hub_product",
        "hub_product_id": product.id,
        "hub_name": product.name,
        "hub_category": product.category,
        "published_by": actor or "",
    }
    db.add(item)

    # 运费模板 / 发货方式：渔枢系统没填就按渔货栈的运费规则套用（类目 > 全局默认）
    try:
        from src.goods.service import apply_shipping_defaults  # noqa: PLC0415

        apply_shipping_defaults(db, item)
    except Exception as exc:  # noqa: BLE001  套不上不影响上架
        logger.warning("渔货栈商品自动套用运费规则失败（%s）：%s", item.item_id, exc)

    # 渔枢系统侧同步状态：在售 + 上架时间
    product.status = "on_sale"
    product.listed_at = now()

    logger.info(
        "上架到渔货栈 product=%s item=%s category=%s new=%s by=%s",
        product.id, item.item_id, hit.category_id, is_new, actor or "-",
    )
    tip = f"{'已上架' if is_new else '已更新'}：{hit.text}（渔货栈商品 {item.item_id}）"
    if hit.note:
        tip = f"{tip}；{hit.note}"
    if float(product.fish_price or 0) <= 0:
        # 渔货价是渔货栈看到的成本价：没填的话渔货栈看到 0 元，得提醒回去补
        tip = f"{tip}；注意：渔货价还没填，渔货栈的价格会显示「待定」（不会显示 0 元）"
    return True, tip


def unpublish(db, product: Product, actor: str = "", reason: str = "") -> tuple[bool, str]:
    """从渔货栈下架：渔货栈商品池那条记录置为下架（保留资料，订单与历史不丢）。

    下架会把**下架前的资料快照**（标题 / 主图 / 渔货价 / 建议售价 / 类目 / 编码）
    一起写进商品池 —— 渔货栈看到的提示就是「上架前预览」那一份，
    知道自己下掉的是哪款货（下架后档案再改也不会把这份提示改花）。
    """
    from src.models import SupplierItem  # noqa: PLC0415

    item = published_item(db, product)
    if item is None:
        return False, "这笔商品还没上架到渔货栈"
    hit = resolve_category(db, product)
    item.is_listed = False
    item.unlisted_at = now()
    item.unlisted_reason = (reason or "").strip() or "渔枢系统下架（商品档案已下架）"
    item.unlisted_by = (actor or "").strip() or None
    item.unlisted_snapshot = snapshot_of(item, category_text=hit.text)
    product.status = "off_sale"
    logger.info("从渔货栈下架 product=%s item=%s by=%s", product.id, item.item_id, actor or "-")
    return True, (
        f"已从渔货栈下架：{item.title or item.item_id}"
        f"（渔货栈会看到下架提示 + 下架前的资料快照）"
    )


def publish_many(db, products: list[Product], actor: str = "") -> tuple[int, list[str]]:
    """批量上架。返回 `(成功数, 失败说明列表)`。"""
    ok_count = 0
    failures: list[str] = []
    for product in products:
        ok, text = publish(db, product, actor=actor)
        if ok:
            ok_count += 1
        else:
            failures.append(f"{product.name or product.id}：{text}")
    return ok_count, failures


def overview(db, products: list[Product]) -> list[dict]:
    """渔货栈发布管理页的数据：每款商品 + 它在渔货栈的落库状态与类目。

    批量预取（渔货栈商品 + 类目表各查一次），不按商品逐条查，列表页才不慢。
    """
    from src.models import SupplierItem  # noqa: PLC0415

    if not products:
        return []
    ids = [item_id_for(p) for p in products]
    codes = [(p.code or "").strip() for p in products if (p.code or "").strip()]
    by_item_id = {
        i.item_id: i
        for i in db.scalars(select(SupplierItem).where(SupplierItem.item_id.in_(ids))).all()
    }
    by_code = {}
    if codes:
        by_code = {
            (i.item_code or "").strip(): i
            for i in db.scalars(select(SupplierItem).where(SupplierItem.item_code.in_(codes))).all()
            if (i.item_code or "").strip()
        }
    cat_rows = load_categories(db)

    rows: list[dict] = []
    for product in products:
        item = by_item_id.get(item_id_for(product)) or by_code.get((product.code or "").strip())
        hit = resolve_category(db, product, rows=cat_rows)
        rows.append(
            {
                "product": product,
                "item": item,
                "listed": bool(item is not None and item.is_listed),
                "category": hit,
                "item_id": getattr(item, "item_id", "") or item_id_for(product),
                "listed_at": getattr(item, "listed_at", None),
                "synced_at": getattr(item, "last_synced_at", None),
            }
        )
    return rows
