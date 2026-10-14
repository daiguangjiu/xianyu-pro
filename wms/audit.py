"""审计日志：改档 / 改价 / 盘盈盘亏 / 报修撤单 / 危废处理，关键操作全留痕。

只增不改（跟库存流水一个脾气）：日志本身不提供改 / 删接口。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .timeutil import now as _now
from .models import WmsAuditLog

# 需要进审计的动作（页面提示与日志列表共用这套文案）
ACTION_LABELS = {
    "grade": "改档",
    "price": "改价",
    "status": "状态流转",
    "inbound": "入库建档",
    "label": "标签打印",
    "reprint": "标签补打",
    "outbound": "出库",
    "repair": "报修工单",
    "repair_cancel": "报修撤单",
    "parts": "拆机件残值",
    "stocktake": "盘点",
    "adjust": "盘盈盘亏调整",
    "hazard": "危废处理",
}


def log(
    session: Session,
    entity: str,
    entity_id,
    action: str,
    before: str = "",
    after: str = "",
    operator: str = "",
    ip: str = "",
    note: str = "",
) -> WmsAuditLog:
    row = WmsAuditLog(
        entity=str(entity or ""),
        entity_id=str(entity_id or ""),
        action=str(action or ""),
        before=str(before or ""),
        after=str(after or ""),
        operator=str(operator or ""),
        ip=str(ip or ""),
        note=str(note or ""),
        created_at=_now(),
    )
    session.add(row)
    session.flush()
    return row


def action_label(value: str) -> str:
    return ACTION_LABELS.get(str(value or "").strip().lower(), str(value or "—") or "—")


def recent(session: Session, limit: int = 50, entity: str = "") -> list[WmsAuditLog]:
    stmt = select(WmsAuditLog)
    if entity:
        stmt = stmt.where(WmsAuditLog.entity == entity)
    return list(
        session.scalars(stmt.order_by(WmsAuditLog.id.desc()).limit(limit)).all()
    )


def of_entity(session: Session, entity: str, entity_id, limit: int = 30) -> list[WmsAuditLog]:
    """某一条记录的流水（如某台货品的改档 / 改价历史）。"""
    return list(
        session.scalars(
            select(WmsAuditLog)
            .where(WmsAuditLog.entity == entity, WmsAuditLog.entity_id == str(entity_id or ""))
            .order_by(WmsAuditLog.id.desc())
            .limit(limit)
        ).all()
    )
