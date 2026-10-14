"""Order 路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from pydantic import BaseModel

from src.deps import get_db
from src.order.service import (
    OrderAcceptError,
    accept_order,
    get_order_with_pos,
)
from src.schemas import OrderCreateIn, OrderDetailOut, OrderOut, PurchaseOrderOut

router = APIRouter()


class FulfillmentIn(BaseModel):
    """渔枢系统回写的履约结果（发货 / 退款）。"""

    order_state: str = ""
    tracking_no: str = ""
    shipping_company: str = ""
    refund_amount: float = 0


@router.post("/{downstream_order_id}/fulfillment", summary="渔枢系统履约回写")
async def push_fulfillment(downstream_order_id: str, payload: FulfillmentIn, db=Depends(get_db)):
    """渔枢系统采购发货 / 退款之后，把结果推给渔货栈，这边按状态机往下流转。

    合并部署时渔枢系统直接调 `src.order.hub_sync`（同进程，见 app/services/self_sync.py）；
    分端部署时用这个接口，字段一样，幂等（已经是目标状态就跳过）。
    """
    from src.order import hub_sync

    done = hub_sync.sync_from_hub(db, downstream_order_id, payload.model_dump())
    return {"ok": True, "actions": done}


@router.post("", response_model=OrderDetailOut, summary="接收下游订单")
async def create_order(payload: OrderCreateIn, db: Session = Depends(get_db)):
    """接收一笔订单（可多 SKU）：
    - 反查 SkuMapping → 1688 item_id / sku_id
    - 实时库存复核 + 预占
    - 写 DownstreamOrder（每 SKU 一行）
    - 派发 push_order → PurchaseOrder（init/pending_1688_confirm）
    - 任何环节失败 → ExceptionLog 落库
    """
    try:
        result = accept_order(db, payload)
    except OrderAcceptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return OrderDetailOut(
        group_downstream_order_id=payload.downstream_order_id,
        channel=payload.channel,
        downstream_orders=[OrderOut.model_validate(d) for d in result.downstream_orders],
        purchase_orders=[PurchaseOrderOut.model_validate(p) for p in result.purchase_orders],
        exception_count=len(result.exceptions),
    )


@router.get("/{downstream_order_id}", response_model=OrderDetailOut, summary="查询订单详情（含 PO）")
async def get_order(downstream_order_id: str, db: Session = Depends(get_db)):
    """按 group_id（不带 ::sku# 后缀）查询，返回这一组的所有 DSO + 关联 PO。"""
    # 兼容 : 集团 id / 单 SKU id 都可查
    dsos, pos = get_order_with_pos(db, downstream_order_id)
    if not dsos:
        raise HTTPException(status_code=404, detail="not found")
    return OrderDetailOut(
        group_downstream_order_id=downstream_order_id,
        channel=dsos[0].channel,
        downstream_orders=[OrderOut.model_validate(d) for d in dsos],
        purchase_orders=[PurchaseOrderOut.model_validate(p) for p in pos],
        exception_count=0,
    )