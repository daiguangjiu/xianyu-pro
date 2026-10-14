"""采购单：渔货栈向渔货栈平台的采购订单（「立即下单」与采购车结算的产物）。

流程（照 1688 的支付结算）：
  下单 → `pending_pay` 待支付 →（收银台付款）→ `paid` 待发货
       →（平台发货，可回填物流单号）→ `shipped` 已发货
       →（确认收货）→ `completed` 已完成
  未支付 / 已支付都能取消 → `cancelled` 已取消（已支付的取消视为退款，金额原路退回）。

支付是**模拟通道**（没有真实收银台）：余额 / 支付宝 / 微信三选一，付款成功记一条
支付流水号 `pay_no`，够用来跑通闭环与对账。
"""
from __future__ import annotations

import logging
import random
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

# 时间口径（2026-10-03）：库里存 UTC（utc_now），单号 / 展示里的日期按北京（beijing_now）
from app.timeutil import beijing_now, fmt, utc_now
from src.models import StockOrder, StockOrderItem, SupplierItem
from src.self_purchase.lines import TWO, BuyLine, freight_of as freight_of_item

logger = logging.getLogger(__name__)

STATUS_TEXT = {
    "pending_pay": "待支付",
    "paid": "待发货",
    "shipped": "已发货",
    "completed": "已完成",
    "cancelled": "已取消",
}
STATUS_ORDER = ["pending_pay", "paid", "shipped", "completed", "cancelled"]

PAY_METHODS = {
    "balance": "账户余额",
    "alipay": "支付宝",
    "wechat": "微信支付",
}
DEFAULT_PAY_METHOD = "balance"

# 交易方式（2026-10-12 用户拍板）：
#   带教代收 —— 唯一在用的：货款线下转付给带教，带教负责带采；
#   在线支付（微信 / 支付宝）—— 结算页先置灰，后续版本再放出来；
#   escrow（担保交易）—— 结算页已撤掉，留着只为把老订单的「担保交易」显示出来。
TRADE_TYPES = {"agent": "带教代收", "escrow": "担保交易", "online": "在线支付（微信 / 支付宝）"}
DEFAULT_TRADE_TYPE = "agent"
DELIVERY_TYPES = {"express": "快递"}

TERMINAL = {"completed", "cancelled"}


def status_text(status: str | None) -> str:
    return STATUS_TEXT.get(str(status or ""), "未知状态")


def pay_method_text(method: str | None) -> str:
    return PAY_METHODS.get(str(method or ""), "—")


def trade_type_text(trade_type: str | None) -> str:
    """交易方式的中文名（老订单的 escrow 也认得，显示成「担保交易」）。"""
    return TRADE_TYPES.get(str(trade_type or ""), "—")


def next_order_no(db: Session, prefix: str = "CG") -> str:
    """采购单号：CG + 日期 + 4 位流水（当天递增，如 CG20261003-0007）。"""
    today = beijing_now().strftime("%Y%m%d")
    head = f"{prefix}{today}-"
    last = db.scalar(
        select(func.max(StockOrder.order_no)).where(StockOrder.order_no.like(f"{head}%"))
    )
    seq = 1
    if last:
        try:
            seq = int(str(last).split("-")[-1]) + 1
        except (TypeError, ValueError):
            seq = 1
    return f"{head}{seq:04d}"


