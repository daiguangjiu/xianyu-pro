"""Goods 业务逻辑：自营商品录入 + 商品审核。

设计约定：
- Web 表单（`/goods/new`）与 API（`POST /api/v1/goods/items`）共用本模块，
  避免同一套校验写两遍。
- 审核状态机：`draft` → `pending` → `approved` / `rejected`（可重复审核）。
- 供应链来源 `source_type`：
    - `self_operated` 自营：本站录入，无 1688 档口，`supplier_id` 为空；
    - `third_party` 供应商：由 `goods/sync.py` 从 1688 同步，需人工审核后放行。
- 自营商品不写 `SkuMapping`（没有 1688 采购链路），`sku_list.skus` 的
  `sku_id` 即本站自有的库存单元。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy.orm import Session

from src.models import SupplierItem
from src.schemas import GoodsItemCreateIn
from app.timeutil import beijing_now, to_iso, utc_now

logger = logging.getLogger(__name__)

# 提交动作 → 审核状态
SUBMIT_ACTION_TO_STATUS: dict[str, str] = {
    "draft": "draft",
    "audit": "pending",
    "publish": "approved",
}

AUDIT_ACTION_TO_STATUS: dict[str, str] = {
    "approve": "approved",
    "reject": "rejected",
}

VALID_AUDIT_STATUSES = ("draft", "pending", "approved", "rejected")


def _now() -> datetime:
    return utc_now()


def generate_item_id() -> str:
    """自营商品编号：SPU + 日期 + 6 位随机（避免与 1688 item_id 混淆）。"""
    # 编号里的日期按北京自然日（不是 UTC 日），否则早上 8 点前生成的编号会差一天
    return f"SPU{beijing_now().strftime('%Y%m%d')}{uuid4().hex[:6].upper()}"


def next_item_code(db: Session, category_id: str | None) -> str:
    """商家编码自动生成，规则与渔枢系统一致：

        YHZ-{一级简拼}-{二级简拼}-{三级简拼}-{YYYYMMDD}-{4 位流水号}

    例：`YHZ-JYDQ-EJYX-20260929-0001`（家用电器 / 耳机音响）。
    类目简拼取类目名拼音首字母；拿不到类目（或独立部署没装渔枢系统那套字表）时退化成
    `YHZ-QT-{日期}-{流水}`，仍然可用。
    """
    from src.goods import catalog  # noqa: PLC0415  避免循环导入

    parts: list[str] = []
    try:
        from app.services.pinyin import initials_of  # noqa: PLC0415  只有渔枢系统侧有这张离线字表

        path = catalog.category_path().get(category_id or "", "")
        parts = [initials_of(p) for p in path.split("/") if p.strip()]
    except Exception:  # noqa: BLE001  独立部署 / 类目读不到时少一段，不影响编码
        parts = []
    slug = "-".join([p for p in parts[:3] if p]) or "QT"
    day = beijing_now().strftime("%Y%m%d")
    prefix = f"YHZ-{slug}-{day}-"
    from sqlalchemy import select  # noqa: PLC0415

    rows = db.scalars(select(SupplierItem.item_code).where(SupplierItem.item_code.like(f"{prefix}%"))).all()
    used = 0
    for code in rows:
        digits = "".join(ch for ch in str(code or "")[len(prefix):] if ch.isdigit())
        if digits:
            used = max(used, int(digits))
    return f"{prefix}{used + 1:04d}"


# ================= 渔货号（2026-10-15）=================
# 渔货号是渔集建档时给**商品款**编的号：``YS + 建档日期(8 位) + 当天 4 位流水``，共 14 位
# （口径见渔集 wms/intake.py 的 next_fish_code()，那边改了这里要跟着改）。
# 渔货栈的「商家编码」跟渔枢系统商品编码、渔集渔货号是同一个号 —— 渔枢「上架到渔货栈」
# 回传时写进 supplier_items.item_code（见 app/services/sourcing_publish.py）。
_FISH_CODE_RE = re.compile(r"^YS\d{12}$")


def is_fish_code(code: object) -> bool:
    """这串编码是不是渔集发的渔货号（``YS`` + 12 位数字）。"""
    return bool(_FISH_CODE_RE.match(str(code or "").strip().upper()))


def fish_code_of(item: SupplierItem | None) -> str:
    """这款货在渔集的渔货号；**不是渔集建档的货一律返回空串**。

    自录入 / 外部档口的货，商家编码是 ``YHZ-…`` 那一套（见 :func:`next_item_code`），
    还有的是档口自己编的号（如 ``QCHA…`` 那种条码）—— 都不是渔货号。
    宁可空着，也不能把条码 / 流水号当成渔货号显示出去。
    """
    if item is None:
        return ""
    code = str(getattr(item, "item_code", "") or "").strip().upper()
    return code if is_fish_code(code) else ""


def _normalize_images(images: Iterable[str] | None) -> list[str]:
    """去空、去重、保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for url in images or []:
        url = (url or "").strip()
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _build_skus(item_id: str, payload: GoodsItemCreateIn, default_stock: int) -> list[dict[str, Any]]:
    """把表单 SKU 行规范化成 `sku_list.skus` 结构。

    无 SKU 行时补一条「默认规格」，保证后续铺货 / 库存预占至少有一个单元。
    """
    rows: list[dict[str, Any]] = []
    for idx, sku in enumerate(payload.skus):
        spec_text = (sku.spec_text or "").strip() or "默认规格"
        rows.append({
            "sku_id": (sku.sku_id or "").strip() or f"{item_id}::sku_{idx + 1}",
            "spec_text": spec_text,
            "price": float(sku.price or Decimal("0")),
            "stock": int(sku.stock or 0),
            "code": (sku.code or "").strip() or None,
        })
    if not rows:
        rows.append({
            "sku_id": f"{item_id}::sku_1",
            "spec_text": "默认规格",
            "price": float(payload.sale_price or payload.cost_price or Decimal("0")),
            "stock": int(default_stock),
            "code": None,
        })
    return rows


