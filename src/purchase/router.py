"""Purchase 路由。

- POST /push        — 生成采购草稿
- POST /{id}/confirm-paid — 人工付款后回写
- POST /{id}/mark-failed — 运营手动标失败
- GET  /            — 列表
- GET  /orders/{id} — 详情
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.deps import get_db
from src.purchase.service import (
    OrderNotFound,
    PurchasePushError,
    confirm_paid,
    list_purchase_orders,
    mark_failed,
    push_order,
)
from src.schemas import (
    PurchaseConfirmPaidIn,
    PurchaseOrderOut,
    PurchasePushRequest,
    PurchasePushResponse,
)

router = APIRouter()


@router.post("/push", response_model=PurchasePushResponse, summary="生成采购草稿")
async def push(req: PurchasePushRequest, db: Session = Depends(get_db)):
    """生成采购草稿：

    - status=init（等人工确认付款）；正向采购走订单页「推给渔枢系统采购」
    - 幂等：同一 DSO+SKU 第二次调用 → 不动重复 PO 直返
    """
    try:
        pos = await push_order(db, req.downstream_order_id)
    except OrderNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PurchasePushError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    pos_out = [PurchaseOrderOut.model_validate(p) for p in pos]
    if not pos:
        return PurchasePushResponse(
            ali_order_id=None,
            status="exception",
            message="no PO created; see ExceptionLog",
            purchase_orders=[],
        )
    first = pos[0]
    return PurchasePushResponse(
        ali_order_id=first.ali_order_id,
        status=first.status,
        message=(
            "采购草稿已生成，请人工确认付款"
            if first.status == "init"
            else None
        ),
        purchase_orders=pos_out,
    )


@router.post(
    "/{purchase_order_id}/confirm-paid",
    response_model=PurchaseOrderOut,
    summary="人工付款后回写 status=paid",
)
async def confirm_paid_route(
    purchase_order_id: int,
    payload: PurchaseConfirmPaidIn | None = None,
    db: Session = Depends(get_db),
):
    try:
        po = confirm_paid(
            db,
            purchase_order_id,
            by=(payload.by if payload else "human"),
            note=(payload.note if payload else None),
        )
    except OrderNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PurchasePushError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PurchaseOrderOut.model_validate(po)


@router.post(
    "/{purchase_order_id}/mark-failed",
    response_model=PurchaseOrderOut,
    summary="运营标推单失败（写 ExceptionLog）",
)
async def mark_failed_route(
    purchase_order_id: int,
    payload: PurchaseConfirmPaidIn,
    db: Session = Depends(get_db),
):
    try:
        po = mark_failed(db, purchase_order_id, reason=payload.note or "manual_mark_failed")
    except OrderNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PurchaseOrderOut.model_validate(po)


@router.get("", response_model=list[PurchaseOrderOut], summary="推单列表")
async def list_route(
    status: str | None = None,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    rows = list_purchase_orders(db, status=status, skip=skip, limit=limit)
    return [PurchaseOrderOut.model_validate(p) for p in rows]


@router.get("/orders/{purchase_order_id}", response_model=PurchaseOrderOut, summary="推单详情")
async def get_one_route(purchase_order_id: int, db: Session = Depends(get_db)):
    from src.models import PurchaseOrder

    row = db.get(PurchaseOrder, purchase_order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return PurchaseOrderOut.model_validate(row)