def create_order(
    db: Session,
    owner: str,
    lines: list[BuyLine],
    *,
    receiver_name: str = "",
    receiver_phone: str = "",
    receiver_address: str = "",
    remark: str = "",
    trade_type: str = DEFAULT_TRADE_TYPE,
    delivery_type: str = "express",
    source: str = "buy_now",
    freight: Decimal | None = None,
    commit: bool = True,
) -> StockOrder:
    """结算下单：一次落单头 + 明细行，金额口径与结算页显示的一致。"""
    if not lines:
        raise ValueError("没有可下单的商品")

    goods_amount = Decimal("0.00")
    total_qty = 0
    for line in lines:
        goods_amount += Decimal(str(line.price)) * int(line.qty)
        total_qty += int(line.qty)
    goods_amount = goods_amount.quantize(TWO)

    if freight is None:
        # 没传就按货源算（同一款货只算一次）
        from src.models import SupplierItem
        from src.self_purchase.lines import freight_of

        freight = Decimal("0.00")
        seen: set[str] = set()
        for line in lines:
            if line.item_id in seen:
                continue
            seen.add(line.item_id)
            item = (
                db.execute(select(SupplierItem).where(SupplierItem.item_id == line.item_id))
                .scalars()
                .first()
            )
            if item is not None:
                freight += freight_of(item)
    freight = Decimal(str(freight)).quantize(TWO)
    discount = Decimal("0.00")

    order = StockOrder(
        # 显式写 UTC：Base 的 created_at 走 DB now()，页面按用户时区渲染（口径同渔枢系统）
        created_at=utc_now(),
        order_no=next_order_no(db),
        owner=owner,
        status="pending_pay",
        source=str(source or "buy_now"),
        item_kinds=len(lines),
        total_qty=total_qty,
        goods_amount=goods_amount,
        freight=freight,
        discount=discount,
        pay_amount=(goods_amount + freight - discount).quantize(TWO),
        trade_type=trade_type if trade_type in TRADE_TYPES else DEFAULT_TRADE_TYPE,
        delivery_type=delivery_type if delivery_type in DELIVERY_TYPES else "express",
        receiver_name=(receiver_name or "").strip() or None,
        receiver_phone=(receiver_phone or "").strip() or None,
        receiver_address=(receiver_address or "").strip() or None,
        remark=(remark or "").strip() or None,
        supplier_id=lines[0].supplier_id or None,
        supplier_name=lines[0].supplier_name or None,
    )
    db.add(order)
    db.flush()

    for line in lines:
        db.add(
            StockOrderItem(
                order_id=order.id,
                item_id=line.item_id,
                sku_id=line.sku_id,
                spec_text=line.spec_text,
                title=line.title,
                image=line.image,
                price=Decimal(str(line.price)),
                qty=int(line.qty),
                amount=line.amount,
            )
        )
    if commit:
        db.commit()
        db.refresh(order)
    else:
        db.flush()
    return order


def _amounts_of(db: Session, lines: list[BuyLine], freight: Decimal | None = None):
    """一组商品的商品金额 / 件数 / 运费（运费：同一款货只收一次）。"""
    goods = Decimal("0.00")
    qty = 0
    for line in lines:
        goods += Decimal(str(line.price)) * int(line.qty)
        qty += int(line.qty)
    goods = goods.quantize(TWO)
    if freight is None:
        freight = Decimal("0.00")
        seen: set[str] = set()
        for line in lines:
            if line.item_id in seen:
                continue
            seen.add(line.item_id)
            item = (
                db.execute(select(SupplierItem).where(SupplierItem.item_id == line.item_id))
                .scalars()
                .first()
            )
            if item is not None:
                freight += freight_of_item(item)
    freight = Decimal(str(freight)).quantize(TWO)
    return goods, qty, freight


