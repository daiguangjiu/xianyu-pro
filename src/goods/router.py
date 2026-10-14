"""Goods 路由：商品列表 / 自营上架 / 商品审核。

路径约定（对齐「供应链 → 商品管理」页面）：
    GET    /goods/items                   商品列表（支持来源 / 审核状态 / 关键词过滤）
    POST   /goods/items                   上架自营商品
    GET    /goods/items/{item_id}         商品详情
    PATCH  /goods/items/{item_id}/audit   单个审核
    POST   /goods/items/batch-audit       批量审核

（1688 同步 /goods/sync 已于 2026-10-13 整体下线。）
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.deps import get_db
from src.goods import service as goods_service
from src.models import SupplierItem
from src.schemas import (
    GoodsAuditResultOut,
    GoodsItemAuditIn,
    GoodsItemBatchAuditIn,
    GoodsItemCreateIn,
    SupplierItemOut,
)

router = APIRouter()


@router.api_route(
    "/unlist-zero-stock",
    methods=["GET", "POST"],
    summary="库存为 0 的货源：立即巡检、自动下架并通知飞书群",
)
async def unlist_zero_stock(
    notify: bool = Query(True, description="是否往飞书群发通知"),
    db: Session = Depends(get_db),
):
    """运维 / 排障用的手动口子（正常由启动与后台线程自动跑）。

    口径见 `src/goods/stock_guard.py`：在架且库存 <= 0 的货源 → 下架 + 群里 @ 人通知；
    幂等（已经下架的不会重复通知），所以想验证效果可以反复调。
    """
    from src.goods import stock_guard

    return stock_guard.unlist_zero_stock(db, notify=notify)


@router.get("/items", response_model=list[SupplierItemOut], summary="列出商品")
async def list_items(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, le=200),
    source_type: str | None = Query(
        None, description="供应链来源：self_operated（自营）/ third_party（供应商）"
    ),
    audit_status: str | None = Query(
        None, description="审核状态：draft / pending / approved / rejected"
    ),
    supplier_id: str | None = Query(None, description="供货商 ID"),
    q: str | None = Query(None, description="标题 / 商品编号模糊搜索"),
    db: Session = Depends(get_db),
):
    """商品列表（分页 + 过滤）。"""
    stmt = select(SupplierItem)
    if source_type:
        stmt = stmt.where(SupplierItem.source_type == source_type)
    if audit_status:
        stmt = stmt.where(SupplierItem.audit_status == audit_status)
    if supplier_id:
        stmt = stmt.where(SupplierItem.supplier_id == supplier_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(SupplierItem.title.like(like), SupplierItem.item_id.like(like))
        )
    stmt = (
        stmt.order_by(SupplierItem.last_synced_at.desc().nullslast())
        .offset(skip)
        .limit(limit)
    )
    rows = db.execute(stmt).scalars().all()
    return [SupplierItemOut.model_validate(r) for r in rows]


@router.post(
    "/items",
    response_model=SupplierItemOut,
    status_code=201,
    summary="上架自营商品",
)
async def create_item(payload: GoodsItemCreateIn, db: Session = Depends(get_db)):
    """创建自营商品（对应 Web「发布商品」表单）。

    `submit_action` 决定初始状态：
    - `publish` → 直接 approved（自营货源不占用审核队列）
    - `audit`   → pending（进入「待审核」）
    - `draft`   → draft（草稿箱）
    """
    item = goods_service.create_item(db, payload)
    return SupplierItemOut.model_validate(item)


@router.post(
    "/items/batch-audit",
    response_model=GoodsAuditResultOut,
    summary="批量审核商品",
)
async def batch_audit_items(
    payload: GoodsItemBatchAuditIn,
    db: Session = Depends(get_db),
):
    result = goods_service.batch_audit(
        db,
        payload.item_ids,
        payload.action,
        note=payload.note,
        by=payload.by,
    )
    return GoodsAuditResultOut(**result)


@router.get("/items/{item_id}", response_model=SupplierItemOut, summary="商品详情")
async def get_item(item_id: str, db: Session = Depends(get_db)):
    item = db.query(SupplierItem).filter(SupplierItem.item_id == item_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail=f"商品不存在：{item_id}")
    return SupplierItemOut.model_validate(item)


@router.patch(
    "/items/{item_id}/audit",
    response_model=SupplierItemOut,
    summary="商品审核（通过 / 驳回）",
)
async def audit_item(
    item_id: str,
    payload: GoodsItemAuditIn,
    db: Session = Depends(get_db),
):
    item = goods_service.audit_item(
        db,
        item_id,
        payload.action,
        note=payload.note,
        by=payload.by,
    )
    if item is None:
        raise HTTPException(status_code=404, detail=f"商品不存在：{item_id}")
    return SupplierItemOut.model_validate(item)
