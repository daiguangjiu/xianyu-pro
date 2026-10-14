"""拆机件与残值（D 档）：**D 档无残值明细不允许关闭工单**。

D 档当废品扔掉，主板 / 电池残值白白流失 —— 这里是止损的地方。
残值 = Σ 配件市场价 × 0.8（打包出）；回收率 = 实际变现金额 / 理论残值。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .timeutil import now as _now
from .models import (
    GRADE_D,
    PART_OTHER,
    STATUS_PARTS,
    WH_PARTS,
    WmsItem,
    WmsPartsItem,
    part_label,
)
from . import audit
from . import ledger
from . import state

# 打包出：整批出给回收商的价格折扣
PACK_FACTOR = 0.8


def add(
    session: Session,
    item: WmsItem,
    *,
    part_type: str,
    part_name: str = "",
    est_value: float = 0.0,
    operator: str = "",
    note: str = "",
) -> WmsPartsItem:
    """逐件估值：主板 / 屏 / 电池 / 摄像头 / 尾插 / 喇叭…"""
    if item is None:
        raise ValueError("货品不存在")
    ptype = str(part_type or PART_OTHER).lower()
    try:
        value = float(est_value or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if value < 0:
        raise ValueError("残值不能为负")
    row = WmsPartsItem(
        item_id=item.id,
        barcode=item.barcode,
        category=item.category,
        part_type=ptype,
        part_name=str(part_name or part_label(ptype)).strip(),
        est_value=value,
        note=str(note or ""),
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(row)
    session.flush()
    # 残值 = Σ 配件估值 × 打包系数
    item.residual_value = round(estimate_total(session, item.id) * PACK_FACTOR, 2)
    item.updated_at = _now()
    audit.log(session, "item", item.id, "parts",
              after=f"{part_label(ptype)} ¥{value:.2f}", operator=operator)
    session.commit()
    return row


def of_item(session: Session, item_id) -> list[WmsPartsItem]:
    return list(
        session.scalars(
            select(WmsPartsItem)
            .where(WmsPartsItem.item_id == item_id)
            .order_by(WmsPartsItem.id.asc())
        ).all()
    )


def estimate_total(session: Session, item_id) -> float:
    total = session.scalar(
        select(func.coalesce(func.sum(WmsPartsItem.est_value), 0.0)).where(
            WmsPartsItem.item_id == item_id
        )
    )
    return round(float(total or 0.0), 2)


def require_parts(session: Session, item: WmsItem) -> bool:
    """有没有残值明细（D 档关工单 / 拆机件出库的硬门槛）。"""
    return bool(of_item(session, item.id))


def to_parts(session: Session, item: WmsItem, operator: str = "", remark: str = "") -> WmsItem:
    """判废转拆机件：D 档入拆机件仓。"""
    if str(item.grade or "").upper() != GRADE_D:
        raise ValueError(f"只有 D 档能转拆机件（当前 {item.grade}）")
    warehouse, status = state.route_of(GRADE_D)
    ledger.apply(
        session, item,
        biz_type="SCRAP",
        biz_id=str(item.id),
        to_warehouse=warehouse or WH_PARTS,
        to_status=status or STATUS_PARTS,
        operator=operator,
        remark=remark or "判废转拆机件",
        idempotent_key=f"SCRAP:{item.id}",
        check_transition=False,
    )
    audit.log(session, "item", item.id, "parts", after="转拆机件", operator=operator)
    session.commit()
    return item


def close(session: Session, item: WmsItem, operator: str = "") -> float:
    """关闭 D 档工单：**无残值明细不允许关闭**（防止整机当废品扔掉）。"""
    rows = of_item(session, item.id)
    if not rows:
        raise ValueError(
            f"{item.display_code} 还没有残值明细，不允许关闭工单 —— "
            "至少要估主板 / 屏 / 电池其中一项"
        )
    item.residual_value = round(estimate_total(session, item.id) * PACK_FACTOR, 2)
    item.updated_at = _now()
    audit.log(session, "item", item.id, "parts",
              after=f"残值 ¥{item.residual_value:.2f}", operator=operator)
    session.commit()
    return float(item.residual_value or 0.0)


def sell(session: Session, part_id: int, price: float, outbound_id: int | None = None,
         operator: str = "") -> WmsPartsItem:
    """拆机件卖出：记实际售价，用于回写残值回收率。"""
    row = session.get(WmsPartsItem, part_id)
    if not row:
        raise ValueError("拆机件不存在")
    row.actual_sold_price = round(float(price or 0.0), 2)
    row.outbound_id = outbound_id
    row.sold_at = _now()
    row.updated_at = _now()
    audit.log(session, "item", row.item_id, "parts",
              after=f"{row.part_name} 售出 ¥{row.actual_sold_price:.2f}", operator=operator)
    session.commit()
    return row


def pool(session: Session, limit: int = 100) -> list[WmsItem]:
    """拆机件仓：待拆 / 已拆的 D 档货。"""
    return list(
        session.scalars(
            select(WmsItem)
            .where(WmsItem.status == STATUS_PARTS)
            .order_by(WmsItem.updated_at.desc())
            .limit(limit)
        ).all()
    )


def recovery_rate(session: Session) -> dict:
    """残值回收率 = 实际变现金额 / 理论残值（管理层最该盯的两个数之一）。"""
    rows = list(session.scalars(select(WmsPartsItem)).all())
    est = sum(float(r.est_value or 0.0) for r in rows)
    sold = sum(float(r.actual_sold_price or 0.0) for r in rows)
    return {
        "parts": len(rows),
        "sold": sum(1 for r in rows if r.actual_sold_price),
        "est": round(est, 2),
        "actual": round(sold, 2),
        "rate": round(sold / est, 4) if est else 0.0,
    }