def create_split_order(
    db: Session,
    owner: str,
    groups: list[dict],
    *,
    remark: str = "",
    trade_type: str = DEFAULT_TRADE_TYPE,
    delivery_type: str = "express",
    source: str = "cart",
    commit: bool = True,
) -> StockOrder:
    """采购车「合并支付 · 拆单履约」：一张母单收钱，按收货地址拆成 N 张子单各发各的。

    2026-10-12 用户拍板：采购车里可以给每一款货各填一个收货地址（一次给好几个人
    各带一件），结算时按「收货地址 + 供应商」拆开 —— 钱只付一次（母单），
    货由各子单分别发货、分别确认收货。

    groups 里每组：`{"receiver": {name/phone/address}, "supplier_id", "supplier_name",
    "lines": [BuyLine...], "freight"（可选）}`。
    """
    if not groups:
        raise ValueError("没有可下单的商品")

    order_no = next_order_no(db)
    first = groups[0].get("receiver") or {}
    only_one = len(groups) == 1

    total_goods = Decimal("0.00")
    total_qty = 0
    total_freight = Decimal("0.00")
    prepared = []
    for group in groups:
        lines = list(group.get("lines") or [])
        if not lines:
            continue
        goods, qty, freight = _amounts_of(db, lines, group.get("freight"))
        total_goods += goods
        total_qty += qty
        total_freight += freight
        prepared.append((group, lines, goods, qty, freight))

    if not prepared:
        raise ValueError("没有可下单的商品")

    # 母单：只负责收钱（没有商品明细行，明细都在子单上）
    parent = StockOrder(
        created_at=utc_now(),
        order_no=order_no,
        owner=owner,
        status="pending_pay",
        source=str(source or "cart"),
        order_kind="main",
        sub_count=len(prepared),
        item_kinds=sum(len(lines) for _g, lines, _a, _q, _f in prepared),
        total_qty=total_qty,
        goods_amount=total_goods.quantize(TWO),
        freight=total_freight.quantize(TWO),
        discount=Decimal("0.00"),
        pay_amount=(total_goods + total_freight).quantize(TWO),
        trade_type=trade_type if trade_type in TRADE_TYPES else DEFAULT_TRADE_TYPE,
        delivery_type=delivery_type if delivery_type in DELIVERY_TYPES else "express",
        receiver_name=(first.get("name") or "").strip() or None if only_one else "（多个收货地址）",
        receiver_phone=(first.get("phone") or "").strip() or None if only_one else None,
        receiver_address=(
            (first.get("address") or "").strip() or None
            if only_one
            else f"已拆成 {len(prepared)} 张子单，各发各的地址"
        ),
        remark=(remark or "").strip() or None,
        supplier_id=str(prepared[0][0].get("supplier_id") or "") or None,
        supplier_name=str(prepared[0][0].get("supplier_name") or "") or None,
    )
    db.add(parent)
    db.flush()

    subs: list[StockOrder] = []
    for index, (group, lines, goods, qty, freight) in enumerate(prepared, start=1):
        receiver = group.get("receiver") or {}
        sub = StockOrder(
            created_at=utc_now(),
            order_no=f"{order_no}-{index}",
            parent_order_no=order_no,
            order_kind="sub",
            sub_index=index,
            owner=owner,
            status="pending_pay",
            source=str(source or "cart"),
            item_kinds=len(lines),
            total_qty=qty,
            goods_amount=goods,
            freight=freight,
            discount=Decimal("0.00"),
            pay_amount=(goods + freight).quantize(TWO),
            trade_type=parent.trade_type,
            delivery_type=parent.delivery_type,
            receiver_name=(receiver.get("name") or "").strip() or None,
            receiver_phone=(receiver.get("phone") or "").strip() or None,
            receiver_address=(receiver.get("address") or "").strip() or None,
            remark=parent.remark,
            supplier_id=str(group.get("supplier_id") or "") or None,
            supplier_name=str(group.get("supplier_name") or "") or None,
        )
        db.add(sub)
        db.flush()
        for line in lines:
            db.add(
                StockOrderItem(
                    order_id=sub.id,
                    item_id=line.item_id,
                    sku_id=line.sku_id,
                    spec_text=line.spec_text,
                    title=line.title,
                    image=line.image,
                    price=Decimal(str(line.price)),
                    qty=int(line.qty),
                    amount=line.amount,
                )
            )
        subs.append(sub)

    if commit:
        db.commit()
        db.refresh(parent)
    else:
        db.flush()
    return parent