def apply_shipping_defaults(db: Session, item: SupplierItem) -> None:
    """按渔枢系统「货源 · 运费模板」的规则补默认运费模板与发货方式。

    命中顺序与渔枢系统一致：供应商 + 类目 > 类目 > 供应商 > 全局默认（自营无档口，
    实际走「类目 > 全局默认」）。独立部署、读不到那张表时静默跳过 —— 页面照旧能发商品。
    """
    if (item.freight_template or "").strip() and (item.shipping_mode or "").strip():
        return
    try:
        from app.services import shipping as hub_shipping  # noqa: PLC0415  独立部署时没有这个包

        from src.goods import catalog  # noqa: PLC0415

        scope = catalog._session_scope()  # noqa: SLF001  渔枢系统库（独立部署时回退自营库）
        if scope is None:
            return
        with scope() as hub_db:
            resolved = hub_shipping.resolve_for_product(
                hub_db,
                category_text=catalog.category_path().get(item.category_id or "", ""),
                supplier_id=None,
            )
    except Exception as exc:  # noqa: BLE001  规则读不到不影响上架
        logger.debug("apply_shipping_defaults skipped: %s", exc)
        return
    if not (item.freight_template or "").strip() and resolved.get("freight_template"):
        item.freight_template = resolved["freight_template"]
        if not item.shipping_fee and resolved.get("freight_fee"):
            item.shipping_fee = Decimal(str(resolved["freight_fee"]))
    if not (item.shipping_mode or "").strip() and resolved.get("shipping_mode"):
        item.shipping_mode = resolved["shipping_mode"]


