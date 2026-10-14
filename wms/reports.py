"""报表中心：管理层视角的 12 张报表（方案 10.2）。

两个最该盯的数：**残值回收率**（D 档有没有被当废品扔掉）与
**报修超时单数**（在途货有多少悬在别人手里）—— 这两个数一恶化，现金流立刻出问题。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    GRADE_CHOICES,
    GRADE_LABELS,
    STATUS_OUT_REPAIR,
    STATUS_SOLD,
    WmsBatch,
    WmsItem,
)
from . import inspection
from . import ledger
from . import parts
from . import repairs
from . import state


def grade_distribution(session: Session) -> list[dict]:
    """分级分布：S / A / B / C / D 占比（检测完的货里各档多少）。"""
    rows = list(
        session.scalars(
            select(WmsItem).where(WmsItem.grade != "W", WmsItem.status != "")
        ).all()
    )
    total = len(rows) or 1
    out = []
    for grade in GRADE_CHOICES:
        if grade == "W":
            continue
        cnt = sum(1 for r in rows if str(r.grade or "").upper() == grade)
        out.append(
            {
                "grade": grade,
                "label": GRADE_LABELS.get(grade, grade),
                "qty": cnt,
                "rate": round(cnt / total, 4),
            }
        )
    return out


def by_batch(session: Session, limit: int = 10) -> list[dict]:
    """按批次对比分级分布：哪一批的货质量差，一眼看得出来。"""
    batches = list(
        session.scalars(select(WmsBatch).order_by(WmsBatch.id.desc()).limit(limit)).all()
    )
    out = []
    for batch in batches:
        rows = list(
            session.scalars(select(WmsItem).where(WmsItem.batch_id == batch.id)).all()
        )
        if not rows:
            continue
        grades: dict[str, int] = {}
        for row in rows:
            key = str(row.grade or "W").upper()
            grades[key] = grades.get(key, 0) + 1
        out.append(
            {
                "batch_no": batch.batch_no,
                "source": batch.source_type,
                "supplier": batch.supplier_name,
                "total": len(rows),
                "grades": grades,
                "bad_rate": round(
                    (grades.get("C", 0) + grades.get("D", 0)) / len(rows), 4
                ),
            }
        )
    return out


def inventory_value(session: Session) -> dict:
    """库存货值：成本口径与售价口径分开列（不要合并成一个数）。"""
    rows = list(
        session.scalars(
            select(WmsItem).where(WmsItem.status.notin_((STATUS_SOLD, "")))
        ).all()
    )
    cost = sum(float(r.purchase_cost or 0.0) for r in rows)
    price = sum(float(r.selling_price or 0.0) for r in rows)
    residual = sum(float(r.residual_value or 0.0) for r in rows)
    return {
        "qty": len(rows),
        "cost": round(cost, 2),
        "price": round(price, 2),
        "residual": round(residual, 2),
        # 在途（报修在外）的钱是垫付 + 货值，单独列
        "in_transit_qty": sum(1 for r in rows if r.status == STATUS_OUT_REPAIR),
    }


def sold_profit(session: Session, limit: int = 20) -> dict:
    """毛利分析：按已售出的出库明细算（售价 − 成本）。"""
    from .models import WmsOutboundItem

    lines = list(
        session.scalars(
            select(WmsOutboundItem).where(WmsOutboundItem.price > 0)
        ).all()
    )
    revenue = sum(float(l.price or 0.0) for l in lines)
    cost = sum(float(l.cost or 0.0) for l in lines)
    return {
        "qty": len(lines),
        "revenue": round(revenue, 2),
        "cost": round(cost, 2),
        "profit": round(revenue - cost, 2),
        "margin": round((revenue - cost) / revenue, 4) if revenue else 0.0,
    }


def dashboard(session: Session) -> dict:
    """工作台一屏：库存口径 + 待办 + 两个最该盯的数。"""
    summary = ledger.summary(session)
    return {
        "summary": summary,
        "on_hand": summary["on_hand"],
        "sellable": summary["sellable"],
        "pending": summary["buckets"]["pending"]["qty"],
        "repair_timeout": len(repairs.timeouts(session, limit=100)),
        "repair_stats": repairs.stats(session),
        "recovery": parts.recovery_rate(session),
        "recheck": len(inspection.recheck_pool(session, limit=100)),
        "negative": len(ledger.negative_balances(session)),
        "value": inventory_value(session),
    }


def overview(session: Session) -> dict:
    """报表中心全量数据（分级分布 / 账龄 / 呆滞 / 报修 / 残值 / 产能）。"""
    return {
        "grades": grade_distribution(session),
        "batches": by_batch(session),
        "aging": ledger.aging(session),
        "dormant": ledger.dormant(session, days=30, limit=20),
        "repair": repairs.stats(session),
        "repair_timeouts": repairs.timeouts(session, limit=20),
        "recovery": parts.recovery_rate(session),
        "testers": inspection.tester_stats(session),
        "value": inventory_value(session),
        "profit": sold_profit(session),
        "negative": ledger.negative_balances(session),
    }


def pending_count(session: Session) -> int:
    """菜单徽标：待检货品数（检测员今天要干多少活）—— 按台数（一台可带 N 台）。"""
    return int(
        session.scalar(
            select(func.coalesce(func.sum(WmsItem.qty), 0)).where(
                WmsItem.status == state.STATUS_PENDING
            )
        ) or 0
    )


def timeout_count(session: Session) -> int:
    """菜单徽标：超时未回的报修单（钱垫着、货在别人手里的）。"""
    return len(repairs.timeouts(session, limit=200))