def sub_orders(db: Session, order: StockOrder) -> list[StockOrder]:
    """母单 → 它的子单（按子单序号）。"""
    if str(order.order_kind or "normal") != "main":
        return []
    return list(
        db.execute(
            select(StockOrder)
            .where(StockOrder.parent_order_no == order.order_no)
            .order_by(StockOrder.sub_index)
        )
        .scalars()
        .all()
    )


def sync_parent_status(db: Session, parent: StockOrder, *, commit: bool = True) -> str:
    """按子单的实际情况回写母单状态：

    · 有一张子单待支付 → 母单待支付
    · 全取消 → 母单已取消
    · 全部完成 → 母单已完成（记完成时间）
    · 其余（有已发货 / 部分发货）→ 母单「已发货」，页面上写清还有几张在路上
    """
    subs = sub_orders(db, parent)
    if not subs:
        return parent.status
    statuses = [str(sub.status) for sub in subs]
    if all(s == "completed" for s in statuses):
        parent.status = "completed"
        parent.completed_at = parent.completed_at or utc_now()
    elif all(s == "cancelled" for s in statuses):
        parent.status = "cancelled"
        parent.cancelled_at = parent.cancelled_at or utc_now()
        parent.cancel_reason = parent.cancel_reason or "子单全部取消"
    elif any(s == "pending_pay" for s in statuses):
        parent.status = "pending_pay"
    elif any(s in ("paid", "shipped") for s in statuses):
        parent.status = "shipped" if any(s == "shipped" for s in statuses) else "paid"
        parent.shipped_at = parent.shipped_at or next(
            (sub.shipped_at for sub in subs if sub.shipped_at), None
        )
    if commit:
        db.commit()
        db.refresh(parent)
    else:
        db.flush()
    return parent.status


def get_order(db: Session, order_no: str, owner: str | None = None) -> StockOrder | None:
    query = select(StockOrder).where(StockOrder.order_no == str(order_no))
    if owner is not None:
        query = query.where(StockOrder.owner == owner)
    return db.execute(query).scalars().first()


def get_items(db: Session, order: StockOrder) -> list[StockOrderItem]:
    return list(
        db.execute(
            select(StockOrderItem)
            .where(StockOrderItem.order_id == order.id)
            .order_by(StockOrderItem.id)
        )
        .scalars()
        .all()
    )


def list_orders(
    db: Session,
    owner: str,
    *,
    status: str = "",
    limit: int = 200,
    include_subs: bool = False,
) -> list[StockOrder]:
    """采购单列表：**默认只列母单**（合并单的子单跟着母单走，不单独占一行）。

    2026-10-12 拆单之后，一次结算会落 1 张母单 + N 张子单；列表里只让人看到母单，
    子单在母单详情页里看，否则同一笔钱会显示成一堆单子。
    """
    query = select(StockOrder).where(StockOrder.owner == owner)
    if not include_subs:
        query = query.where(StockOrder.parent_order_no.is_(None))
    if status and status in STATUS_TEXT:
        query = query.where(StockOrder.status == status)
    query = query.order_by(StockOrder.id.desc()).limit(limit)
    return list(db.execute(query).scalars().all())


def status_counts(db: Session, owner: str) -> dict[str, int]:
    rows = db.execute(
        select(StockOrder.status, func.count(StockOrder.id))
        .where(StockOrder.owner == owner, StockOrder.parent_order_no.is_(None))
        .group_by(StockOrder.status)
    ).all()
    return {str(status): int(count) for status, count in rows}