def create_item(
    db: Session,
    payload: GoodsItemCreateIn,
    *,
    commit: bool = True,
) -> SupplierItem:
    """创建商品（自营录入）。

    - `submit_action=published` → 直接 approved（自营内部货源不走平台审核）
    - `submit_action=audit`     → pending
    - `submit_action=draft`     → draft
    """
    images = _normalize_images([payload.main_image, *payload.images])
    detail_images = _normalize_images(payload.detail_images)

    item_id = generate_item_id()
    skus = _build_skus(item_id, payload, payload.stock)
    stock = int(payload.stock) or sum(int(s["stock"]) for s in skus)

    audit_status = SUBMIT_ACTION_TO_STATUS.get(payload.submit_action, "approved")
    now = _now()

    # 成本口径（2026-09-29 起）：渔货栈不显示「渔货栈对供应链的采购价」，
    # 渔货价对自营的人就是成本价 —— 没填成本价时用它兜底，定价与毛利都以此为准
    cost_price = payload.cost_price or payload.fish_price or Decimal("0")

    item = SupplierItem(
        item_id=item_id,
        supplier_id=None,  # 自营无档口
        source_type=payload.source_type,
        title=payload.title.strip(),
        category_id=payload.category_id,
        main_image=images[0] if images else None,
        images=images,
        detail_images=detail_images,
        cost_price=cost_price,
        sale_price=payload.sale_price,
        market_price=payload.market_price,
        fish_price=payload.fish_price,
        stock=stock,
        prev_stock=stock,
        sku_list={"skus": skus},
        spec_defs=payload.spec_defs,
        # 商家编码：留空按「YHZ + 类目简拼 + 日期 + 流水号」自动生成
        item_code=(payload.item_code or "").strip() or next_item_code(db, payload.category_id),
        brand=(payload.brand or "").strip() or None,
        selling_points=(payload.selling_points or "").strip() or None,
        # 商品标签（固定标签）：一行一个，与渔枢系统商品详情同一口径
        tags=(getattr(payload, "tags", None) or "").strip() or None,
        supplier_tip=(payload.supplier_tip or "").strip() or None,
        freight_template=payload.freight_template,
        shipping_mode=payload.shipping_mode,
        delivery_days=payload.delivery_days,
        after_sale=payload.after_sale,
        detail_html=payload.detail_html,
        shipping_fee=payload.shipping_fee or Decimal("0"),
        # 最近上架：表单不填（新建页已去掉该字段），由上架动作决定
        listed_at=payload.listed_at or (now if payload.submit_action == "publish" else None),
        return_rate=0,
        audit_status=audit_status,
        audit_note=None if audit_status != "approved" else "自营商品直接上架",
        audited_at=now if audit_status == "approved" else None,
        last_synced_at=now,
        last_stock_change_at=now,
        raw_json={"origin": "web_form", "submit_action": payload.submit_action},
    )
    # 运费模板 / 发货方式：表单没选就按「货源 · 运费模板」的规则套用（类目 > 全局默认）
    apply_shipping_defaults(db, item)
    db.add(item)
    if commit:
        db.commit()
    logger.info("create_item item_id=%s source=%s audit=%s", item_id, item.source_type, audit_status)
    return item


def audit_item(
    db: Session,
    item_id: str,
    action: str,
    *,
    note: str | None = None,
    by: str = "human",
    commit: bool = True,
) -> SupplierItem | None:
    """审核单个商品；商品不存在返回 None。"""
    status = AUDIT_ACTION_TO_STATUS.get(action)
    if status is None:
        raise ValueError(f"未知审核动作：{action}")

    item = db.query(SupplierItem).filter(SupplierItem.item_id == item_id).first()
    if item is None:
        return None

    item.audit_status = status
    item.audit_note = note or (f"{by} 审核通过" if action == "approve" else f"{by} 驳回")
    item.audited_at = _now()
    if commit:
        db.commit()
    return item


def batch_audit(
    db: Session,
    item_ids: list[str],
    action: str,
    *,
    note: str | None = None,
    by: str = "human",
) -> dict[str, Any]:
    """批量审核；返回成功 / 失败统计（不因个别失败中断整批）。"""
    succeeded = 0
    failed: list[str] = []
    for iid in item_ids:
        try:
            item = audit_item(db, iid, action, note=note, by=by, commit=False)
        except ValueError:
            failed.append(iid)
            continue
        if item is None:
            failed.append(iid)
        else:
            succeeded += 1
    db.commit()
    return {
        "total": len(item_ids),
        "succeeded": succeeded,
        "failed": len(failed),
        "failed_item_ids": failed,
    }


__all__ = [
    "SUBMIT_ACTION_TO_STATUS",
    "AUDIT_ACTION_TO_STATUS",
    "VALID_AUDIT_STATUSES",
    "generate_item_id",
    "create_item",
    "audit_item",
    "batch_audit",
]
