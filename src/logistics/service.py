"""物流服务。

接口：
    receive_callback(db, payload, *, source='manual')  # 接物流回写 webhook（人工推送 tracking 号的接入点）

（主动拉取 1688 物流的 sync_logistics 已于 2026-10-13 下线——
能力从未申请下来。）

物流状态变更：
    DSO.status: pushed → shipped → completed
    DSO.raw_json['logistics'] = {shipping_no, carrier, status, received_at, ...}
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.models import DownstreamOrder, ExceptionLog, PurchaseOrder
from app.timeutil import to_iso, utc_now

logger = logging.getLogger(__name__)


# =====================================================================
# 异常
# =====================================================================
class LogisticsError(Exception):
    pass


class OrderNotFound(LogisticsError):
    pass


# =====================================================================
# DTO
# =====================================================================
@dataclass
class LogisticsSnapshot:
    """写回 DSO 的物流快照。"""

    shipping_no: str
    carrier: str = ""
    status: str = "shipped"
    received_at: datetime = field(default_factory=lambda: utc_now())
    source: str = "manual"
    extra: dict = field(default_factory=dict)


# =====================================================================
# 接收 callback
# =====================================================================
def receive_callback(
    db: Session,
    payload: dict[str, Any],
    *,
    source: str = "manual",
) -> LogisticsSnapshot:
    """接物流回写 webhook（现阶段作为人工推送 tracking 号的接入点）。

    入参 payload 期望包含：
      - downstream_order_id 或 ali_order_id：唯一定位 DSO
      - shipping_no（必填）
      - carrier（可选；默认 ''）
      - status（默认 'shipped'）
      - extra（任意扩展字段，会一并写 DSO.raw_json['logistics']）

    返回：
        LogisticsSnapshot 写库后回填

    失败处理：
      - 找不到 DSO → ExceptionLog(type='logistics.dso_not_found')，**不**抛
      - payload 缺 shipping_no → ExceptionLog(type='logistics.payload_invalid')
    """
    shipping_no = (payload.get("shipping_no") or "").strip()
    carrier = (payload.get("carrier") or "").strip()
    status = (payload.get("status") or "shipped").strip()
    extra = dict(payload.get("extra") or {})

    dso = _resolve_dso(db, payload)
    if dso is None:
        _log_exception(
            db,
            type_="logistics.dso_not_found",
            payload=payload,
        )
        db.commit()
        raise OrderNotFound(
            f"DSO not found via identifiers={payload.get('downstream_order_id')!r} "
            f"or ali_order_id={payload.get('ali_order_id')!r}"
        )

    if not shipping_no:
        _log_exception(
            db,
            type_="logistics.payload_invalid",
            payload={"dso_id": dso.id, **payload},
        )
        db.commit()
        raise LogisticsError("shipping_no missing")

    snapshot = LogisticsSnapshot(
        shipping_no=shipping_no,
        carrier=carrier,
        status=status,
        source=source,
        extra=extra,
    )
    _write_to_dso(db, dso, snapshot)

    # 同步推一个状态回写：pushed → shipped → completed
    if status == "shipped":
        dso.status = "shipped"
    elif status in ("delivered", "signed", "completed"):
        dso.status = "completed"
    # 推进 PO 状态
    pos = (
        db.query(PurchaseOrder)
        .filter(PurchaseOrder.downstream_order_id == dso.downstream_order_id)
        .all()
    )
    for po in pos:
        if po.status in ("paid",):
            po.status = "shipped"
        if status in ("delivered", "signed", "completed"):
            po.status = "completed"

    db.commit()
    logger.info(
        "receive_callback dso=%s shipping_no=%s carrier=%s status=%s",
        dso.downstream_order_id,
        shipping_no,
        carrier,
        status,
    )
    return snapshot


# =====================================================================
# 查询
# =====================================================================
def get_logistics(db: Session, downstream_order_id: str) -> dict | None:
    """查 DSO.raw_json['logistics']；没有 → None。"""
    dso = (
        db.query(DownstreamOrder)
        .filter(DownstreamOrder.downstream_order_id == downstream_order_id)
        .first()
    )
    if dso is None:
        return None
    return (dso.raw_json or {}).get("logistics")


# =====================================================================
# 工具
# =====================================================================
def _resolve_dso(db: Session, payload: dict) -> DownstreamOrder | None:
    """按多种标识查 DSO：downstream_order_id / ali_order_id。"""
    dso_id = payload.get("downstream_order_id")
    if dso_id:
        dso = (
            db.query(DownstreamOrder)
            .filter(DownstreamOrder.downstream_order_id == dso_id)
            .first()
        )
        if dso:
            return dso
    ali_id = payload.get("ali_order_id")
    if ali_id:
        po = (
            db.query(PurchaseOrder)
            .filter(PurchaseOrder.ali_order_id == ali_id)
            .first()
        )
        if po:
            return (
                db.query(DownstreamOrder)
                .filter(
                    DownstreamOrder.downstream_order_id == po.downstream_order_id
                )
                .first()
            )
    return None


def _write_to_dso(
    db: Session, dso: DownstreamOrder, snapshot: LogisticsSnapshot
) -> None:
    raw = dict(dso.raw_json or {})
    raw["logistics"] = {
        "shipping_no": snapshot.shipping_no,
        "carrier": snapshot.carrier,
        "status": snapshot.status,
        "source": snapshot.source,
        "received_at": snapshot.received_at.isoformat(),
        "extra": snapshot.extra,
    }
    dso.raw_json = raw
    db.flush()


def _log_exception(db: Session, *, type_: str, payload: dict) -> ExceptionLog:
    row = ExceptionLog(
        type=type_,
        payload=payload,
        error=payload.get("error") or "",
        retry_count=0,
        resolved=False,
    )
    db.add(row)
    db.flush()
    logger.warning("exception_log type=%s payload=%s", type_, payload)
    return row


__all__ = [
    "LogisticsSnapshot",
    "receive_callback",
    "sync_logistics",
    "get_logistics",
    "LogisticsError",
    "OrderNotFound",
]