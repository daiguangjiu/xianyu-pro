"""Logistics 路由。

- POST /callback              接物流回写 webhook（人工推送 tracking 号的接入点）
- GET  /{downstream_order_id} 查物流

（主动拉取 1688 物流的 /sync/{ali_order_id} 已于 2026-10-13 下线——
该能力从未申请下来，路由只会报 503。）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.deps import get_db
from src.logistics.service import (
    LogisticsError,
    OrderNotFound,
    get_logistics,
    receive_callback,
)
from src.schemas import LogisticsCallbackIn, LogisticsCallbackOut

router = APIRouter()


@router.post(
    "/callback",
    response_model=LogisticsCallbackOut,
    summary="接物流回写 webhook（人工推送 tracking 号）",
)
async def callback_route(
    payload: LogisticsCallbackIn, db: Session = Depends(get_db)
):
    """接物流 webhook；payload 中 shipping_no 为必填。

    DSO 状态推进：
      pushed → shipped → completed
    PO 状态推进：
      paid → shipped → completed

    失败 → ExceptionLog(type='logistics.*')
    """
    try:
        snap = receive_callback(
            db,
            payload.model_dump(),
            source=payload.source or "manual",
        )
    except OrderNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LogisticsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return LogisticsCallbackOut(
        shipping_no=snap.shipping_no,
        carrier=snap.carrier,
        status=snap.status,
        received_at=snap.received_at,
    )


@router.get(
    "/{downstream_order_id}",
    summary="查 DSO 物流信息",
)
async def get_route(downstream_order_id: str, db: Session = Depends(get_db)):
    info = get_logistics(db, downstream_order_id)
    if info is None:
        raise HTTPException(status_code=404, detail="no logistics record")
    return info
