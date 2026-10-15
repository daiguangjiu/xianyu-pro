"""渔枢 → 渔集（检测仓 wms）的只读桥（2026-10-10）。

方向跟 ``wms/bridge.py`` **相反**：那边是渔集读渔枢的供应商与账号，这边是渔枢读渔集的货品
—— 发布商品时扫一个条码，就把渔集建档的那条货带过来：

  商品名称 ← 渔集的产品名（清单里那串）
  品牌     ← 渔集的品牌
  渔货价   ← 渔集定级后落库的售价（没定级就按「基准价 × 该档系数」现算）
  建议售价 ← 渔集的建议售价（基准价 × 建议售价系数）
  市场价   ← 渔集的**基准价**（渔集口径里基准价就是市场价）
  库存     ← 渔集的数量（一条货可以带 N 台）
  商家编码 ← 渔集的条码（终身唯一，直接复用，SKU 编码由它派生）

三条硬规矩：
  1. **只读** —— 一个字都不写渔集的表；
  2. **桥断不 500** —— 渔集子系统没装 / 库连不上 / 条码查不到，一律返回 None，
     页面给一句人话提示（渔枢该照常能用）；
  3. 渔货价不能高于建议售价（渔集的定价规矩，带过来也要守着）。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

logger = logging.getLogger(__name__)


def block_reason_of(item) -> str:
    """这台渔集的货**能不能拿去渔枢建档 / 上架**：空串 = 可以，非空 = 不行 + 原因。

    2026-10-13 用户拍板：渔枢上架商品的条码必须是**可售档 + 可售状态 + 在可售仓**
    的货 —— 报修中（C）、报废（D）、待检（W）、还在维修仓 / 危废区的都不许带上岸，
    否则渔枢会拿着一台根本卖不了的货去建档、去铺货。

    「已锁货」（订单占用）算可售 —— 它只是被单子占着，档位与仓位都没问题。
    """
    try:
        from wms.models import (  # 延迟导入：渔集缺了不能拖垮渔枢
            GRADE_LABELS, SELLABLE_GRADES, STATUS_LABELS, STATUS_LOCKED,
            STATUS_SELLABLE, WAREHOUSE_DEFAULTS, WH_SALE,
        )
    except Exception:  # noqa: BLE001
        return ""
    grade = str(getattr(item, "grade", "") or "").upper()
    status = str(getattr(item, "status", "") or "").upper()
    wh = str(getattr(item, "warehouse_code", "") or "").upper()
    if grade not in SELLABLE_GRADES:
        return (f"这台货在渔集判的是 {GRADE_LABELS.get(grade, grade)} 档"
                f"（不是可卖的 N / S / A / B 档），渔枢不能用它建档上架")
    if status not in (STATUS_SELLABLE, STATUS_LOCKED):
        return (f"这台货在渔集是「{STATUS_LABELS.get(status, status)}」状态"
                f"（不是「可售」），渔枢不能用它建档上架")
    if wh != WH_SALE:
        wh_name = dict((c, n) for c, n, _d, _o in WAREHOUSE_DEFAULTS).get(wh, wh or "（没仓）")
        return (f"这台货还在渔集的「{wh_name}」，不在「可售仓」—— "
                f"先把它入到可售仓再来渔枢建档上架")
    return ""


def _levels(raw: str) -> list[str]:
    """把「一级 › 二级 › 三级」切成三段（渔集与渔枢都用这个分隔符）。"""
    text = str(raw or "").replace("＞", "›").replace(">", "›")
    return [p.strip() for p in text.split("›") if p.strip()][:3]


def lookup_item(code: str) -> dict[str, Any] | None:
    """按条码 / 货号 / 序列号查渔集的货品，返回一份「建档够用」的快照。"""
    raw = str(code or "").strip()
    if not raw:
        return None
    try:
        from wms import grading, intake  # 延迟导入：渔集是独立子系统，缺了也不能拖垮渔枢
        from wms.db import SessionLocal
        from wms.models import category_1688, category_path
    except Exception:  # noqa: BLE001
        logger.info("渔集（wms）子系统不可用 —— 跳过条码带出")
        return None

    db = SessionLocal()
    try:
        item = intake.find_item(db, raw)
        if item is None:
            return None
        # 建议售价：建档 / 编辑时人工填过的优先（2026-10-12 新建档页加了这格），
        # 没填才按「市场价 × 建议售价系数」现算 —— 交给 price_of 统一口径
        quote = grading.price_of(
            db, item.grade, float(item.base_price or 0.0), float(item.purchase_cost or 0.0),
            manual_suggest=float(item.suggest_price or 0.0) or None,
        )
        # 渔货价：定级后落库的售价优先；没定级（W）就按该档系数现算
        fish = float(item.selling_price or 0.0) or float(quote.get("price") or 0.0)
        suggest = float(quote.get("suggest_price") or 0.0)
        if suggest > 0 and fish > suggest:
            fish = suggest       # 渔货价不能高于建议售价
        return {
            "barcode": str(item.barcode or ""),
            "display_code": str(item.display_code or ""),
            "sku_code": str(item.sku_code or ""),
            # 渔货号（2026-10-14）：**商家编码写的就是它** —— 这款货在渔集的身份证，
            # 后面渔集出库 / 退货召回也按它认回来改库存（一个号对多个条码商品）
            "fish_code": str(item.fish_code or ""),
            "product_name": str(item.product_name or item.title or ""),
            "brand": str(item.brand or ""),
            "model": str(item.model or ""),
            "spec": str(item.spec or ""),
            "serial_no": str(item.serial_no or ""),
            "category": str(item.category or ""),
            # 类目（2026-10-10）：渔集给两套三级路径 ——
            #   category_1688  对标好的 1688 官方类目名（渔枢类目树就是 1688 那套，优先用它）
            #   category_local 渔集自己的中文名（没对标时兜底）
            "category_levels": _levels(category_1688(item.category)) or _levels(
                category_path(item.category, sep="›")
                if item.category else ""
            ),
            "category_1688": _levels(category_1688(item.category)),
            "category_local": _levels(
                category_path(item.category, sep="›") if item.category else ""
            ),
            "grade": str(item.grade or ""),
            "status": str(item.status or ""),
            "warehouse_code": str(item.warehouse_code or ""),
            # 能不能拿去建档上架（2026-10-13 用户拍板：必须可售档 + 可售状态 + 在可售仓）
            "sellable": not block_reason_of(item),
            "block_reason": block_reason_of(item),
            # 数量（渔集口径：一条货可以带 N 台）→ 渔枢的库存
            "qty": int(item.qty or 0) or 1,
            "fish_price": round(fish, 2),
            "suggest_price": round(suggest, 2),
            # 市场价 = 渔集的基准价
            "market_price": round(float(item.base_price or 0.0), 2),
            "purchase_cost": round(float(item.purchase_cost or 0.0), 2),
        }
    except Exception:  # noqa: BLE001
        logger.warning("按条码查渔集货品失败（不阻断渔枢）", exc_info=True)
        return None
    finally:
        db.close()


def sync_price_from_wms(
    fish_code: str,
    *,
    name: str = "",
    selling_price: float = 0.0,
    suggest_price: float = 0.0,
    base_price: float = 0.0,
) -> dict[str, Any]:
    """渔集价格更新 → 渔枢商品档案 + 渔货栈货源一起跟着改（2026-10-15 用户拍板）。

    口径与 :func:`app.services.product.import_from_yushangzai` 完全一致：
      渔货价 = ``selling_price``（同时写 ``cost_amount``）；
      建议售价 = ``suggest_price``，没设按「市场价 × 0.5」兜底，再没有回落渔货价；
      市场价 = ``base_price``（>0 才写，别把人手填的划线价清成 0）。

    渔货价或销售价真变了时，往 ``price_change_notices`` 落一条「商品价格变动」通知
    （渔货栈「异常提醒」/self/logs 页顶部的通知区块直接读它）。渔枢里认不到这款
    商品不报错，返回摘要让调用方自己决定说不说。
    """
    empty: dict[str, Any] = {"updated": False, "notice": False, "notes": []}
    code = str(fish_code or "").strip().upper()
    if not code:
        return empty

    fish = round(float(selling_price or 0.0), 2)
    base = round(float(base_price or 0.0), 2)
    suggest = round(float(suggest_price or 0.0), 2) or round(base * 0.5, 2) or fish

    from ..db import SessionLocal
    from ..models import Product
    from ..timeutil import now

    with SessionLocal() as db:
        product = db.scalar(select(Product).where(Product.code == code))
        if product is None:
            return {**empty, "notes": [f"渔枢里没有商家编码为 {code} 的商品，价格没同步"]}

        old_fish = round(float(product.fish_price or product.cost_amount or 0.0), 2)
        old_price = round(float(product.price or 0.0), 2)
        product.fish_price = fish
        product.cost_amount = fish
        product.price = suggest
        if base > 0:
            product.original_price = base
        product.updated_at = now()

        # 渔货栈那条货源（hub-prd-<商品id>）一起改：cost_price=渔货价、sale_price=建议售价
        notice = False
        try:
            from src.models import SupplierItem

            src_item = db.scalar(
                select(SupplierItem).where(SupplierItem.item_id == f"hub-prd-{product.id}")
            )
            if src_item is not None:
                src_item.cost_price = fish
                src_item.fish_price = fish
                src_item.sale_price = suggest
                if base > 0:
                    src_item.market_price = base
        except Exception:  # noqa: BLE001  渔货栈表不在（分端部署）→ 只改渔枢
            logger.info("渔货栈货源价格同步跳过（表不可读）", exc_info=True)

        if abs(old_fish - fish) > 0.005 or abs(old_price - suggest) > 0.005:
            notice = True
            try:
                from src.models import PriceChangeNotice

                db.add(
                    PriceChangeNotice(
                        fish_code=code,
                        product_code=str(product.code or ""),
                        product_name=str(name or product.name or ""),
                        old_fish_price=old_fish,
                        new_fish_price=fish,
                        old_price=old_price,
                        new_price=suggest,
                        direction="up" if fish > old_fish else ("down" if fish < old_fish else ""),
                        created_at=now(),
                    )
                )
            except Exception:  # noqa: BLE001  通知表不在 → 价格照常同步
                logger.info("价格变动通知落库跳过（表不可读）", exc_info=True)

        db.commit()
        return {
            "updated": True,
            "notice": notice,
            "notes": [
                f"{product.name}：渔货价 ¥{old_fish:g} → ¥{fish:g}，销售价 ¥{old_price:g} → ¥{suggest:g}"
                + ("（价格变动通知已发出）" if notice else "")
            ],
        }


def search_items(keyword: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """按关键词（条码 / 货号 / 品名 / 品牌）列渔集货品 —— 给「商品条码」那格的下拉候选。

    跟「供应链商品选供应链」一个心思：不想手打条码的人，点开就能看到渔集里有哪些货、
    各自几台。**只读**，桥断返回空列表（页面照旧能手输 / 扫码）。
    """
    try:
        from wms import intake
        from wms.db import SessionLocal
    except Exception:  # noqa: BLE001
        logger.info("渔集（wms）子系统不可用 —— 候选列表为空")
        return []
    db = SessionLocal()
    try:
        rows = intake.list_items(db, q=str(keyword or "").strip(), limit=max(1, int(limit or 20)))
        return [
            {
                "barcode": str(row.barcode or ""),
                "display_code": str(row.display_code or ""),
                "sku_code": str(row.sku_code or ""),
                "product_name": str(row.product_name or row.title or ""),
                "brand": str(row.brand or ""),
                "grade": str(row.grade or ""),
                "status": str(row.status or ""),
                "warehouse_code": str(row.warehouse_code or ""),
                # 候选照样列全（未检测 / 报修中的也得让人看见），但标出能不能带出去上架
                "sellable": not block_reason_of(row),
                "block_reason": block_reason_of(row),
                "qty": int(row.qty or 0) or 1,
                "market_price": round(float(row.base_price or 0.0), 2),
            }
            for row in rows
        ]
    except Exception:  # noqa: BLE001
        logger.warning("列渔集货品失败（不阻断渔枢）", exc_info=True)
        return []
    finally:
        db.close()
