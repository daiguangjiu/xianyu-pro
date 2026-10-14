"""库存账本：**余额由流水算出，任何库存变动都必须走这里**。

三条原则（方案 4.1）：
  1. 余额由流水汇总 —— 系统里**没有**「改库存数量」的入口；
  2. 强制扫码 —— ``apply()`` 只接受 WmsItem 对象（条码扫出来的），不接受手填数量；
  3. 在途单独计 —— 报修寄出的货物理上不在手上，单列 ``in_transit_qty``。

所有写操作都带 ``idempotent_key``：网络重试 / 重复点提交只会记一条流水。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .timeutil import now as _now
from .models import (
    LEDGER_ADJUST,
    LEDGER_INBOUND,
    LEDGER_OUT,
    STATUS_OUT_REPAIR,
    STATUS_SOLD,
    WH_HAZARD,
    WH_PENDING,
    WmsItem,
    WmsStockBalance,
    WmsStockLedger,
)
from . import state


def _bucket(session: Session, warehouse: str, status: str, grade: str) -> WmsStockBalance | None:
    return session.scalar(
        select(WmsStockBalance).where(
            WmsStockBalance.warehouse_code == str(warehouse or ""),
            WmsStockBalance.status == str(status or ""),
            WmsStockBalance.grade == str(grade or "").upper(),
        )
    )


def _bump(session: Session, warehouse: str, status: str, grade: str, delta: int,
          in_transit: int = 0, ledger_id: int = 0) -> None:
    """余额加减：为 0 的行直接删掉，免得表里堆一堆空桶。"""
    warehouse = str(warehouse or "")
    if not warehouse:
        return
    row = _bucket(session, warehouse, status, grade)
    if row is None:
        if delta <= 0 and in_transit <= 0:
            return
        row = WmsStockBalance(
            warehouse_code=warehouse,
            status=str(status or ""),
            grade=str(grade or "").upper(),
            qty=0,
            in_transit_qty=0,
        )
        session.add(row)
        session.flush()
    row.qty = int(row.qty or 0) + int(delta)
    row.in_transit_qty = max(int(row.in_transit_qty or 0) + int(in_transit), 0)
    row.last_ledger_id = ledger_id or row.last_ledger_id
    row.updated_at = _now()
    if row.qty <= 0 and row.in_transit_qty <= 0:
        session.delete(row)


def apply(
    session: Session,
    item: WmsItem,
    *,
    biz_type: str,
    biz_id: str = "",
    to_warehouse: str | None = None,
    to_status: str | None = None,
    # 不传就按货品自己的数量（默认 1）。**签名上不能写 qty: int = 1** ——
    # 那样调用方不传时拿到的是 1，永远走不到「按货品数量」这条兜底（2026-10-10）
    qty: int | None = None,
    operator: str = "",
    remark: str = "",
    idempotent_key: str = "",
    at=None,
    check_transition: bool = True,
) -> WmsStockLedger:
    """一次库存变动：校验状态机 → 改货品 → 记流水 → 汇总余额。

    ``to_warehouse`` / ``to_status`` 不传表示不变（只记流水，如入库复核）。

    数量（2026-10-10）：一台货品可以带 N 台（见 ``WmsItem.qty``，**同一个条码**），
    **流转整台走** —— 出库 / 报修 / 移库 / 核销都按这台的数量整体进出，不拆分。
    不传 qty 就按货品自己的数量（老数据默认 1，与「一机一码」时代一致）。
    """
    qty = int(qty or 0) or int(getattr(item, "qty", 0) or 0) or 1
    from_warehouse = str(item.warehouse_code or "")
    from_status = str(item.status or "")
    target_wh = str(to_warehouse or from_warehouse)
    target_st = str(to_status or from_status).upper()
    if check_transition and target_st != from_status:
        state.assert_transition(from_status, target_st)

    key = idempotent_key or f"{biz_type}:{biz_id}:{item.id}:{target_st}:{from_status}"
    existed = session.scalar(
        select(WmsStockLedger).where(WmsStockLedger.idempotent_key == key)
    )
    if existed:
        return existed

    row = WmsStockLedger(
        item_id=item.id,
        barcode=item.barcode,
        biz_type=str(biz_type or "").upper(),
        biz_id=str(biz_id or ""),
        from_warehouse=from_warehouse,
        to_warehouse=target_wh,
        from_status=from_status,
        to_status=target_st,
        qty_delta=int(qty),
        operator=str(operator or ""),
        remark=str(remark or ""),
        idempotent_key=key,
        operated_at=at or _now(),
        created_at=_now(),
    )
    session.add(row)
    session.flush()

    grade = str(item.grade or "").upper()
    if (from_warehouse, from_status) != (target_wh, target_st):
        # 余额按这台的数量整进整出（一台带 N 台就是 ±N，不是 ±1）
        _bump(session, from_warehouse, from_status, grade, -qty,
              in_transit=-qty if from_status == STATUS_OUT_REPAIR else 0, ledger_id=row.id)
        _bump(session, target_wh, target_st, grade, qty,
              in_transit=qty if target_st == STATUS_OUT_REPAIR else 0, ledger_id=row.id)

    item.warehouse_code = target_wh
    item.status = target_st
    item.ver = int(item.ver or 0) + 1
    item.updated_at = _now()
    return row


def inbound(session: Session, item: WmsItem, *, biz_id: str = "", operator: str = "",
            remark: str = "") -> WmsStockLedger:
    """入库：货品从「外部」进入待检仓（from_warehouse 为空，只加不减）。"""
    item.warehouse_code = ""
    item.status = ""
    return apply(
        session, item,
        biz_type=LEDGER_INBOUND,
        biz_id=biz_id,
        to_warehouse=WH_PENDING,
        to_status=state.STATUS_PENDING,
        operator=operator,
        remark=remark,
        idempotent_key=f"IN:{biz_id}:{item.id}",
        check_transition=False,
    )


def write_off(session: Session, item: WmsItem, *, biz_id: str = "", operator: str = "",
              remark: str = "") -> WmsStockLedger:
    """盘亏核销：货品从账面移出（仓库与状态都清空，余额减 1）。

    **严禁为了对平而直接改 items 表** —— 这里只写流水，货品的去向由流水说得清。
    """
    if not remark:
        raise ValueError("盘亏必须填原因")
    return apply(
        session, item,
        biz_type=LEDGER_ADJUST,
        biz_id=biz_id,
        to_warehouse="",
        to_status="",
        operator=operator,
        remark=remark,
        idempotent_key=f"OFF:{biz_id}:{item.id}",
        check_transition=False,
    )


def change_qty(session: Session, item: WmsItem, new_qty: int, *, operator: str = "",
               remark: str = "") -> WmsStockLedger | None:
    """改一台货品的数量（这台到底有几台）：**只动台数，不动仓与状态**。

    仓与状态没变，``apply`` 就不会加减余额（它是按「搬没搬家」判定的），所以这里单独
    写一条差额流水并直接把差额记进所在桶 —— 余额始终由流水说得清。
    """
    old = int(getattr(item, "qty", 0) or 0) or 1
    new = max(int(new_qty or 0), 1)
    if new == old:
        return None
    warehouse = str(item.warehouse_code or "")
    status = str(item.status or "")
    delta = new - old
    row = WmsStockLedger(
        item_id=item.id,
        barcode=item.barcode,
        biz_type=str(LEDGER_ADJUST or "").upper(),
        biz_id="",
        from_warehouse=warehouse,
        to_warehouse=warehouse,
        from_status=status,
        to_status=status,
        qty_delta=int(delta),
        operator=str(operator or ""),
        remark=str(remark or f"改数量 {old} → {new} 台"),
        idempotent_key=f"QTY:{item.id}:{new}:{int(item.ver or 0)}",
        operated_at=_now(),
        created_at=_now(),
    )
    session.add(row)
    session.flush()
    item.qty = new
    if warehouse:
        _bump(session, warehouse, status, str(item.grade or "").upper(), delta,
              ledger_id=row.id)
    return row


def adjust(session: Session, item: WmsItem, *, biz_id: str = "", operator: str = "",
           remark: str = "", to_warehouse: str | None = None,
           to_status: str | None = None) -> WmsStockLedger:
    """盘点调整：唯一允许「不扫动作」的流水，但必须带单据号与原因。"""
    if not remark:
        raise ValueError("盘点调整必须填原因（严禁为了对平而直接改库存）")
    return apply(
        session, item,
        biz_type=LEDGER_ADJUST,
        biz_id=biz_id,
        to_warehouse=to_warehouse,
        to_status=to_status,
        operator=operator,
        remark=remark,
        idempotent_key=f"ADJ:{biz_id}:{item.id}",
        check_transition=False,
    )


def rebuild(session: Session) -> int:
    """按流水重算余额（修数据用）：清表 → 按顺序回放。返回重算后的桶数。"""
    session.query(WmsStockBalance).delete()
    session.flush()
    rows = list(
        session.scalars(
            select(WmsStockLedger).order_by(WmsStockLedger.id.asc())
        ).all()
    )
    for row in rows:
        grade = ""
        item = session.get(WmsItem, row.item_id) if row.item_id else None
        if item is not None:
            grade = str(item.grade or "").upper()
        if (row.from_warehouse, row.from_status) != (row.to_warehouse, row.to_status):
            _bump(session, row.from_warehouse, row.from_status, grade, -int(row.qty_delta or 0),
                  in_transit=-1 if row.from_status == STATUS_OUT_REPAIR else 0,
                  ledger_id=row.id)
            _bump(session, row.to_warehouse, row.to_status, grade, int(row.qty_delta or 0),
                  in_transit=1 if row.to_status == STATUS_OUT_REPAIR else 0,
                  ledger_id=row.id)
    session.commit()
    return int(session.scalar(select(func.count()).select_from(WmsStockBalance)) or 0)


def balances(session: Session) -> list[WmsStockBalance]:
    return list(
        session.scalars(
            select(WmsStockBalance).order_by(
                WmsStockBalance.warehouse_code.asc(),
                WmsStockBalance.status.asc(),
                WmsStockBalance.grade.asc(),
            )
        ).all()
    )


# ---------------------------------------------------------------- 库存口径
# 「在库总数」不含在外报修（货不在手上），但要含拆机件 —— 财务口径与实物口径不同，
# 报表里必须分开列，不要合并成一个「总库存」数字。
STATUS_BUCKETS = (
    ("pending", "待检库存", "PENDING"),
    ("sellable", "可售库存", "SELLABLE"),
    ("locked", "锁定库存", "LOCKED"),
    ("repair", "维修库存", "REPAIR"),
    ("rework", "返工复检", "REWORK"),
    ("out_repair", "在外报修", "OUT_REPAIR"),
    ("parts", "拆机件库存", "PARTS"),
    ("hazard", "危废库存", "HAZARD"),
    ("sold", "已售", "SOLD"),
    ("recall", "召回待检", "RECALL"),
)


def _sum_qty(session: Session, *where) -> int:
    """台数口径（2026-10-10）：一台货品可以带 N 台，数「多少台」要 sum(qty) 不是 count(*)。"""
    return int(
        session.scalar(
            select(func.coalesce(func.sum(WmsItem.qty), 0)).where(*where)
        ) or 0
    )


def _sum_value(session: Session, column, *where) -> float:
    """货值口径：单价 × 台数（一台带 N 台就是 N 份钱）。"""
    return float(
        session.scalar(
            select(func.coalesce(func.sum(WmsItem.qty * column), 0.0)).where(*where)
        ) or 0.0
    )


def summary(session: Session) -> dict:
    """库存总览：各口径数量 + 货值（成本口径 / 售价口径分开）。"""
    buckets: dict[str, dict] = {}
    for key, label, status in STATUS_BUCKETS:
        buckets[key] = {
            "key": key, "label": label, "status": status,
            "qty": _sum_qty(session, WmsItem.status == status),
            "cost": round(_sum_value(session, WmsItem.purchase_cost, WmsItem.status == status), 2),
            "price": round(_sum_value(session, WmsItem.selling_price, WmsItem.status == status), 2),
        }

    sellable = _sum_qty(
        session, WmsItem.status == "SELLABLE", WmsItem.grade.in_(("S", "A"))
    )
    # 在库总数：所有状态 − 已售 − 在外报修（后者货不在手上）− 已盘亏核销（status 为空）
    on_hand = _sum_qty(
        session, WmsItem.status.notin_((STATUS_SOLD, STATUS_OUT_REPAIR, ""))
    )
    return {
        "buckets": buckets,
        "on_hand": on_hand,
        "sellable": sellable,
        "in_transit": buckets["out_repair"]["qty"],
    }


def aging(session: Session) -> list[dict]:
    """库存账龄：0-30 / 31-60 / 61-90 / 90+ 天（按建档时间算，在库且未售）。"""
    today = _now()
    edges = ((0, 30), (31, 60), (61, 90), (91, 100000))
    rows = list(
        session.scalars(
            select(WmsItem).where(WmsItem.status.notin_((STATUS_SOLD, "")))
        ).all()
    )
    out = []
    for lo, hi in edges:
        cnt = 0
        cost = 0.0
        for row in rows:
            if not row.created_at:
                continue
            days = (today - row.created_at).days
            if lo <= days <= hi:
                cnt += int(row.qty or 0) or 1
                cost += float(row.purchase_cost or 0.0) * (int(row.qty or 0) or 1)
        label = f"{lo}-{hi} 天" if hi < 100000 else "90 天以上"
        out.append({"label": label, "qty": cnt, "cost": round(cost, 2)})
    return out


def dormant(session: Session, days: int = 30, limit: int = 20) -> list[WmsItem]:
    """呆滞库存：可售但最近 N 天没有出库流水（该降价清仓的就是这批）。"""
    since = _now() - timedelta(days=int(days))
    moved = select(WmsStockLedger.item_id).where(
        WmsStockLedger.biz_type == LEDGER_OUT,
        WmsStockLedger.operated_at >= since,
    )
    return list(
        session.scalars(
            select(WmsItem)
            .where(
                WmsItem.status == "SELLABLE",
                WmsItem.grade.in_(("S", "A")),
                WmsItem.id.notin_(moved),
            )
            .order_by(WmsItem.created_at.asc())
            .limit(limit)
        ).all()
    )


def item_ledgers(session: Session, item_id: int, limit: int = 50) -> list[WmsStockLedger]:
    return list(
        session.scalars(
            select(WmsStockLedger)
            .where(WmsStockLedger.item_id == item_id)
            .order_by(WmsStockLedger.id.desc())
            .limit(limit)
        ).all()
    )


def negative_balances(session: Session) -> list[WmsStockBalance]:
    """负库存告警：出现负数说明前面有流程走漏了。"""
    return list(
        session.scalars(
            select(WmsStockBalance).where(WmsStockBalance.qty < 0)
        ).all()
    )


def hazard_items(session: Session, limit: int = 50) -> list[WmsItem]:
    """危废台账：安全红线品，禁止充电与流转，只做登记与拍照存档。"""
    return list(
        session.scalars(
            select(WmsItem)
            .where(WmsItem.warehouse_code == WH_HAZARD)
            .order_by(WmsItem.updated_at.desc())
            .limit(limit)
        ).all()
    )
