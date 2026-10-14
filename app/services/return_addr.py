"""地址管理（2026-09-29 起）：退货地址（买家寄回）/ 发货地址（我们寄出）。

管理与运费模板（`services/shipping.py`）同一套逻辑：一条全局默认 + 按供应商 / 类目细分，
命中优先级 **商品 > 供应商 + 类目 > 类目 > 供应商 > 全局默认**。

比运费模板多的一层：**可以直接关联指定商品**（`ReturnAddressProduct`）——
某个商品走专门的退货仓 / 发货仓时就用它，商品是最具体的维度，
命中时压过供应商 / 类目规则。

类目口径与运费模板一致：**只关联到一级类目**。

两种用途的字段与命中规则完全一样，只用 `kind` 区分（`return` / `ship`），共用一张表 ——
与运费模板用 kind 区分「运费模板 / 发货方式」是同一套做法。
「全局默认」**按 kind 各一条**：设了默认发货地址不会把默认退货地址顶掉。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    ADDR_KIND_RETURN,
    ADDR_KIND_SHIP,
    Product,
    ProductCategory,
    ReturnAddress,
    ReturnAddressProduct,
    Supplier,
)
from .shipping import category_path_of, top_category_options, top_id_of

# 用途常量（与 models.ADDR_KIND_* 同值，这里给服务层一套短名字）
KIND_RETURN = ADDR_KIND_RETURN
KIND_SHIP = ADDR_KIND_SHIP
KIND_LABELS = {KIND_RETURN: "退货地址", KIND_SHIP: "发货地址"}


def normalize_kind(kind: str | None) -> str:
    """只看两种用途，其它值一律当退货地址（老链接不带 kind 时的默认）。"""
    return KIND_SHIP if str(kind or "").strip() == KIND_SHIP else KIND_RETURN


def kind_label(kind: str | None) -> str:
    return KIND_LABELS.get(normalize_kind(kind), KIND_LABELS[KIND_RETURN])


# 系统默认：某个用途一条全局默认都没有时才播种，运营自己建过就不动
DEFAULT_NAMES = {
    KIND_RETURN: "默认退货地址（系统默认）",
    KIND_SHIP: "默认发货地址（系统默认）",
}
DEFAULT_NOTES = {
    KIND_RETURN: "系统默认：所有商品都用这一条。可按供应商 / 一级类目细分，或直接指定商品。",
    KIND_SHIP: "系统默认：所有商品的货都从这里发出。可按供应商 / 一级类目细分，或直接指定商品。",
}
# 旧名保留（启动日志等处引用过）
DEFAULT_NAME = DEFAULT_NAMES[KIND_RETURN]
DEFAULT_NOTE = DEFAULT_NOTES[KIND_RETURN]


def _specificity(addr: ReturnAddress) -> int:
    """关联得越具体越优先（类目 > 供应商）；关联商品的直接按最高级算（见 `pick`）。"""
    return (30 if addr.category_id else 0) + (5 if addr.supplier_id else 0)


# ------------------------------------------------------------------ 读
def list_addresses(
    session: Session, *, kind: str | None = KIND_RETURN, only_enabled: bool = False
) -> list[ReturnAddress]:
    """某个用途的地址列表：默认在前，然后按「关联得越具体越靠前」排。

    kind 传 None = 不过滤（跨用途统计用，例如「关联商品总数」）。
    """
    stmt = select(ReturnAddress)
    if kind is not None:
        stmt = stmt.where(ReturnAddress.kind == normalize_kind(kind))
    if only_enabled:
        stmt = stmt.where(ReturnAddress.enabled.is_(True))
    rows = list(session.scalars(stmt))
    rows.sort(key=lambda r: (not r.is_default, -_specificity(r), r.priority or 100, r.id))
    return rows


def get_address(session: Session, address_id: int) -> ReturnAddress | None:
    return session.get(ReturnAddress, address_id)


def product_ids_of(session: Session, address_id: int) -> set[int]:
    """这条地址关联的商品 id。"""
    return {
        int(row.product_id)
        for row in session.scalars(
            select(ReturnAddressProduct).where(ReturnAddressProduct.address_id == address_id)
        )
    }


def product_ids_map(session: Session) -> dict[int, set[int]]:
    """一次取全部「地址 → 关联商品 id」（列表页用，避免逐条查）。"""
    out: dict[int, set[int]] = {}
    for row in session.scalars(select(ReturnAddressProduct)):
        out.setdefault(int(row.address_id), set()).add(int(row.product_id))
    return out


def pick(
    session: Session,
    *,
    product_id: int | None = None,
    category_text: str | None = None,
    supplier_id: int | None = None,
    kind: str | None = KIND_RETURN,
) -> ReturnAddress | None:
    """按「商品 > 供应商 + 类目 > 类目 > 供应商 > 全局默认」挑一条生效地址。"""
    rows = list_addresses(session, kind=kind, only_enabled=True)
    if not rows:
        return None
    top_id = top_category_id(session, category_text)

    best: tuple[tuple, ReturnAddress] | None = None
    for addr in rows:
        linked = product_ids_of(session, int(addr.id))
        if linked:
            # 关联了具体商品：只对那些商品生效。商品是最具体的维度 ——
            # 命中时不再看类目 / 供应商（「这几个商品固定退到这个仓」就是最终口径）
            if product_id is None or int(product_id) not in linked:
                continue
            key = (-100, addr.priority or 100, int(addr.id))
            if best is None or key < best[0]:
                best = (key, addr)
            continue
        cat_hit = 0
        if addr.category_id:
            cat_hit = 1 if (top_id and int(addr.category_id) == top_id) else 0
            if not cat_hit:
                continue  # 关联的是别的一级类目 → 不适用
        sup_hit = 1 if (addr.supplier_id and supplier_id and int(addr.supplier_id) == int(supplier_id)) else 0
        if addr.supplier_id and not sup_hit:
            continue  # 关联的是别的供应商 → 不适用
        is_global = not addr.category_id and not addr.supplier_id and not linked
        if is_global and not addr.is_default:
            continue  # 什么都没关联时，只有「全局默认」那条算数
        key = (
            -_specificity(addr),
            -cat_hit,
            -sup_hit,
            addr.priority or 100,
            int(addr.id),
        )
        if best is None or key < best[0]:
            best = (key, addr)
    return best[1] if best else None


def default_address(
    session: Session, *, kind: str | None = KIND_RETURN
) -> ReturnAddress | None:
    """某个用途的全局默认那一条（没关联任何维度，且 is_default）。"""
    for addr in list_addresses(session, kind=kind, only_enabled=True):
        if addr.is_default and not addr.supplier_id and not addr.category_id:
            return addr
    return None


def top_category_id(session: Session, category_text: str | None) -> int | None:
    """商品类目文本「一级 / 二级 / 三级」→ 一级类目 id（对不上的返回 None）。

    口径与运费模板一致：只认一级类目。
    """
    name = next((p.strip() for p in (category_text or "").split("/") if p.strip()), "")
    if not name:
        return None
    row = session.scalar(
        select(ProductCategory.id).where(
            ProductCategory.parent_id.is_(None), ProductCategory.name == name
        )
    )
    return int(row) if row else None


def resolve_for_product(
    session: Session,
    *,
    product_id: int | None = None,
    category_text: str | None = None,
    supplier_id: int | None = None,
    kind: str | None = KIND_RETURN,
) -> dict:
    """商品该用的地址（商品发布 / 详情页展示用）。"""
    addr = pick(
        session,
        product_id=product_id,
        category_text=category_text,
        supplier_id=supplier_id,
        kind=kind,
    )
    if addr is None:
        return {"name": "", "text": "", "rule": {}}
    return {"name": addr.name, "text": full_text(addr), "rule": _brief(addr)}


def full_text(addr: ReturnAddress) -> str:
    """地址全文：联系人 + 电话 + 地区 + 详细地址（给买家 / 运营看的一行）。"""
    parts = [addr.contact or "", addr.phone or "", addr.region or "", addr.address or ""]
    return " ".join(p for p in parts if p).strip()


# ------------------------------------------------------------------ 写
def ensure_default(session: Session, *, kind: str | None = KIND_RETURN) -> int:
    """保证某个用途有一条全局默认地址（幂等，可每次启动调）。"""
    use = normalize_kind(kind)
    if any(
        a for a in list_addresses(session, kind=use) if not a.supplier_id and not a.category_id
    ):
        return 0
    create_address(
        session,
        kind=use,
        name=DEFAULT_NAMES[use],
        note=DEFAULT_NOTES[use],
        is_default=True,
        commit=False,
    )
    session.commit()
    return 1


def create_address(
    session: Session,
    *,
    name: str,
    kind: str | None = KIND_RETURN,
    contact: str = "",
    phone: str = "",
    region: str = "",
    address: str = "",
    supplier_id: int | None = None,
    category_id: int | None = None,
    priority: int = 100,
    note: str = "",
    is_default: bool = False,
    product_ids: list[int] | None = None,
    commit: bool = True,
) -> ReturnAddress:
    category_id = top_id_of(session, category_id)   # 口径：只关联到一级类目
    addr = ReturnAddress(
        kind=normalize_kind(kind),
        name=(name or "").strip(),
        contact=(contact or "").strip(),
        phone=(phone or "").strip(),
        region=(region or "").strip(),
        address=(address or "").strip(),
        supplier_id=supplier_id or None,
        category_id=category_id,
        category_text=category_path_of(session, category_id),
        priority=int(priority or 100),
        note=(note or "").strip(),
        is_default=bool(is_default),
    )
    session.add(addr)
    session.flush()
    if addr.is_default:
        _clear_other_defaults(session, addr)
    set_products(session, addr, product_ids or [], commit=False)
    if commit:
        session.commit()
    return addr


def update_address(
    session: Session, addr: ReturnAddress, form: dict, *, commit: bool = True
) -> ReturnAddress:
    """按表单更新地址（字段不在 form 里就不动）。"""
    if "name" in form:
        addr.name = str(form.get("name") or "").strip()
    for attr in ("contact", "phone", "region", "address", "note"):
        if attr in form:
            setattr(addr, attr, str(form.get(attr) or "").strip())
    if "supplier_id" in form:
        addr.supplier_id = _to_int(form.get("supplier_id"))
    if "category_id" in form:
        addr.category_id = top_id_of(session, _to_int(form.get("category_id")))  # 只到一级
        addr.category_text = category_path_of(session, addr.category_id)
    if "priority" in form:
        addr.priority = _to_int(form.get("priority")) or 100
    if "enabled" in form:
        addr.enabled = str(form.get("enabled")) not in ("0", "false", "False")
    if "is_default" in form:
        addr.is_default = str(form.get("is_default")) not in ("0", "false", "False", "")
    if addr.is_default:
        _clear_other_defaults(session, addr)
    if commit:
        session.commit()
    return addr


def set_products(
    session: Session, addr: ReturnAddress, product_ids: list[int], *, commit: bool = True
) -> int:
    """重设这条地址关联的商品（传空列表 = 不指定商品）。"""
    wanted = {int(pid) for pid in product_ids if str(pid).strip().lstrip("-").isdigit()}
    current = {
        row: int(row.product_id)
        for row in session.scalars(
            select(ReturnAddressProduct).where(ReturnAddressProduct.address_id == addr.id)
        )
    }
    for row, pid in current.items():
        if pid not in wanted:
            session.delete(row)
    have = set(current.values())
    missing = wanted - have
    if missing:
        names = {
            int(p.id): (p.name or "")
            for p in session.scalars(select(Product).where(Product.id.in_(missing)))
        }
        for pid in sorted(missing):
            session.add(
                ReturnAddressProduct(
                    address_id=int(addr.id), product_id=pid, product_name=names.get(pid, "")
                )
            )
    if commit:
        session.commit()
    return len(missing)


def delete_address(session: Session, addr: ReturnAddress, *, commit: bool = True) -> None:
    session.query(ReturnAddressProduct).filter(
        ReturnAddressProduct.address_id == addr.id
    ).delete(synchronize_session=False)
    session.delete(addr)
    if commit:
        session.commit()


def _clear_other_defaults(session: Session, addr: ReturnAddress) -> None:
    """保证「同一用途」的全局默认只有一条。

    必须按 kind 隔离：否则设一条默认发货地址会把默认退货地址也顶掉 ——
    这是共用一张表最容易踩的坑。
    """
    for other in session.scalars(
        select(ReturnAddress).where(ReturnAddress.kind == normalize_kind(addr.kind))
    ):
        if other.id != addr.id and other.is_default:
            other.is_default = False


def _to_int(value) -> int | None:
    try:
        text = str(value or "").strip()
        return int(text) if text else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ 展示
def scope_text(addr: ReturnAddress, linked_count: int = 0) -> str:
    """适用范围的人话描述。"""
    parts = []
    if linked_count:
        parts.append(f"指定商品 {linked_count} 款")
    if addr.supplier_id:
        parts.append(f"供应商 #{addr.supplier_id}")
    if addr.category_id:
        parts.append(addr.category_text or f"类目 #{addr.category_id}")
    if not parts:
        return "全局默认" if addr.is_default else "全局（不生效）"
    return " + ".join(parts)


def rows(session: Session, *, kind: str | None = KIND_RETURN) -> list[dict]:
    """给页面的行数据（补上供应商名、范围描述、关联商品数）。"""
    supplier_names = {int(s.id): s.name for s in session.scalars(select(Supplier))}
    linked = product_ids_map(session)
    out = []
    for addr in list_addresses(session, kind=kind):
        ids = linked.get(int(addr.id), set())
        out.append(
            {
                "address": addr,
                "supplier_name": supplier_names.get(int(addr.supplier_id or 0), ""),
                "scope": scope_text(addr, len(ids)),
                "product_ids": sorted(ids),
                "product_count": len(ids),
                "text": full_text(addr),
            }
        )
    return out


def product_options(session: Session, limit: int = 500) -> list[dict]:
    """商品候选（关联指定商品时选）：名称（编码）· 供应商，按名称排。"""
    rows_ = session.scalars(
        select(Product).order_by(Product.name).limit(limit)
    ).all()
    supplier_names = {int(s.id): s.name for s in session.scalars(select(Supplier))}
    return [
        {
            "id": int(p.id),
            "label": f"{p.name or '未命名'}"
            + (f"（{p.code}）" if p.code else "")
            + (f" · {supplier_names[int(p.supplier_id)]}" if p.supplier_id and supplier_names.get(int(p.supplier_id)) else ""),
        }
        for p in rows_
    ]


def count_products(session: Session, *, kind: str | None = None) -> int:
    """关联商品总数（kind 传 None = 两种用途合计）。"""
    if kind is None:
        return int(session.scalar(select(func.count()).select_from(ReturnAddressProduct)) or 0)
    return int(
        session.scalar(
            select(func.count())
            .select_from(ReturnAddressProduct)
            .join(ReturnAddress, ReturnAddress.id == ReturnAddressProduct.address_id)
            .where(ReturnAddress.kind == normalize_kind(kind))
        )
        or 0
    )


# 关联类目只到一级：下拉口径与运费模板共用一份
category_options = top_category_options
