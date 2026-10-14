"""Signed read-only product API for xh-fish."""
from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.config import get_settings
from src.deps import get_db
from src.goods.catalog import category_path
from src.models import SkuMapping, SupplierItem
from src.schemas import SupplierItemOut

router = APIRouter(prefix="/openapi/v1/xh-fish/products", tags=["openapi"])
_MAX_CLOCK_SKEW_SECONDS = 300


def _verify_signature(request: Request, client_id: str | None, timestamp: str | None,
                      nonce: str | None, signature: str | None, secret: str,
                      raw_body: bytes = b"", signed_path: str | None = None) -> None:
    if not secret:
        raise HTTPException(status_code=503, detail="product API is not configured")
    if not all((client_id, timestamp, nonce, signature)) or client_id != "xh-fish":
        raise HTTPException(status_code=401, detail="invalid service credentials")
    if len(nonce) > 128 or len(signature) != 64:
        raise HTTPException(status_code=401, detail="invalid service credentials")
    try:
        issued_at = int(timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="invalid service credentials") from None
    if abs(int(time.time()) - issued_at) > _MAX_CLOCK_SKEW_SECONDS:
        raise HTTPException(status_code=401, detail="expired request")
    digest = hashlib.sha256(raw_body).hexdigest()
    signed_path = signed_path or request.url.path
    if signed_path == request.url.path and request.url.query:
        signed_path += f"?{request.url.query}"
    message = "\n".join((request.method.upper(), signed_path, timestamp, nonce, digest))
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise HTTPException(status_code=401, detail="invalid service credentials")


def _authorized(request: Request, x_client_id: str | None, x_timestamp: str | None,
                x_nonce: str | None, x_signature: str | None, raw_body: bytes = b"") -> None:
    signed_path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    _verify_signature(request, x_client_id, x_timestamp, x_nonce, x_signature,
                      get_settings().xianmou_identity_sync_secret, raw_body, signed_path)


def _product_out(item: SupplierItem, db: Session) -> dict[str, Any]:
    data = SupplierItemOut.model_validate(item).model_dump(mode="json")
    raw_skus = (item.sku_list or {}).get("skus") or []
    sku_ids = [str(row.get("sku_id") or "") for row in raw_skus if isinstance(row, dict)]
    mappings = db.scalars(select(SkuMapping).where(
        SkuMapping.item_id == item.item_id, SkuMapping.ali_sku_id.in_(sku_ids or [""])
    )).all()
    reserved = {row.ali_sku_id: int(row.reserved_stock or 0) for row in mappings}
    skus = []
    for index, row in enumerate(raw_skus):
        if not isinstance(row, dict):
            continue
        sku_id = str(row.get("sku_id") or f"{item.item_id}::sku_{index + 1}")
        stock = int(row.get("stock") or 0)
        available = max(0, stock - reserved.get(sku_id, 0))
        skus.append({
            "id": index + 1,
            "sourceSkuId": sku_id,
            "code": str(row.get("code") or ""),
            "spec": str(row.get("spec_text") or row.get("spec") or "默认规格"),
            "price": str(row.get("price") or item.sale_price or item.fish_price or Decimal("0")),
            "fishPrice": str(row.get("fish_price") or item.fish_price or item.cost_price or Decimal("0")),
            "stock": stock,
            "available": available,
        })
    images = list(item.images or [])
    details = list(item.detail_images or [])
    if item.main_image and item.main_image not in images:
        images.insert(0, item.main_image)
    image_materials = [{"url": url, "tags": ["主图"] if index == 0 else []}
                       for index, url in enumerate(images)]
    image_materials.extend({"url": url, "tags": ["详情图"]} for url in details if url)
    data.update({
        "id": item.id,
        "sourceItemId": item.item_id,
        "version": int(item.updated_at.timestamp()) if item.updated_at else int(item.id),
        "code": item.item_code or item.item_id,
        "category": category_path().get(item.category_id or "", item.category_id or ""),
        "supplierId": item.supplier_id or "",
        "description": item.detail_html or "",
        "sellingPoints": item.selling_points or "",
        "brand": item.brand or "",
        "mainImage": item.main_image or (images[0] if images else ""),
        "images": images,
        "imageMaterials": {"tags": ["主图", "详情图"], "items": image_materials},
        "fishPrice": str(item.fish_price or item.cost_price or Decimal("0")),
        "salePrice": str(item.sale_price or Decimal("0")),
        "marketPrice": str(item.market_price or Decimal("0")),
        "deliveryDays": int(item.delivery_days or 2),
        "afterSale": item.after_sale or "",
        "shippingProvince": "",
        "shippingProvinceCode": "",
        "shippingCity": "",
        "shippingCityCode": "",
        "skus": skus,
    })
    return data


def _query_items(db: Session, q: str, category: str):
    stmt = select(SupplierItem).where(
        SupplierItem.audit_status == "approved", SupplierItem.is_listed.is_(True)
    )
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(SupplierItem.title.like(like), SupplierItem.item_id.like(like)))
    if category:
        category_text = f"%{category.strip()}%"
        stmt = stmt.where(or_(
            SupplierItem.category_id == category,
            SupplierItem.category_l3 == category,
            SupplierItem.category_l3.like(category_text),
        ))
    return stmt


@router.get("")
async def list_products(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, alias="pageSize", ge=1, le=100),
    q: str = Query(""),
    category: str = Query(""),
    db: Session = Depends(get_db),
    x_client_id: str | None = Header(default=None),
    x_timestamp: str | None = Header(default=None),
    x_nonce: str | None = Header(default=None),
    x_signature: str | None = Header(default=None),
):
    _authorized(request, x_client_id, x_timestamp, x_nonce, x_signature)
    stmt = _query_items(db, q, category)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(SupplierItem.last_synced_at.desc().nullslast(), SupplierItem.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [_product_out(item, db) for item in rows], "total": total,
            "page": page, "pageSize": page_size}


@router.get("/{item_id}")
async def get_product(
    item_id: str,
    request: Request,
    db: Session = Depends(get_db),
    x_client_id: str | None = Header(default=None),
    x_timestamp: str | None = Header(default=None),
    x_nonce: str | None = Header(default=None),
    x_signature: str | None = Header(default=None),
):
    _authorized(request, x_client_id, x_timestamp, x_nonce, x_signature)
    item = db.scalar(select(SupplierItem).where(
        SupplierItem.item_id == item_id,
        SupplierItem.audit_status == "approved",
        SupplierItem.is_listed.is_(True),
    ))
    if item is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    return _product_out(item, db)
