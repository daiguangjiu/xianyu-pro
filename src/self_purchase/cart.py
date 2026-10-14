"""采购车（详情页「加采购车」）—— 1688 采购单的自营版。

一行 = 账号 + 货源 + 规格（`uq_cart_line` 唯一）；重复加购只累加数量；
改数量 / 删行 / 勾选结算 / 清空都在这里，页面只管调。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models import PurchaseCartItem, SupplierItem
from src.self_purchase.lines import TWO, freight_of, line_of

MAX_QTY = 999


def _default_receiver(
    db: Session, owner: str, name: str = "", phone: str = "", address: str = ""
) -> dict[str, str | None]:
    """新加进车的一行，收货信息先用账号地址簿里的默认地址填上（没有就留空）。"""
    if str(name or "").strip() or str(address or "").strip():
        return {
            "receiver_name": (name or "").strip() or None,
            "receiver_phone": (phone or "").strip() or None,
            "receiver_address": (address or "").strip() or None,
        }
    try:
        from src.self_purchase.address import default_address

        addr = default_address(db, owner)
    except Exception:  # noqa: BLE001  地址簿表还没建出来：留空，结算页手工填
        return {"receiver_name": None, "receiver_phone": None, "receiver_address": None}
    if addr is None:
        return {"receiver_name": None, "receiver_phone": None, "receiver_address": None}
    return {
        "receiver_name": addr.receiver_name or None,
        "receiver_phone": addr.phone or None,
        "receiver_address": f"{addr.region or ''} {addr.address or ''}".strip() or None,
    }


def add_to_cart(
    db: Session,
    owner: str,
    item,
    *,
    sku_id: str = "",
    qty: int = 1,
    receiver_name: str = "",
    receiver_phone: str = "",
    receiver_address: str = "",
    commit: bool = True,
) -> PurchaseCartItem:
    """加采购车：同一款货 + 同一规格只有一行，重复加购累加数量。

    加购那一刻的价格 / 库存会刷新成最新（不然放几天再来结算，价和货都对不上）。
    没可售库存直接拒绝（抛 ValueError，路由翻译成 400 + 提示）。

    收货地址（2026-10-12）：**每一行各填各的** —— 一次给好几个人各带一件时，
    结算页按地址拆单。没传就用账号地址簿里的默认地址先填上（后面还能改）。
    """
    line = line_of(db, item, sku_id=sku_id, qty=qty)
    if line.stock <= 0:
        raise ValueError("这款货当前没有可售库存，先看看同类货源")
    row = (
        db.execute(
            select(PurchaseCartItem).where(
                PurchaseCartItem.owner == owner,
                PurchaseCartItem.item_id == line.item_id,
                PurchaseCartItem.sku_id == line.sku_id,
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = PurchaseCartItem(
            owner=owner,
            item_id=line.item_id,
            sku_id=line.sku_id,
            qty=line.qty,
            channel="xianyu",
            # 新行先把默认收货地址带上（没传也没有默认地址就留空，结算页再填）
            **_default_receiver(db, owner, receiver_name, receiver_phone, receiver_address),
        )
        db.add(row)
    else:
        row.qty = int(row.qty or 0) + line.qty
        # 明确传了地址就覆盖（采购车里「改收货地址」走这条）
        if str(receiver_name or "").strip() or str(receiver_address or "").strip():
            row.receiver_name = (receiver_name or "").strip() or None
            row.receiver_phone = (receiver_phone or "").strip() or None
            row.receiver_address = (receiver_address or "").strip() or None

    row.spec_text = line.spec_text
    row.title = line.title or row.title
    row.image = line.image or row.image
    row.supplier_id = line.supplier_id
    row.supplier_name = line.supplier_name
    row.price = line.price
    row.stock = line.stock
    row.qty = max(1, min(int(row.qty or 1), min(MAX_QTY, line.stock)))

    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def to_buy_line(row: PurchaseCartItem):
    """购物车行 → 结算明细行（结算页与下单共用，价格取车里那份）。"""
    from src.self_purchase.lines import BuyLine

    return BuyLine(
        item_id=row.item_id,
        sku_id=row.sku_id or "",
        spec_text=row.spec_text or "默认规格",
        title=row.title or "",
        image=row.image,
        price=Decimal(str(row.price or 0)),
        qty=int(row.qty or 1),
        stock=int(row.stock or 0),
        supplier_id=row.supplier_id or "",
        supplier_name=row.supplier_name or "渔货栈 · 平台货源",
        line_id=row.id,
    )


def list_cart(db: Session, owner: str) -> list[PurchaseCartItem]:
    """按加入顺序列（后加的排前面，跟购物车一个习惯）。"""
    return list(
        db.execute(
            select(PurchaseCartItem)
            .where(PurchaseCartItem.owner == owner)
            .order_by(PurchaseCartItem.id.desc())
        )
        .scalars()
        .all()
    )


def get_line(db: Session, owner: str, line_id: int) -> PurchaseCartItem | None:
    return (
        db.execute(
            select(PurchaseCartItem).where(
                PurchaseCartItem.id == int(line_id), PurchaseCartItem.owner == owner
            )
        )
        .scalars()
        .first()
    )


def update_qty(db: Session, owner: str, line_id: int, qty: int, *, commit: bool = True):
    """改数量：夹到 [1, 可售]，库存不够就按库存封顶（页面会看到实际值）。"""
    row = get_line(db, owner, line_id)
    if row is None:
        return None
    limit = max(1, min(MAX_QTY, int(row.stock or 1)))
    row.qty = max(1, min(int(qty or 1), limit))
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def remove_lines(db: Session, owner: str, line_ids: list[int], *, commit: bool = True) -> int:
    """删行（支持批量：结算页/购物车勾选后删除）。"""
    ids = [int(i) for i in line_ids if str(i).strip()]
    if not ids:
        return 0
    rows = (
        db.execute(
            select(PurchaseCartItem).where(
                PurchaseCartItem.owner == owner, PurchaseCartItem.id.in_(ids)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        db.delete(row)
    if commit:
        db.commit()
    else:
        db.flush()
    return len(rows)


def clear_cart(db: Session, owner: str, *, commit: bool = True) -> int:
    """清空采购车（下单成功后不必清——成功下单的行走的是另一张表；这里是「清空」按钮）。"""
    rows = list_cart(db, owner)
    for row in rows:
        db.delete(row)
    if commit:
        db.commit()
    else:
        db.flush()
    return len(rows)


def cart_count(db: Session, owner: str) -> int:
    """购物车角标：行数（不是件数，跟 1688 一致）。"""
    value = db.scalar(
        select(func.count(PurchaseCartItem.id)).where(PurchaseCartItem.owner == owner)
    )
    return int(value or 0)


def set_line_address(
    db: Session,
    owner: str,
    line_id: int,
    *,
    receiver_name: str = "",
    receiver_phone: str = "",
    receiver_address: str = "",
    commit: bool = True,
) -> PurchaseCartItem | None:
    """给采购车里**这一行**单独设收货地址（2026-10-12：可以给不同的人各带一件）。"""
    row = get_line(db, owner, line_id)
    if row is None:
        return None
    row.receiver_name = (receiver_name or "").strip() or None
    row.receiver_phone = (receiver_phone or "").strip() or None
    row.receiver_address = (receiver_address or "").strip() or None
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def _addr_key(row: PurchaseCartItem) -> str:
    """拆单用的地址指纹：同一个人同一地址算一组（去掉空格与标点差异）。"""
    text = f"{row.receiver_name or ''}|{row.receiver_phone or ''}|{row.receiver_address or ''}"
    return "".join(ch for ch in text if not ch.isspace())


def group_lines(db: Session, rows: list[PurchaseCartItem]) -> list[dict]:
    """按「收货地址 + 供应商」把车里的行拆成几组 —— 一组就是一张子单。

    2026-10-12 用户拍板：采购车可以一次给好几个人各带一件，结算时按地址拆开，
    钱一次付、货各发各的（拆单履约）。地址完全相同的行仍归到同一张子单。
    """
    groups: list[dict] = []
    index: dict[tuple[str, str], dict] = {}
    # 按加入顺序（id 升序）分组：页面上的「第 1 张子单」永远是车里先加的那件
    for row in sorted(rows, key=lambda r: int(r.id or 0)):
        key = (_addr_key(row), str(row.supplier_id or ""))
        group = index.get(key)
        if group is None:
            group = {
                "key": f"g{len(groups) + 1}",
                "receiver": {
                    "name": row.receiver_name or "",
                    "phone": row.receiver_phone or "",
                    "address": row.receiver_address or "",
                },
                "supplier_id": row.supplier_id or "",
                "supplier_name": row.supplier_name or "渔货栈 · 平台货源",
                "lines": [],
            }
            index[key] = group
            groups.append(group)
        group["lines"].append(row)

    for group in groups:
        goods = Decimal("0.00")
        total_qty = 0
        for row in group["lines"]:
            goods += Decimal(str(row.price or 0)) * int(row.qty or 0)
            total_qty += int(row.qty or 0)
        goods = goods.quantize(TWO)

        freight = Decimal("0.00")
        seen: set[str] = set()
        for row in group["lines"]:
            if row.item_id in seen:
                continue
            seen.add(row.item_id)
            item = (
                db.execute(select(SupplierItem).where(SupplierItem.item_id == row.item_id))
                .scalars()
                .first()
            )
            if item is not None:
                freight += freight_of(item)
        freight = freight.quantize(TWO)

        group["goods_amount"] = goods
        group["freight"] = freight
        group["discount"] = Decimal("0.00")
        group["pay_amount"] = (goods + freight).quantize(TWO)
        group["kinds"] = len(group["lines"])
        group["total_qty"] = total_qty
        group["line_ids"] = ",".join(str(row.id) for row in group["lines"])
    return groups


def cart_summary(db: Session, rows: list[PurchaseCartItem]) -> dict:
    """结算口径：商品总计 / 总运费 / 优惠 / 应付。

    运费按「不同货源各算一次」——同一款货买 3 件也只收一份运费（与 1688 一致）。
    """
    goods_amount = Decimal("0.00")
    total_qty = 0
    for row in rows:
        goods_amount += Decimal(str(row.price or 0)) * int(row.qty or 0)
        total_qty += int(row.qty or 0)
    goods_amount = goods_amount.quantize(TWO)

    freight = Decimal("0.00")
    seen: set[str] = set()
    for row in rows:
        if row.item_id in seen:
            continue
        seen.add(row.item_id)
        item = (
            db.execute(select(SupplierItem).where(SupplierItem.item_id == row.item_id))
            .scalars()
            .first()
        )
        if item is not None:
            freight += freight_of(item)
    freight = freight.quantize(TWO)

    discount = Decimal("0.00")
    return {
        "kinds": len(rows),
        "total_qty": total_qty,
        "goods_amount": goods_amount,
        "freight": freight,
        "discount": discount,
        "pay_amount": (goods_amount + freight - discount).quantize(TWO),
    }
