"""渔集（检测仓）出库 / 回库 → 渔枢商品库存 + 渔货栈平台库存跟着变（2026-10-10）。

**为什么要有这个**：渔集那边一台货卖出去（销售出库核销），货真的不在了 —— 可渔枢的商品
档案还挂着原来的库存、渔货栈还在按老库存卖。以前两边各算各的，只能靠人手动改。

**谁调它**：渔集的 ``wms/bridge.py::push_stock()``（渔集出库核销、买家退货召回时各调一次）。
方向是**渔集 → 渔枢**，只减/加库存，不动价格、不动状态、不动别的字段。

**认货靠渔货号**（2026-10-14 起）：以前「商家编码」直接复用渔集那**一台货的条码**，
同款多台分散在好几个编码上，渔枢永远不知道这一款还剩多少。现在统一认渔集内部发的
**渔货号**（``YS + 日期 + 流水``，一个渔货号 = 一款货 = 多个条码商品），按
``Product.code == 渔货号`` 认回这款商品；再顺着它找到渔货栈那条货源（``hub-prd-<商品id>``），
一起把库存改掉。认不回来的记在 ``missing`` 里返回，页面会说人话。
调用方（``wms.bridge.push_stock``）已经把条码换算成渔货号并按款合并过了 ——
这里收到的就是「这一款少了 / 多了几台」。

**口径**：库存按台数整进整出（渔集一台货可以带 N 台，见 ``WmsItem.qty``）；
商品级库存 = 各规格库存之和（沿用 ``product.sync_stock_from_skus`` 的口径），
所以有规格时先扣规格、再回算商品级；一条规格都没有才直接改商品级。
库存为 -1（不管理库存）的商品**一个数字都不动**，只在摘要里说明。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from ..models import Product
from ..timeutil import now

logger = logging.getLogger(__name__)


def _list_skus(db, product: Product) -> list[Any]:
    from . import product as product_service
    return list(product_service.list_skus(db, product.id))


def _spread(stocks: list[Any], delta: int) -> None:
    """把变动摊到这些库存行上：减就从前往后扣（扣到 0 为止），加就全给第一行。

    跟出库拣货一个思路 —— 先扣前面的规格，别让某一行的库存变成负的。
    """
    if not stocks or delta == 0:
        return
    if delta > 0:
        stocks[0].stock = max(0, int(stocks[0].stock or 0)) + delta
        return
    left = abs(delta)
    for row in stocks:
        cur = max(0, int(row.stock or 0))
        take = min(cur, left)
        if take:
            row.stock = cur - take
            left -= take
        if left <= 0:
            break


def _shift_product(db, product: Product, delta: int) -> tuple[int, int, bool]:
    """改渔枢商品的库存。返回 ``(改前, 改后, 是否动过)``。"""
    from . import product as product_service

    before = int(product.stock or 0)
    skus = [k for k in _list_skus(db, product) if int(k.stock or -1) >= 0]
    if skus:
        _spread(skus, delta)
        product_service.sync_stock_from_skus(db, product)     # 商品级 = 各规格之和
        return before, int(product.stock or 0), True
    if before < 0:
        return before, before, False                          # -1 = 不管理库存，别乱给数
    product.stock = max(0, before + delta)
    return before, int(product.stock or 0), True


def _shift_supplier_item(db, product: Product, delta: int) -> tuple[int, int] | None:
    """渔货栈那条货源（已上架才跟着动）的库存 —— 商品级 + 规格明细一起改。"""
    from . import sourcing_publish

    try:
        item = sourcing_publish.published_item(db, product)
    except Exception:  # noqa: BLE001
        logger.info("渔货栈货源查询失败（跳过库存同步）", exc_info=True)
        return None
    if item is None:
        return None
    before = max(0, int(item.stock or 0))
    after = max(0, before + delta)
    item.stock = after
    item.prev_stock = after
    item.last_stock_change_at = now()
    payload = dict(item.sku_list or {})
    skus = list(payload.get("skus") or [])
    if skus:
        left = abs(delta)
        if delta > 0:
            skus[0]["stock"] = max(0, int(skus[0].get("stock") or 0)) + delta
        else:
            for row in skus:
                cur = max(0, int(row.get("stock") or 0))
                take = min(cur, left)
                if take:
                    row["stock"] = cur - take
                    left -= take
                if left <= 0:
                    break
        payload["skus"] = skus
        item.sku_list = payload
    return before, after


def apply_stock_deltas(rows: list[tuple[str, int]]) -> dict:
    """把 ``[(渔货号, 变动台数)]`` 落到渔枢商品库存与渔货栈平台库存上。

    负数 = 出库减少，正数 = 回库增加。返回 ``{updated, missing, notes, unchanged}``：
    ``missing`` 是渔枢里没认到商品的渔货号（页面上要提醒人），``notes`` 是给人看的账变说明。
    """
    moves = [(str(code or "").strip(), int(qty or 0)) for code, qty in (rows or [])]
    moves = [(code, qty) for code, qty in moves if code and qty]
    report: dict[str, Any] = {"updated": 0, "missing": [], "notes": [], "unchanged": []}
    if not moves:
        return report

    from ..db import SessionLocal

    with SessionLocal() as db:
        for fish_code, delta in moves:
            product = db.scalar(select(Product).where(Product.code == fish_code))
            if product is None:
                report["missing"].append(fish_code)
                continue
            before, after, moved = _shift_product(db, product, delta)
            if not moved:
                report["unchanged"].append(f"{product.name}（不管理库存）")
                db.commit()
                continue
            tail = ""
            shot = _shift_supplier_item(db, product, delta)
            if shot is not None:
                tail = f"，渔货栈 {shot[0]} → {shot[1]}"
            # 售罄自动下架（2026-10-15 用户拍板）：渔集出库核销把这款渔货号的库存
            # 减到 0 —— 渔枢这边还在「在售」就自动下架，商品列表打「售罄」标。
            # 只在真减到 0 时动，回库（delta>0）不自动重新上架（那是人的决定）。
            sellout = ""
            if (
                after <= 0
                and getattr(product, "stock_enabled", True)
                and str(product.status or "") == "on_sale"
            ):
                product.status = "off_sale"
                sellout = "，库存归零已自动下架（售罄）"
            report["updated"] += 1
            report["notes"].append(
                f"{product.name}：渔枢库存 {before} → {after}{tail}{sellout}"
            )
            db.commit()
    return report


def sync_note(report: dict | None) -> str:
    """把同步摘要翻成页面提示条尾巴（没有变化就返回空串）。"""
    if not report:
        return ""
    bits: list[str] = []
    updated = int(report.get("updated") or 0)
    if updated:
        bits.append(f"渔枢 / 渔货栈库存已同步 {updated} 款")
    missing = list(report.get("missing") or [])
    if missing:
        bits.append("这些渔货号在渔枢没认到商品（库存没同步）："
                    + "、".join(str(x) for x in missing[:3])
                    + ("…" if len(missing) > 3 else ""))
    unchanged = list(report.get("unchanged") or [])
    if unchanged:
        bits.append("、".join(unchanged[:2]) + " 是不管理库存的商品，没动")
    return "；".join(bits)