def pay_order(db: Session, order: StockOrder, method: str, *, commit: bool = True):
    """模拟支付：只有待支付能付；成功后状态转「待发货」并记支付流水号。

    合并单（2026-10-12）：母单付一次钱，下面的子单跟着一起变成「待发货」——
    各子单分别发货，钱还是只收这一次。
    """
    if order.status != "pending_pay":
        return False, f"当前是「{status_text(order.status)}」，不能支付"
    pay_method = method if method in PAY_METHODS else DEFAULT_PAY_METHOD
    order.pay_method = pay_method
    order.pay_no = "PAY{}{:04d}".format(
        beijing_now().strftime("%Y%m%d%H%M%S"), random.randint(0, 9999)
    )
    order.paid_at = utc_now()
    order.status = "paid"
    db.flush()

    subs: list[StockOrder] = []
    if str(order.order_kind or "normal") == "main":
        # 子单跟着一起付掉（钱记在母单的流水号上，不再各收一次）
        subs = sub_orders(db, order)
        for sub in subs:
            if sub.status != "pending_pay":
                continue
            sub.pay_method = pay_method
            sub.pay_no = order.pay_no
            sub.paid_at = order.paid_at
            sub.status = "paid"
            db.flush()

    if commit:
        db.commit()
        db.refresh(order)
    else:
        db.flush()

    # 付款后把这笔采购记进渔枢系统的资金页（应付流水），否则那边只看到销售订单的钱。
    # 合并单：按**子单**各记一条（母单再记一遍钱就重了）
    for row in (subs or [order]):
        _sync_paid_to_hub(row)
    extra = f"，已拆成 {len(subs)} 张子单分别发货" if len(subs) > 1 else ""
    return True, f"支付成功（{pay_method_text(pay_method)}），等待平台发货{extra}"


def _sync_paid_to_hub(order) -> None:
    """采购付款 → 渔枢系统资金流水（桥坏了也不影响采购流程）。"""
    try:
        from .hub_sync import sync_paid_purchase

        sync_paid_purchase(order)
    except Exception as exc:  # noqa: BLE001
        logger.warning("采购单 %s 同步渔枢系统资金流水失败：%s", order.order_no, exc)


def cancel_order(db: Session, order: StockOrder, reason: str = "", *, commit: bool = True):
    """取消采购单：未支付直接关单；已支付的按退款处理（钱原路退回，状态仍是已取消）。

    合并单（2026-10-12）：取消母单 = 把所有还没走完的子单一起取消；
    也可以只取消某一张子单（母单状态按剩下的子单重算）。
    """
    kind = str(order.order_kind or "normal")
    if kind == "main":
        subs = [sub for sub in sub_orders(db, order) if sub.status not in TERMINAL]
        if not subs:
            return False, f"当前是「{status_text(order.status)}」，不能取消"
        done = []
        for sub in subs:
            if sub.status == "shipped":
                continue
            ok, _msg = cancel_order(db, sub, reason, commit=False)
            if ok:
                done.append(sub.order_no)
        if not done:
            return False, "子单都已发货，不能直接取消，请联系平台处理"
        order.status = "cancelled"
        order.cancelled_at = utc_now()
        order.cancel_reason = (reason or "").strip() or f"取消 {len(done)} 张子单"
        db.flush()
        sync_parent_status(db, order, commit=False)
        if commit:
            db.commit()
            db.refresh(order)
        return True, f"已取消 {len(done)} 张子单，支付金额将原路退回"

    if order.status in TERMINAL:
        return False, f"当前是「{status_text(order.status)}」，不能取消"
    if order.status == "shipped":
        return False, "已发货的采购单不能直接取消，请联系平台处理"
    paid = order.status == "paid"
    order.status = "cancelled"
    order.cancelled_at = utc_now()
    order.cancel_reason = (reason or "").strip() or ("已支付，取消后退款" if paid else "未支付取消")
    if commit:
        db.commit()
        db.refresh(order)
    else:
        db.flush()
    # 已支付的取消：渔枢系统资金页那条应付流水要作废（钱要退回来，不该再挂着）
    if paid:
        try:
            from .hub_sync import void_purchase_settlement

            void_purchase_settlement(order.order_no)
        except Exception as exc:  # noqa: BLE001
            logger.warning("采购单 %s 作废渔枢系统资金流水失败：%s", order.order_no, exc)
    # 子单变了 → 母单状态跟着重算（还剩几张在走 / 全取消了）
    _sync_parent_after(db, order, commit=commit)
    return True, ("已取消，支付金额将原路退回" if paid else "已取消")


