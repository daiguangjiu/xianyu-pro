"""出库管理：4 类出库（销售 / 报修 / 调拨 / 拆机件）共一张表，用 type 区分。

出库是**唯一会让货离开你手**的动作，所以这里的校验最严。
最容易出事的地方：销售出库只校验「能不能卖」，没校验「等级对不对」——
A 档当 S 档发出去，差价几百块且买家收货后必然纠纷。
"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .timeutil import now as _now
from .models import (
    LEDGER_OUT,
    OUT_PARTS,
    OUT_REPAIR,
    OUT_SALE,
    OUT_TRANSFER,
    OUTBOUND_STATUS_CANCELLED,
    OUTBOUND_STATUS_DRAFT,
    OUTBOUND_STATUS_PICKING,
    OUTBOUND_STATUS_SHIPPED,
    STATUS_LOCKED,
    STATUS_OUT_REPAIR,
    STATUS_PARTS,
    STATUS_SELLABLE,
    STATUS_SOLD,
    WH_OUT_REPAIR,
    WH_PARTS,
    WmsItem,
    WmsOutboundItem,
    WmsOutboundOrder,
    WmsWarehouse,
    grade_label,
)
from . import audit
from . import bridge
from . import ledger
from . import state


def next_order_no(session: Session, at=None) -> str:
    moment = at or _now()
    prefix = "CK" + moment.strftime("%y%m%d")
    cnt = int(
        session.scalar(
            select(func.count()).select_from(WmsOutboundOrder).where(
                WmsOutboundOrder.order_no.like(f"{prefix}%")
            )
        ) or 0
    )
    return f"{prefix}{cnt + 1:03d}"


def create(session: Session, data, operator: str = "") -> WmsOutboundOrder:
    otype = str(data.get("type") or OUT_SALE).upper()
    if otype not in (OUT_SALE, OUT_REPAIR, OUT_TRANSFER, OUT_PARTS):
        raise ValueError("出库类型只支持：销售 / 报修 / 调拨 / 拆机件")
    warehouse = str(data.get("warehouse_code") or "").upper()
    row = WmsOutboundOrder(
        order_no=next_order_no(session),
        type=otype,
        target_no=str(data.get("target_no") or "").strip(),
        warehouse_code=warehouse,
        to_warehouse=str(data.get("to_warehouse") or "").upper(),
        item_count=0,
        status=OUTBOUND_STATUS_DRAFT,
        express_company=str(data.get("express_company") or "").strip(),
        waybill_no=str(data.get("waybill_no") or "").strip(),
        receiver_name=str(data.get("receiver_name") or "").strip(),
        receiver_phone=str(data.get("receiver_phone") or "").strip(),
        receiver_addr=str(data.get("receiver_addr") or "").strip(),
        remark=str(data.get("remark") or "").strip(),
        operator=str(operator or ""),
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(row)
    session.flush()
    audit.log(session, "outbound", row.id, "outbound", after=f"{otype} {row.order_no}",
              operator=operator)
    session.commit()
    return row


def _filtered_stmt(q: str = "", type: str = "", status: str = ""):
    stmt = select(WmsOutboundOrder)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                WmsOutboundOrder.order_no.like(like),
                WmsOutboundOrder.target_no.like(like),
                WmsOutboundOrder.waybill_no.like(like),
                WmsOutboundOrder.receiver_name.like(like),
            )
        )
    if type:
        stmt = stmt.where(WmsOutboundOrder.type == type)
    if status:
        stmt = stmt.where(WmsOutboundOrder.status == status)
    return stmt


def list_orders(
    session: Session, q: str = "", type: str = "", status: str = "",
    limit: int | None = None, offset: int = 0,
) -> list[WmsOutboundOrder]:
    stmt = _filtered_stmt(q, type, status)
    stmt = stmt.order_by(WmsOutboundOrder.id.desc())
    if offset:
        stmt = stmt.offset(int(offset))
    if limit is not None:
        stmt = stmt.limit(int(limit))
    return list(session.scalars(stmt).all())


def count_orders(session: Session, q: str = "", type: str = "", status: str = "") -> int:
    stmt = _filtered_stmt(q, type, status)
    return int(session.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0)


def lines_of(session: Session, order_id: int) -> list[WmsOutboundItem]:
    return list(
        session.scalars(
            select(WmsOutboundItem)
            .where(WmsOutboundItem.outbound_id == order_id)
            .order_by(WmsOutboundItem.id.asc())
        ).all()
    )


def lines_of_orders(session: Session, order_ids: list[int]) -> dict[int, list[dict]]:
    """一批出库单各自的商品清单（列表页按单分组展示，一次查询别 N+1）。

    2026-10-15 用户拍板：列表「出库商品」列要显示**商品名称 + 渔货号** ——
    光一串条码对不上人脑里的货。渔货号 / 商品名从货品档案 join 出来；
    货品已经删了的退回单上留的快照标题。
    """
    ids = [int(i) for i in (order_ids or []) if i]
    if not ids:
        return {}
    rows = list(session.scalars(
        select(WmsOutboundItem)
        .where(WmsOutboundItem.outbound_id.in_(ids))
        .order_by(WmsOutboundItem.id.asc())
    ).all())
    item_ids = {int(r.item_id) for r in rows if r.item_id}
    items: dict[int, WmsItem] = {}
    if item_ids:
        items = {
            int(it.id): it
            for it in session.scalars(
                select(WmsItem).where(WmsItem.id.in_(item_ids))
            ).all()
        }
    out: dict[int, list[dict]] = {}
    for row in rows:
        it = items.get(int(row.item_id)) if row.item_id else None
        out.setdefault(int(row.outbound_id), []).append(
            {
                "barcode": str(row.barcode or ""),
                "title": str((it.product_name or it.title) if it else (row.title or "")),
                "fish_code": str((it.fish_code or "") if it else ""),
                "qty": int((it.qty or 1)) if it else 1,
            }
        )
    return out


def _lock_and_pick(
    session: Session, order: WmsOutboundOrder, item: WmsItem,
    require_grade: str, operator: str,
) -> WmsOutboundItem:
    """把一台货挂到出库单上：可售先锁货再拣（校验全走 :func:`pick` 一条路）。"""
    if str(item.status or "").upper() == STATUS_SELLABLE:
        lock(session, order, item, operator=operator)
    return pick(
        session, order, item.barcode, require_grade=require_grade, operator=operator,
    )


def add_lines_from_code(
    session: Session,
    order: WmsOutboundOrder,
    *,
    barcode: str = "",
    fish_code: str = "",
    sku_code: str = "",
    qty: int | str | None = None,
    require_grade: str = "",
    operator: str = "",
) -> tuple[int, list[str]]:
    """建单时直接把货挂上（2026-10-15）：出库单不用建完再一台台扫。

    · **条码**认一台 —— 一台一条码，最准；
    · **渔货号**认一款（2026-10-15 加，与详情页「订单商品」面板同一口径）——
      一款多台按「数量」取前 N 台；
    · **货号**认同款多台 —— 一个货号可以挂多台，按「数量」取前 N 台；
    · 条码 > 渔货号 > 货号，填了前一个就不管后两个。

    挂不上的（状态不对 / 已被别的单占用 / 分区放错…）**不拦建单**，
    把原因一条条返回去，页面黄条说明 —— 别让人为一张写错的货号把单建不成。
    返回 ``(挂上了几台, 未挂上的原因)``。
    """
    want = int(qty or 0) or 1
    added = 0
    notes: list[str] = []

    if str(barcode or "").strip():
        try:
            _lock_and_pick(session, order, _item_by_code(session, barcode),
                           require_grade, operator)
            added += 1
        except ValueError as exc:
            notes.append(str(exc))
        return added, notes

    code = str(fish_code or "").strip().upper() or str(sku_code or "").strip()
    if not code:
        return 0, notes
    if str(fish_code or "").strip():
        rows = list(session.scalars(
            select(WmsItem).where(WmsItem.fish_code == code).order_by(WmsItem.id.asc())
        ).all())
        if not rows:
            return 0, [f"渔货号 {code} 在货品台账里查不到，先确认再挂"]
        label = "渔货号"
    else:
        rows = list(session.scalars(
            select(WmsItem).where(WmsItem.sku_code == code).order_by(WmsItem.id.asc())
        ).all())
        if not rows:
            return 0, [f"货号 {code} 在货品台账里查不到，先确认再挂"]
        label = "货号"
    already: set[int] = {
        int(x.item_id) for x in lines_of(session, order.id) if x.item_id
    }
    for item in rows:
        if added >= want:
            break
        if item.id in already:
            continue
        try:
            _lock_and_pick(session, order, item, require_grade, operator)
            added += 1
        except ValueError as exc:
            notes.append(str(exc))
    if not added and not notes:
        notes.append(f"{label} {code} 名下没有可出库的货")
    elif added and added < want:
        notes.append(f"{label} {code} 只挂上 {added} 台（要 {want} 台）")
    return added, notes


def _item_by_code(session: Session, code: str) -> WmsItem:
    """按条码 / 显示编码 / 货号认一台货（建单挂货用，别再让运营抄两遍）。"""
    compact = str(code or "").strip().replace("-", "").upper()
    item = session.scalar(select(WmsItem).where(WmsItem.barcode == compact))
    if not item:
        item = session.scalar(select(WmsItem).where(WmsItem.sku_code == str(code or "").strip()))
    if not item:
        raise ValueError(f"条码 / 货号 {code} 在货品台账里查不到")
    return item


def pick_item(session: Session, order: WmsOutboundOrder, item_id: int, operator: str = "") -> WmsOutboundItem:
    """从「订单商品」面板点选一台出库（2026-10-15）。

    与扫码拣货走**同一套校验**（:func:`pick`）：可售先自动锁货再拣，
    已被别的单占用 / 分区放错 / 账实不符照样拦 —— 面板只是把条码替人填了。
    """
    item = session.get(WmsItem, int(item_id or 0))
    if not item:
        raise ValueError("货品不存在")
    if session.scalar(
        select(WmsOutboundItem).where(
            WmsOutboundItem.outbound_id == order.id,
            WmsOutboundItem.item_id == item.id,
        )
    ):
        raise ValueError(f"{item.display_code} 已经在本单里了")
    return _lock_and_pick(session, order, item, "", operator)


def lock(session: Session, order: WmsOutboundOrder, item: WmsItem, operator: str = "") -> None:
    """订单锁货：SELLABLE → LOCKED，记 locked_by（超时 30 分钟自动释放）。"""
    if str(item.status or "").upper() != STATUS_SELLABLE:
        raise ValueError(f"「{state.STATUS_LABELS.get(item.status, item.status)}」状态不能锁货")
    if item.locked_by and item.locked_by != order.order_no:
        raise ValueError(f"该货品已被 {item.locked_by} 占用")
    ok, why = state.can_sell(item.grade, item.status)
    if not ok:
        raise ValueError(why)
    item.locked_by = order.order_no
    item.locked_at = _now()
    ledger.apply(
        session, item,
        biz_type="LOCK",
        biz_id=order.order_no,
        to_status=STATUS_LOCKED,
        operator=operator,
        remark=f"订单 {order.order_no} 锁货",
        idempotent_key=f"LOCK:{order.order_no}:{item.id}",
    )
    session.commit()


def release(session: Session, item: WmsItem, operator: str = "") -> None:
    """释放锁货：LOCKED → SELLABLE（取消订单 / 超时）。"""
    if str(item.status or "").upper() != STATUS_LOCKED:
        return
    order_no = item.locked_by
    item.locked_by = ""
    item.locked_at = None
    ledger.apply(
        session, item,
        biz_type="UNLOCK",
        biz_id=order_no or "",
        to_status=STATUS_SELLABLE,
        operator=operator,
        remark="取消占用，释放回可售",
        idempotent_key=f"UNLOCK:{order_no}:{item.id}:{_now().timestamp()}",
    )
    session.commit()


def pick(
    session: Session,
    order: WmsOutboundOrder,
    barcode: str,
    require_grade: str = "",
    operator: str = "",
) -> WmsOutboundItem:
    """拣货扫码校验：后端强制 5 项，错货 / 错等级当场拦。

    1. 条码存在
    2. 销售出库必须是本单锁定的货（没锁就先锁），没被别的单占用
    3. 货品等级 == 订单要求等级
    4. 实际库位等级区 == 货品等级（A 档不能放 S 区）
    5. 账实一致（有流水支撑），且所在仓允许出库
    """
    compact = str(barcode or "").strip().replace("-", "").upper()
    item = session.scalar(select(WmsItem).where(WmsItem.barcode == compact))
    if not item:
        raise ValueError(f"未知条码 {barcode}，拒收")
    if session.scalar(
        select(WmsOutboundItem).where(
            WmsOutboundItem.outbound_id == order.id,
            WmsOutboundItem.item_id == item.id,
        )
    ):
        raise ValueError(f"{item.display_code} 已经在本单里了")

    need = str(require_grade or "").strip().upper()
    status = str(item.status or "").upper()

    if order.type == OUT_SALE:
        if item.locked_by and item.locked_by != order.order_no:
            raise ValueError(f"{item.display_code} 已被 {item.locked_by} 占用")
        if not item.locked_by:
            raise ValueError(f"{item.display_code} 还没被本单锁定，先锁货再拣")
        ok, why = state.can_sell(item.grade, status)
        if not ok:
            raise ValueError(f"{item.display_code}：{why}")
        if need and need != str(item.grade or "").upper():
            raise ValueError(
                f"等级不符：订单要 {need}，实扫 {item.grade}（{item.display_code}）"
            )
    elif order.type == OUT_REPAIR:
        if status not in (STATUS_OUT_REPAIR, STATUS_SELLABLE, "GRADED"):
            raise ValueError(f"{item.display_code}：{status} 状态不能报修出库")
    elif order.type == OUT_PARTS:
        if status != STATUS_PARTS:
            raise ValueError(f"{item.display_code}：只有拆机件（PARTS）能走拆机件出库")
    elif order.type == OUT_TRANSFER:
        if not order.to_warehouse:
            raise ValueError("调拨出库要先填目标仓")
        if str(item.warehouse_code or "").upper() != str(order.warehouse_code or "").upper():
            raise ValueError(f"{item.display_code} 不在 {order.warehouse_code} 仓")

    if status in state.NO_OUTBOUND_STATUSES:
        raise ValueError(f"{item.display_code}：「{status}」状态禁止出库")

    # 库位等级区校验：A 档货放进 S 区要告警（仓管拿错一单就是几百块）
    if item.location_code:
        loc = session.scalar(
            select(WmsWarehouse).where(WmsWarehouse.code == item.location_code)
        )
        if loc and loc.grade_zone and loc.grade_zone.upper() != str(item.grade or "").upper():
            raise ValueError(
                f"分区存放错误：{item.display_code} 是 {item.grade} 档，却在 {loc.grade_zone} 区"
            )
    wh = session.scalar(
        select(WmsWarehouse).where(WmsWarehouse.code == str(item.warehouse_code or "").upper())
    )
    if wh and not wh.can_out and order.type != OUT_REPAIR:
        raise ValueError(f"{item.display_code} 所在仓 {wh.name} 不允许出库")

    # 账实一致：余额桶里得有这台货（没有流水支撑的货不许出）
    bucket = ledger._bucket(session, item.warehouse_code, status, str(item.grade or "").upper())
    if bucket is None or int(bucket.qty or 0) <= 0:
        raise ValueError(f"{item.display_code} 账实不符（没有库存流水支撑），先盘点再出库")

    row = WmsOutboundItem(
        outbound_id=order.id,
        item_id=item.id,
        barcode=item.barcode,
        require_grade=need,
        grade=str(item.grade or "").upper(),
        title=item.title,
        price=float(item.selling_price or 0.0),
        cost=float(item.purchase_cost or 0.0),
        picked=True,
        picked_at=_now(),
        created_at=_now(),
    )
    session.add(row)
    order.item_count = int(order.item_count or 0) + 1
    if str(order.status or "").lower() == OUTBOUND_STATUS_DRAFT:
        order.status = OUTBOUND_STATUS_PICKING
    order.updated_at = _now()
    session.commit()
    return row


def ship(session: Session, order_id: int, operator: str = "", waybill_no: str = "",
         express_company: str = "") -> WmsOutboundOrder:
    """出库核销：写流水、结转成本、货品置终态（销售=已售，拆机件=已出，报修=在途）。"""
    order = session.get(WmsOutboundOrder, order_id)
    if not order:
        raise ValueError("出库单不存在")
    if str(order.status or "").lower() == OUTBOUND_STATUS_SHIPPED:
        raise ValueError("这张单已经出库了")
    lines = lines_of(session, order.id)
    if not lines:
        raise ValueError("还没拣货，不能出库")
    if waybill_no:
        order.waybill_no = str(waybill_no)
    if express_company:
        order.express_company = str(express_company)
    if order.type == OUT_SALE and not order.waybill_no:
        raise ValueError("销售出库必须填运单号（先打面单）")

    sold_rows: list[tuple[str, int]] = []     # 真卖出去的（要同步给渔枢 / 渔货栈减库存）
    for line in lines:
        item = session.get(WmsItem, line.item_id)
        if not item:
            continue
        if order.type == OUT_SALE:
            target_status = STATUS_SOLD
            target_wh = ""
        elif order.type == OUT_PARTS:
            target_status = STATUS_SOLD
            target_wh = ""
        elif order.type == OUT_REPAIR:
            target_status = STATUS_OUT_REPAIR
            target_wh = WH_OUT_REPAIR
        else:  # 调拨
            target_status = str(item.status or "").upper()
            target_wh = order.to_warehouse

        ledger.apply(
            session, item,
            biz_type=LEDGER_OUT,
            biz_id=order.order_no,
            to_warehouse=target_wh,
            to_status=target_status,
            operator=operator,
            remark=f"{order.type} 出库核销（{order.order_no}）",
            idempotent_key=f"OUT:{order.order_no}:{item.id}",
            check_transition=False,
        )
        if target_status == STATUS_SOLD:
            item.sold_at = _now()
            # 这台（这条带 N 台的货）是真的卖掉了 —— 记下来等会儿同步给渔枢 / 渔货栈减库存
            sold_rows.append((str(item.barcode or ""), int(item.qty or 0) or 1))
        item.locked_by = ""
        item.locked_at = None
        line.price = float(item.selling_price or line.price or 0.0)
        line.cost = float(item.purchase_cost or 0.0)

    order.status = OUTBOUND_STATUS_SHIPPED
    order.shipped_at = _now()
    order.updated_at = _now()
    audit.log(session, "outbound", order.id, "outbound", after=f"出库 {order.order_no}",
              operator=operator, note=f"{len(lines)} 件")
    session.commit()

    # 渔集出库 = 货卖了：渔枢商品库存与渔货栈平台库存跟着减（2026-10-10）。
    # 放在渔集自己的事务提交之后 —— 同步是「账」，出库是「现场动作」，
    # 同步失败（桥断 / 商品没认到）绝不能把出库回滚掉。
    order.hub_sync = _sync_hub_stock(session, order, sold_rows, operator)

    # 渔货栈的订单（target_no = 渔枢订单号）：出库填了运单号就把单号回填渔枢 ——
    # 发货页的物流单号栏、群里 @ 发送人、语音播报全部走平台同一套（2026-10-15）。
    # 桥断 / 订单不存在只降级（报告记进审计），绝不能拖垮出库核销。
    order.tracking_sync = {}
    if order.type == OUT_SALE and order.waybill_no and order.target_no:
        order.tracking_sync = bridge.apply_hub_tracking(
            order.target_no, order.waybill_no, operator
        )
        if order.tracking_sync:
            audit.log(
                session, "outbound", order.id, "tracking_sync",
                after=f"{order.waybill_no}",
                operator=operator,
                note=(
                    "物流单号已回填渔枢订单并发群通知"
                    if order.tracking_sync.get("ok")
                    else "渔枢订单没找到，单号只留在渔集"
                ),
            )
            session.commit()
    return order


def _sync_hub_stock(session: Session, order: WmsOutboundOrder,
                    sold_rows: list[tuple[str, int]], operator: str) -> dict:
    """把「卖掉的台数」推给渔枢（商品库存）与渔货栈（平台库存），并把结果留痕。"""
    if not sold_rows:
        return {}
    report = bridge.push_stock([(code, -qty) for code, qty in sold_rows])
    note = bridge.hub_sync_note(report)
    audit.log(session, "outbound", order.id, "stock_sync",
              after=f"库存同步 {len(sold_rows)} 台", operator=operator,
              note=note or "渔枢 / 渔货栈没有对应商品，未同步")
    session.commit()
    return report


def cancel(session: Session, order_id: int, operator: str = "") -> WmsOutboundOrder:
    """取消出库单：把锁的货全部释放回可售。"""
    order = session.get(WmsOutboundOrder, order_id)
    if not order:
        raise ValueError("出库单不存在")
    if str(order.status or "").lower() == OUTBOUND_STATUS_SHIPPED:
        raise ValueError("已出库的单不能取消")
    for line in lines_of(session, order.id):
        item = session.get(WmsItem, line.item_id)
        if item and item.locked_by == order.order_no:
            release(session, item, operator=operator)
    order.status = OUTBOUND_STATUS_CANCELLED
    order.updated_at = _now()
    audit.log(session, "outbound", order.id, "outbound", after=f"取消 {order.order_no}",
              operator=operator)
    session.commit()
    return order


def release_timeout_locks(session: Session, minutes: int = 30) -> int:
    """超时锁货自动释放（拍品锁 30 分钟）：避免货被长期占用下不了架。"""
    from datetime import timedelta

    deadline = _now() - timedelta(minutes=int(minutes))
    rows = list(
        session.scalars(
            select(WmsItem).where(
                WmsItem.status == STATUS_LOCKED,
                WmsItem.locked_at.isnot(None),
                WmsItem.locked_at <= deadline,
            )
        ).all()
    )
    for item in rows:
        release(session, item, operator="系统")
    return len(rows)
