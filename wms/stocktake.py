"""盘点与差异处理。

两条红线（方案 4.4）：
  ① 盘盈盘亏**必须生成调整流水**并附原因说明，**不允许无单据调整**；
  ② 单笔差异 > 500 元或差异率 > 0.5%，**自动升级主管审批**。

扫码盘点：扫一个记一个，**未扫到的不自动算盘亏**（防误判，先列出来给主管看）。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .timeutil import now as _now
from .models import (
    STOCKTAKE_APPROVED,
    STOCKTAKE_DIFF_AMOUNT_LIMIT,
    STOCKTAKE_DIFF_RATE_LIMIT,
    STOCKTAKE_DONE,
    STOCKTAKE_DRAFT,
    STOCKTAKE_SCOPE_LABELS,
    WmsItem,
    WmsStocktake,
    WmsStocktakeLine,
)
from . import audit
from . import intake
from . import ledger


def next_take_no(session: Session, at=None) -> str:
    moment = at or _now()
    prefix = "PD" + moment.strftime("%y%m%d")
    cnt = int(
        session.scalar(
            select(func.count()).select_from(WmsStocktake).where(
                WmsStocktake.take_no.like(f"{prefix}%")
            )
        ) or 0
    )
    return f"{prefix}{cnt + 1:03d}"


def _plan_items(session: Session, scope: str, warehouse: str, grade: str,
                batch_id: int | None) -> list[WmsItem]:
    stmt = select(WmsItem).where(WmsItem.status != "", WmsItem.status != "SOLD")
    if warehouse:
        stmt = stmt.where(WmsItem.warehouse_code == warehouse)
    if grade:
        stmt = stmt.where(WmsItem.grade == grade)
    if batch_id:
        stmt = stmt.where(WmsItem.batch_id == batch_id)
    rows = list(session.scalars(stmt.order_by(WmsItem.id.asc())).all())
    if scope == "value":
        # 专项盘点：高价值货（> ¥3000）
        rows = [r for r in rows if float(r.base_price or 0) > 3000]
    return rows


def create(session: Session, data, operator: str = "") -> WmsStocktake:
    """开盘点单：按范围把账面货品铺成明细行（found=False，等扫码）。"""
    scope = str(data.get("scope") or "cycle").lower()
    if scope not in STOCKTAKE_SCOPE_LABELS:
        raise ValueError("盘点类型只支持：循环 / 批次 / 全盘 / 专项")
    warehouse = str(data.get("warehouse_code") or "").upper()
    grade = str(data.get("grade") or "").upper()
    batch_id = int(data.get("batch_id") or 0) or None
    if scope == "batch" and not batch_id:
        raise ValueError("批次盘点要选批次")

    items = _plan_items(session, scope, warehouse, grade, batch_id)
    if not items:
        raise ValueError("这个范围内没有货品，换一个范围再开")

    row = WmsStocktake(
        take_no=next_take_no(session),
        scope=scope,
        warehouse_code=warehouse,
        grade=grade,
        batch_id=batch_id,
        plan_qty=len(items),
        actual_qty=0,
        diff_qty=0,
        diff_rate=0.0,
        diff_amount=0.0,
        status=STOCKTAKE_DRAFT,
        operator=str(operator or ""),
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(row)
    session.flush()
    for item in items:
        session.add(
            WmsStocktakeLine(
                take_id=row.id,
                item_id=item.id,
                barcode=item.barcode,
                grade=str(item.grade or "").upper(),
                status=str(item.status or ""),
                book_warehouse=str(item.warehouse_code or ""),
                found=False,
                created_at=_now(),
            )
        )
    session.flush()
    audit.log(session, "stocktake", row.id, "stocktake",
              after=f"开单 {row.take_no}（{len(items)} 件）", operator=operator)
    session.commit()
    return row


def scan(session: Session, take_id: int, barcode: str, operator: str = "") -> WmsStocktakeLine:
    """扫码盘点：扫一个记一个（扫到不在本单的货会提示，不静默忽略）。"""
    take = session.get(WmsStocktake, take_id)
    if not take:
        raise ValueError("盘点单不存在")
    if str(take.status or "").lower() != STOCKTAKE_DRAFT:
        raise ValueError("这张盘点单已经结束了")
    item = intake.find_item(session, barcode)
    if not item:
        raise ValueError(f"未知条码 {barcode}（不在系统里）")
    line = session.scalar(
        select(WmsStocktakeLine).where(
            WmsStocktakeLine.take_id == take_id,
            WmsStocktakeLine.item_id == item.id,
        )
    )
    if not line:
        raise ValueError(
            f"{item.display_code} 不在本单范围内"
            f"（{take.warehouse_code or '全部仓'} / {take.grade or '全部等级'}）"
        )
    if line.found:
        raise ValueError(f"{item.display_code} 已经盘过了")
    line.found = True
    line.found_warehouse = str(item.warehouse_code or "")
    line.scanned_at = _now()
    session.commit()
    return line


def finish(session: Session, take_id: int, operator: str = "") -> WmsStocktake:
    """结束盘点：算出差异（未扫到的列为盘亏候选，等审批）。"""
    take = session.get(WmsStocktake, take_id)
    if not take:
        raise ValueError("盘点单不存在")
    lines = lines_of(session, take_id)
    found = [line for line in lines if line.found]
    missing = [line for line in lines if not line.found]
    take.actual_qty = len(found)
    take.diff_qty = len(found) - int(take.plan_qty or 0)
    take.diff_rate = round(abs(take.diff_qty) / take.plan_qty, 6) if take.plan_qty else 0.0
    missing_ids = [line.item_id for line in missing if line.item_id]
    amount = 0.0
    if missing_ids:
        amount = float(
            session.scalar(
                select(func.coalesce(func.sum(WmsItem.purchase_cost), 0.0)).where(
                    WmsItem.id.in_(missing_ids)
                )
            ) or 0.0
        )
    take.diff_amount = round(amount, 2)
    # 差异率 > 0.5% 或单笔 > ¥500：自动升级主管核查
    take.need_upgrade = bool(
        take.diff_rate > STOCKTAKE_DIFF_RATE_LIMIT
        or abs(amount) > STOCKTAKE_DIFF_AMOUNT_LIMIT
    )
    take.status = STOCKTAKE_DONE
    take.finished_at = _now()
    take.updated_at = _now()
    audit.log(session, "stocktake", take.id, "stocktake",
              after=f"盘点结束：差异 {take.diff_qty} 件 / ¥{take.diff_amount:.2f}",
              operator=operator)
    session.commit()
    return take


def approve(session: Session, take_id: int, approver: str = "", note: str = "") -> WmsStocktake:
    """审批：盘亏的货生成调整流水（**不允许无单据直接改库存**）。"""
    take = session.get(WmsStocktake, take_id)
    if not take:
        raise ValueError("盘点单不存在")
    if str(take.status or "").lower() != STOCKTAKE_DONE:
        raise ValueError("只有「待审批」的盘点单能审批")
    if take.need_upgrade and not str(note or "").strip():
        raise ValueError("差异超阈值（>0.5% 或 >¥500），必须填核查说明才能审批")
    lines = lines_of(session, take_id)
    written = 0
    for line in lines:
        if line.found:
            continue
        item = session.get(WmsItem, line.item_id)
        if not item or not str(item.warehouse_code or ""):
            continue
        ledger.write_off(
            session, item,
            biz_id=take.take_no,
            operator=str(approver or take.operator or ""),
            remark=f"盘点 {take.take_no} 盘亏核销（{note or '未扫到，主管已核'}）",
        )
        written += 1
    take.status = STOCKTAKE_APPROVED
    take.approver = str(approver or "")
    take.diff_note = str(note or "")
    take.approved_at = _now()
    take.updated_at = _now()
    audit.log(session, "stocktake", take.id, "adjust",
              after=f"盘亏核销 {written} 件", operator=approver, note=note)
    session.commit()
    return take


def lines_of(session: Session, take_id: int) -> list[WmsStocktakeLine]:
    return list(
        session.scalars(
            select(WmsStocktakeLine)
            .where(WmsStocktakeLine.take_id == take_id)
            .order_by(WmsStocktakeLine.id.asc())
        ).all()
    )


def list_takes(session: Session, status: str = "") -> list[WmsStocktake]:
    stmt = select(WmsStocktake)
    if status:
        stmt = stmt.where(WmsStocktake.status == status)
    return list(session.scalars(stmt.order_by(WmsStocktake.id.desc())).all())