def _sync_parent_after(db: Session, order: StockOrder, *, commit: bool = True) -> None:
    """子单状态变了，把它挂的母单回写一遍（没有母单就什么事也没有）。"""
    parent_no = str(order.parent_order_no or "")
    if not parent_no:
        return
    parent = get_order(db, parent_no)
    if parent is None:
        return
    sync_parent_status(db, parent, commit=commit)


def mark_shipped(
    db: Session,
    order: StockOrder,
    *,
    company: str = "",
    shipping_no: str = "",
    commit: bool = True,
):
    """平台侧发货（渔货栈这里先支持手动回填，物流单号可选）。

    合并单：母单只管收钱，发货要在**子单**上做（各发各的地址）。
    """
    if str(order.order_kind or "normal") == "main":
        return False, "这是一张合并单，请在下面各张子单上分别发货"
    if order.status != "paid":
        return False, f"当前是「{status_text(order.status)}」，不能发货"
    order.status = "shipped"
    order.shipping_company = (company or "").strip() or None
    order.shipping_no = (shipping_no or "").strip() or None
    order.shipped_at = utc_now()
    if commit:
        db.commit()
        db.refresh(order)
    else:
        db.flush()
    _sync_parent_after(db, order, commit=commit)
    return True, "已标记发货"


def complete_order(db: Session, order: StockOrder, *, commit: bool = True):
    """确认收货 → 采购单完成（合并单在子单上做，最后一张收完母单才算完成）。"""
    if str(order.order_kind or "normal") == "main":
        return False, "这是一张合并单，请在下面各张子单上分别确认收货"
    if order.status != "shipped":
        return False, f"当前是「{status_text(order.status)}」，不能确认收货"
    order.status = "completed"
    order.completed_at = utc_now()
    if commit:
        db.commit()
        db.refresh(order)
    else:
        db.flush()
    _sync_parent_after(db, order, commit=commit)
    return True, "已确认收货，采购单完成"


def progress_of(order: StockOrder, subs: list[StockOrder] | None = None) -> list[dict]:
    """详情页进度条：待支付 → 待发货 → 已发货 → 已完成。

    合并单（2026-10-12）：中间那一步写成「分批发货 x/N」，让买家一眼看到还有几张在路上。
    """
    if order.status == "cancelled":
        return [
            {"key": "pending_pay", "label": "提交订单", "done": True, "time": order.created_at},
            {"key": "cancelled", "label": "已取消", "done": True, "time": order.cancelled_at},
        ]
    is_main = str(order.order_kind or "normal") == "main"
    sub_total = len(subs or [])
    shipped_cnt = len([s for s in (subs or []) if str(s.status) in ("shipped", "completed")])
    ship_label = "平台发货"
    if is_main and sub_total:
        ship_label = f"分批发货 {shipped_cnt}/{sub_total}"
    flow = [
        ("pending_pay", "提交订单", order.created_at),
        ("paid", "支付完成", order.paid_at),
        ("shipped", ship_label, order.shipped_at),
        ("completed", "已完成", order.completed_at),
    ]
    current_index = STATUS_ORDER.index(order.status) if order.status in STATUS_ORDER else 0
    out = []
    for index, (key, label, at) in enumerate(flow):
        out.append({"key": key, "label": label, "done": index <= current_index, "time": at})
    return out


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(TWO)


def created_text(order: StockOrder) -> str:
    """下单时间文案：库里存 UTC，这里按用户时区（默认北京）渲染。"""
    at: datetime | None = order.created_at
    return fmt(at) or "—"
