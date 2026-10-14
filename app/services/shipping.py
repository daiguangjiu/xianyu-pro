"""运费模板 / 闲鱼发货方式规则（2026-09-29 起）。

两类规则共用一张表 `shipping_rules`（`kind` 区分），管理逻辑与命中逻辑完全一样：

- `freight` 运费模板：上架商品时默认套用的运费口径（全国包邮 / 固定运费 ¥6 …）；
- `method`  闲鱼发货方式：邮寄·包邮 / 邮寄·不包邮-按距离付费 / 邮寄·不包邮-固定收费 /
  无需邮寄·买家自提。

命中优先级（`pick_rule`）：供应商 + 类目 > 类目 > 供应商 > 全局默认。
只关联供应商的规则对所有类目生效；只关联类目的规则对所有供应商生效；
两者都没关联的规则 = 全局规则，其中可以指定一条 `is_default`（每个 kind 只能一条）。

类目口径（2026-09-29 起）：**只关联到一级类目** —— 二三级太细，维护成本高，
而运费与发货方式通常按品类分，一级够用。下拉只给一级，保存时二 / 三级会自动
收敛到它的一级（`top_id_of`），历史数据由 `ensure_top_level_scope` 迁上来。
退货地址（`services/return_addr.py`）用同一套口径，只是多一层「关联指定商品」。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Product, ProductCategory, ShippingRule, Supplier

KIND_FREIGHT = "freight"
KIND_METHOD = "method"
KINDS = (KIND_FREIGHT, KIND_METHOD)
KIND_LABELS = {KIND_FREIGHT: "运费模板", KIND_METHOD: "闲鱼发货方式"}

# 闲鱼发货方式的四种取值（商品 shipping_mode 存的是这里的 value）
METHOD_CHOICES: list[tuple[str, str]] = [
    ("free", "邮寄：包邮"),
    ("distance", "邮寄：不包邮 - 按距离付费"),
    ("fixed", "邮寄：不包邮 - 固定收费"),
    ("self_pickup", "无需邮寄：买家自提"),
]
METHOD_LABELS = dict(METHOD_CHOICES)
# 老数据兼容：历史商品的 shipping_mode 用过 fee（按件运费）
METHOD_LEGACY = {"fee": "邮寄：不包邮 - 固定收费"}

# 系统默认规则：库里一个 kind 都没有全局规则时才播种，运营自己建过就不动
DEFAULT_FREIGHT_NAME = "全国包邮（系统默认）"
DEFAULT_METHOD_NAME = "邮寄：包邮（系统默认）"
DEFAULT_RULES: tuple[tuple[str, str, float, str, str], ...] = (
    (
        KIND_FREIGHT,
        DEFAULT_FREIGHT_NAME,
        0.0,
        "",
        "系统默认：所有商品运费 0 元（全国包邮）。可按供应商 / 类目细分，或改名改成自己的口径。",
    ),
    (
        KIND_METHOD,
        DEFAULT_METHOD_NAME,
        0.0,
        "free",
        "系统默认：所有商品走「邮寄：包邮」。重货、大件可按供应商 / 类目改成自提或按距离付费。",
    ),
)


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def method_label(value: str) -> str:
    """发货方式取值 → 文案（兼容历史值）。"""
    if not value:
        return ""
    return METHOD_LABELS.get(value) or METHOD_LEGACY.get(value) or value


# ------------------------------------------------------------------ 读
def list_rules(session: Session, kind: str | None = None, *, only_enabled: bool = False) -> list[ShippingRule]:
    """规则列表：默认在前，然后按关联维度（供应商+类目 → 类目 → 供应商 → 全局）排。"""
    stmt = select(ShippingRule)
    if kind:
        stmt = stmt.where(ShippingRule.kind == kind)
    if only_enabled:
        stmt = stmt.where(ShippingRule.enabled.is_(True))
    rows = list(session.scalars(stmt))
    rows.sort(key=lambda r: (not r.is_default, -_specificity(r), r.priority or 100, r.id))
    return rows


def _specificity(rule: ShippingRule) -> int:
    """关联得越具体越优先：类目层级（3/2/1）为主，供应商为次。"""
    level = 0
    if rule.category_id:
        level = 3  # 具体是几级在 pick_rule 里再分（这里只判"有没有关联类目"）
    return level * 10 + (5 if rule.supplier_id else 0)


def get_rule(session: Session, rule_id: int) -> ShippingRule | None:
    return session.get(ShippingRule, rule_id)


def top_category_id(session: Session, category_text: str | None) -> int | None:
    """商品类目文本「一级 / 二级 / 三级」→ 一级类目 id（2026-09-29 口径）。

    模板（运费模板 / 发货方式 / 退货地址）**只关联到一级类目**：
    二三级太细，维护成本高，而运费与退货仓通常按品类分，一级够用。
    名字对不上（老数据里的写法）返回 None，规则就按「不限类目」处理。
    """
    name = next((part.strip() for part in (category_text or "").split("/") if part.strip()), "")
    if not name:
        return None
    row = session.scalar(
        select(ProductCategory.id).where(
            ProductCategory.parent_id.is_(None), ProductCategory.name == name
        )
    )
    return int(row) if row else None


def top_id_of(session: Session, category_id: int | None) -> int | None:
    """类目 id → 它所属的一级类目 id（自己就是一级则原样返回）。

    保存规则时用它把二 / 三级类目收敛到一级，历史数据也靠它迁上来。
    """
    if not category_id:
        return None
    node = session.get(ProductCategory, int(category_id))
    hops = 0
    while node is not None and node.parent_id and hops < 5:
        node = session.get(ProductCategory, int(node.parent_id))
        hops += 1
    return int(node.id) if node is not None else None


def pick_rule(
    session: Session,
    kind: str,
    *,
    category_text: str | None = None,
    supplier_id: int | None = None,
) -> ShippingRule | None:
    """按「供应商 + 类目 > 类目 > 供应商 > 全局默认」挑一条生效规则，没有则返回 None。"""
    rules = [r for r in list_rules(session, kind, only_enabled=True)]
    if not rules:
        return None
    top_id = top_category_id(session, category_text)

    best: tuple[tuple, ShippingRule] | None = None
    for rule in rules:
        cat_level = 0
        if rule.category_id:
            # 只认一级类目：规则存的一级 id 与商品所属一级一致才算命中
            cat_level = 3 if (top_id and int(rule.category_id) == top_id) else 0
            if cat_level == 0:
                continue  # 关联的类目和商品对不上 → 不适用
        sup_match = 1 if (rule.supplier_id and supplier_id and int(rule.supplier_id) == int(supplier_id)) else 0
        if rule.supplier_id and not sup_match:
            continue  # 关联的是别的供应商 → 不适用
        is_global = not rule.category_id and not rule.supplier_id
        if is_global and not rule.is_default:
            continue  # 没关联任何维度时只有「全局默认」那条算数
        key = (-cat_level, -sup_match, rule.priority or 100, int(rule.id))
        if best is None or key < best[0]:
            best = (key, rule)
    return best[1] if best else None


def default_rule(session: Session, kind: str) -> ShippingRule | None:
    """全局默认那一张（没有关联任何维度的 is_default 规则）。"""
    for rule in list_rules(session, kind, only_enabled=True):
        if rule.is_default and not rule.supplier_id and not rule.category_id:
            return rule
    return None


def freight_names(session: Session) -> list[str]:
    """运费模板名列表（商品表单的下拉用；全局默认排第一）。"""
    return [r.name for r in list_rules(session, KIND_FREIGHT, only_enabled=True) if r.name]


def resolve_for_product(
    session: Session,
    *,
    category_text: str | None,
    supplier_id: int | None,
) -> dict:
    """商品上架时该用的运费模板与发货方式（页面上用来预选 / 自动套用）。"""
    freight = pick_rule(session, KIND_FREIGHT, category_text=category_text, supplier_id=supplier_id)
    method = pick_rule(session, KIND_METHOD, category_text=category_text, supplier_id=supplier_id)
    return {
        "freight_template": freight.name if freight else "",
        "freight_fee": float(freight.fee or 0) if freight else 0.0,
        "shipping_mode": method.mode if method else "",
        "freight_rule": _rule_brief(freight),
        "method_rule": _rule_brief(method),
    }


def _rule_brief(rule: ShippingRule | None) -> dict:
    if rule is None:
        return {}
    return {
        "id": int(rule.id),
        "name": rule.name,
        "scope": rule_scope(rule),
        "note": rule.note or "",
    }


# ------------------------------------------------------------------ 写
def ensure_defaults(session: Session) -> int:
    """保证每个 kind 都有一条全局默认规则（运费模板 + 发货方式）。

    只在「该 kind 一条全局规则都没有」时补，运营自己建过 / 改过就不覆盖；
    幂等，可以每次启动都调。
    """
    created = 0
    for kind, name, fee, mode, note in DEFAULT_RULES:
        has_global = any(
            r for r in list_rules(session, kind) if not r.supplier_id and not r.category_id
        )
        if has_global:
            continue
        create_rule(
            session, kind=kind, name=name, fee=fee, mode=mode,
            note=note, is_default=True, commit=False,
        )
        created += 1
    if created:
        session.commit()
    return created


def create_rule(
    session: Session,
    *,
    kind: str,
    name: str,
    fee: float = 0.0,
    mode: str = "",
    supplier_id: int | None = None,
    category_id: int | None = None,
    priority: int = 100,
    note: str = "",
    is_default: bool = False,
    commit: bool = True,
) -> ShippingRule:
    category_id = top_id_of(session, category_id)   # 口径：只关联到一级类目
    rule = ShippingRule(
        kind=kind,
        name=(name or "").strip(),
        fee=float(fee or 0),
        mode=(mode or "").strip(),
        supplier_id=supplier_id or None,
        category_id=category_id,
        category_text=category_path_of(session, category_id),
        priority=int(priority or 100),
        note=(note or "").strip(),
        is_default=bool(is_default),
    )
    session.add(rule)
    session.flush()
    if rule.is_default:
        _clear_other_defaults(session, rule)
    if commit:
        session.commit()
    return rule


def update_rule(session: Session, rule: ShippingRule, form: dict, *, commit: bool = True) -> ShippingRule:
    """按表单更新规则；运费模板改名时，同步改掉商品上存的模板名（否则对不上）。"""
    old_name = rule.name
    if "name" in form:
        rule.name = str(form.get("name") or "").strip()
    if "fee" in form:
        try:
            rule.fee = float(str(form.get("fee") or 0).strip() or 0)
        except ValueError:
            rule.fee = 0.0
    if "mode" in form:
        rule.mode = str(form.get("mode") or "").strip()
    if "supplier_id" in form:
        rule.supplier_id = _to_int(form.get("supplier_id"))
    if "category_id" in form:
        rule.category_id = top_id_of(session, _to_int(form.get("category_id")))  # 只到一级
        rule.category_text = category_path_of(session, rule.category_id)
    if "priority" in form:
        rule.priority = _to_int(form.get("priority")) or 100
    if "note" in form:
        rule.note = str(form.get("note") or "").strip()
    if "enabled" in form:
        rule.enabled = str(form.get("enabled")) not in ("0", "false", "False")
    if "is_default" in form:
        rule.is_default = str(form.get("is_default")) not in ("0", "false", "False", "")
    if rule.is_default:
        _clear_other_defaults(session, rule)
    if rule.kind == KIND_FREIGHT and old_name and rule.name and old_name != rule.name:
        session.query(Product).filter(Product.freight_template == old_name).update(
            {Product.freight_template: rule.name}, synchronize_session=False
        )
    if commit:
        session.commit()
    return rule


def delete_rule(session: Session, rule: ShippingRule, *, commit: bool = True) -> None:
    session.delete(rule)
    if commit:
        session.commit()


def _clear_other_defaults(session: Session, rule: ShippingRule) -> None:
    """同一个 kind 只留一条全局默认。"""
    for other in session.scalars(select(ShippingRule).where(ShippingRule.kind == rule.kind)):
        if other.id != rule.id and other.is_default:
            other.is_default = False


def _to_int(value) -> int | None:
    try:
        text = str(value or "").strip()
        return int(text) if text else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ 展示
def rule_scope(rule: ShippingRule) -> str:
    """规则适用范围的人话描述。"""
    parts = []
    if rule.supplier_id:
        parts.append(f"供应商 #{rule.supplier_id}")
    if rule.category_id:
        parts.append(rule.category_text or f"类目 #{rule.category_id}")
    if not parts:
        return "全局默认" if rule.is_default else "全局"
    return " + ".join(parts)


def rule_rows(session: Session, kind: str) -> list[dict]:
    """给页面的规则行（补上供应商名与范围描述）。"""
    supplier_names = {int(s.id): s.name for s in session.scalars(select(Supplier))}
    rows = []
    for rule in list_rules(session, kind):
        rows.append(
            {
                "rule": rule,
                "supplier_name": supplier_names.get(int(rule.supplier_id or 0), ""),
                "scope": rule_scope(rule),
                "mode_label": method_label(rule.mode),
            }
        )
    return rows


def top_category_options(session: Session) -> list[dict]:
    """类目下拉：**只有一级类目**（2026-09-29 口径）。

    运费模板 / 发货方式 / 退货地址都只关联到一级 —— 二三级太细，维护成本高，
    而运费与退货仓通常按品类分，一级够用。顺序按类目管理里的排序
    （`sort_order`），运营调完顺序，这里的下拉跟着变。
    """
    rows = session.scalars(
        select(ProductCategory)
        .where(ProductCategory.parent_id.is_(None))
        .order_by(ProductCategory.sort_order, ProductCategory.id)
    )
    return [{"id": int(node.id), "label": node.name, "level": 1} for node in rows]


def category_options(session: Session) -> list[dict]:
    """兼容旧名：现在与 `top_category_options` 一样只返回一级类目。"""
    return top_category_options(session)


def ensure_top_level_scope(session: Session) -> int:
    """历史规则迁移：把关联到二 / 三级类目的规则收敛到它的一级类目（幂等）。

    老数据里可能存着「耳机音响」这种二级类目 id —— 新口径只认一级，
    不迁的话这些规则会永远命中不上。迁完按一级重新生效。
    """
    fixed = 0
    for rule in session.scalars(
        select(ShippingRule).where(ShippingRule.category_id.is_not(None))
    ):
        top = top_id_of(session, rule.category_id)
        if top and top != int(rule.category_id):
            rule.category_id = top
            rule.category_text = category_path_of(session, top)
            fixed += 1
    if fixed:
        session.commit()
    return fixed


def category_path_of(session: Session, category_id: int | None) -> str:
    if not category_id:
        return ""
    node = session.get(ProductCategory, int(category_id))
    if node is None:
        return ""
    names = [node.name]
    parent = node.parent_id
    while parent:
        up = session.get(ProductCategory, int(parent))
        if up is None:
            break
        names.append(up.name)
        parent = up.parent_id
    return " / ".join(reversed(names))
