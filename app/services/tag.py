"""商品标签字典：发布商品时可勾选的固定标签（「源头工厂」「现货速发」…）。

分工（2026-10-04 口径）：
  · **固定标签**（本模块）= 人工认定的事实标签，运营在「商品 · 商品标签」页维护、
    发布 / 编辑商品时勾选，存进 products.tags（一行一个），上架到渔货栈时一并同步；
  · **动态标签** = 渔货栈按商品数据当场算出来的（今日上新 / 直降 x 元 / 高毛利 x% /
    库存预警 / 已开单），不入库、也管不着 —— 见 src/web/router.py 的货源卡片构造。

标签名是商品上的文本，改名时会把已有商品上的旧名一起改掉（否则商品上的标签就成了
「字典里没有的野标签」，页面还能显示，但再编辑时选不回来）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Product, ProductTag

# 展示配色：与 app/static/style.css 的 .ftag 一致
KIND_CHOICES: tuple[str, ...] = ("brand", "success", "info", "danger")
KIND_TEXT: dict[str, str] = {
    "brand": "金色 · 品牌资质",
    "success": "绿色 · 服务承诺",
    "info": "蓝色 · 常规",
    "danger": "红色 · 促销",
}

# 种子标签：首次启动播种（seeded=True；误删了下一次启动会补回来）
TAG_SEEDS: tuple[dict, ...] = (
    {"name": "源头工厂", "kind": "brand", "sort_order": 10,
     "hint": "厂家直供、有工厂资质 —— 对分销商最有说服力的一条"},
    {"name": "现货速发", "kind": "success", "sort_order": 20,
     "hint": "仓库有货，下单当天 / 次日发出"},
    {"name": "工厂直供", "kind": "brand", "sort_order": 30,
     "hint": "省掉中间环节，价格与账期更有优势"},
    {"name": "支持定制", "kind": "info", "sort_order": 40,
     "hint": "可改包装 / 加 logo / 定做规格"},
    {"name": "七天无理由", "kind": "success", "sort_order": 50,
     "hint": "售后承诺，闲鱼发布包里同步展示"},
    {"name": "出口品质", "kind": "info", "sort_order": 60,
     "hint": "外贸尾单 / 出口标准"},
)


# ------------------------------------------------------------------ 工具
def normalize_kind(value: str | None) -> str:
    """配色归一：非法值回落 info。"""
    kind = (value or "").strip().lower()
    return kind if kind in KIND_CHOICES else "info"


def parse_tag_text(text: str | None) -> list[str]:
    """把商品上存的标签文本（一行一个）解析成列表：去空白、去重、保序。"""
    out: list[str] = []
    for line in (text or "").replace("，", "\n").replace(",", "\n").splitlines():
        name = line.strip()
        if name and name not in out:
            out.append(name)
    return out


def join_tags(names) -> str:
    """标签列表 → 入库文本（一行一个）。"""
    return "\n".join(parse_tag_text("\n".join(str(x or "") for x in (names or []))))


# ------------------------------------------------------------------ 读
def list_tags(session: Session, *, only_enabled: bool = False) -> list[ProductTag]:
    """标签字典（按 sort_order、id 排）；only_enabled=True 只给启用中的。"""
    stmt = select(ProductTag)
    if only_enabled:
        stmt = stmt.where(ProductTag.enabled.is_(True))
    return list(
        session.scalars(stmt.order_by(ProductTag.sort_order.asc(), ProductTag.id.asc()))
    )


def tag_names(session: Session) -> list[str]:
    """发布 / 编辑页能勾选的标签名（启用中的）。"""
    return [t.name for t in list_tags(session, only_enabled=True)]


def kind_map(session: Session) -> dict[str, str]:
    """标签名 → 配色，供商品详情页渲染（字典里没有的野标签回落 info）。"""
    return {t.name: normalize_kind(t.kind) for t in list_tags(session)}


def usage_counts(session: Session) -> dict[str, int]:
    """每个标签被多少商品用了（管理页展示，便于判断能不能删）。"""
    counts: dict[str, int] = {}
    for (text,) in session.execute(select(Product.tags).where(Product.tags != "")).all():
        for name in parse_tag_text(text):
            counts[name] = counts.get(name, 0) + 1
    return counts


def get_tag(session: Session, tag_id: int) -> ProductTag | None:
    return session.get(ProductTag, tag_id)


# ------------------------------------------------------------------ 写
def ensure_seeds(session: Session) -> int:
    """播种种子标签：按名字补（缺哪个补哪个，已停用的不重开）。幂等，启动时调用。"""
    existing = {t.name for t in list_tags(session)}
    created = 0
    for seed in TAG_SEEDS:
        if seed["name"] in existing:
            continue
        session.add(
            ProductTag(
                name=seed["name"],
                kind=normalize_kind(seed.get("kind")),
                hint=str(seed.get("hint") or ""),
                sort_order=int(seed.get("sort_order") or 100),
                enabled=True,
                seeded=True,
            )
        )
        created += 1
    if created:
        session.commit()
    return created


def _name_taken(session: Session, name: str, exclude_id: int | None = None) -> bool:
    row = session.scalar(select(ProductTag).where(ProductTag.name == name))
    return bool(row and row.id != exclude_id)


def create_tag(
    session: Session,
    *,
    name: str,
    kind: str = "info",
    hint: str = "",
    sort_order: int = 100,
    commit: bool = True,
) -> tuple[ProductTag | None, str]:
    """新增标签；返回 (标签, 错误文案)，错误文案非空表示没建成。"""
    clean = (name or "").strip()
    if not clean:
        return None, "标签名不能为空"
    if len(clean) > 16:
        return None, "标签名太长（最多 16 字，货源卡片上放不下）"
    if _name_taken(session, clean):
        return None, f"标签「{clean}」已经存在"
    tag = ProductTag(
        name=clean,
        kind=normalize_kind(kind),
        hint=(hint or "").strip()[:255],
        sort_order=int(sort_order or 100),
        enabled=True,
        seeded=False,
    )
    session.add(tag)
    session.flush()
    if commit:
        session.commit()
    return tag, ""


def update_tag(
    session: Session,
    tag: ProductTag,
    form: dict,
    *,
    commit: bool = True,
) -> tuple[ProductTag | None, str]:
    """按表单更新标签；改名时同步改掉已有商品上的旧名。"""
    old_name = tag.name
    if "name" in form:
        clean = str(form.get("name") or "").strip()
        if not clean:
            return None, "标签名不能为空"
        if len(clean) > 16:
            return None, "标签名太长（最多 16 字）"
        if _name_taken(session, clean, exclude_id=tag.id):
            return None, f"标签「{clean}」已经存在"
        tag.name = clean
    if "kind" in form:
        tag.kind = normalize_kind(str(form.get("kind") or ""))
    if "hint" in form:
        tag.hint = str(form.get("hint") or "").strip()[:255]
    if "sort_order" in form:
        try:
            tag.sort_order = int(str(form.get("sort_order") or 100).strip() or 100)
        except ValueError:
            tag.sort_order = 100
    if "enabled" in form:
        tag.enabled = str(form.get("enabled")) not in ("0", "false", "False", "")
    if old_name and tag.name != old_name:
        rename_on_products(session, old_name, tag.name)
    if commit:
        session.commit()
    return tag, ""


def rename_on_products(session: Session, old_name: str, new_name: str) -> int:
    """把商品上写的旧标签名换成新名（返回改了几款商品）。"""
    changed = 0
    rows = session.scalars(
        select(Product).where(Product.tags.contains(old_name))
    ).all()
    for product in rows:
        names = parse_tag_text(product.tags)
        if old_name not in names:
            continue
        product.tags = join_tags([new_name if n == old_name else n for n in names])
        changed += 1
    if changed:
        session.flush()
    return changed


def toggle_tag(session: Session, tag: ProductTag, *, commit: bool = True) -> ProductTag:
    tag.enabled = not bool(tag.enabled)
    if commit:
        session.commit()
    return tag


def delete_tag(session: Session, tag: ProductTag, *, commit: bool = True) -> str:
    """删除标签：还在被商品用时拒绝（返回错误文案），避免商品上留下野标签。"""
    used = usage_counts(session).get(tag.name, 0)
    if used:
        return f"还有 {used} 款商品在用「{tag.name}」，先在这些商品上取消勾选"
    session.delete(tag)
    if commit:
        session.commit()
    return ""


__all__ = [
    "KIND_CHOICES",
    "KIND_TEXT",
    "TAG_SEEDS",
    "normalize_kind",
    "parse_tag_text",
    "join_tags",
    "list_tags",
    "tag_names",
    "kind_map",
    "usage_counts",
    "get_tag",
    "ensure_seeds",
    "create_tag",
    "update_tag",
    "rename_on_products",
    "toggle_tag",
    "delete_tag",
]
