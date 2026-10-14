#!/usr/bin/env python3
"""清理渔货栈里剩下的演示数据（2026-10-12，一次性脚本，系统要开放给学员用）。

货源那批（1688_* / self_* / DEMO-*）已经在 2026-10-04 用 scripts/purge_demo_items.py
清过；这次是把**订单域**的演示数据也清掉：

    downstream_orders  单号 xy_demo_order_*、收货人「演示收货人」、标题带【演示】
    stock_orders       收货人「演示收货人」（采购单），连带它的明细行 stock_order_items
    supplier_items     还剩下的 DEMO-* / 【演示】货源（连带 sku_mappings /
                       channel_listings / shop_goods / purchase_cart_items）

真实数据一律不碰：保留前缀 hub-prd- / M1- / SPU，以及单号不是演示标记的单。

用法（默认只预览，加 --apply 才真删）：
    python scripts/purge_demo_data.py
    python scripts/purge_demo_data.py --apply

注意：会改线上数据，跑之前先 scripts/backup.sh 备份。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal  # noqa: E402
from src.models import (  # noqa: E402
    ChannelListing,
    DownstreamOrder,
    PurchaseCartItem,
    ShopGoods,
    SkuMapping,
    StockOrder,
    StockOrderItem,
    SupplierItem,
)

# 演示货源的标记
DEMO_ITEM_PREFIXES = ("1688_", "self_", "DEMO-")
DEMO_TITLE_MARK = "【演示】"
# 真实货源前缀（双保险：命中这些的一律不删）
KEEP_PREFIXES = ("hub-prd-", "M1-", "SPU")
# 演示订单的标记（渔货栈下游单 + 渔枢系统里由演示单同步过去的那笔）
DEMO_ORDER_PREFIXES = ("xy_demo_order", "DEMO-")
DEMO_RECEIVER = "演示收货人"
BATCH = 400


def _chunks(ids):
    for i in range(0, len(ids), BATCH):
        yield ids[i:i + BATCH]


def _demo_item_ids(s) -> list[str]:
    doomed: list[str] = []
    for item_id, item_code, title in s.query(
        SupplierItem.item_id, SupplierItem.item_code, SupplierItem.title
    ).all():
        if not item_id:
            continue
        if item_id.startswith(KEEP_PREFIXES):
            continue
        text = f"{item_code or ''} {title or ''}"
        if item_id.startswith(DEMO_ITEM_PREFIXES) or DEMO_TITLE_MARK in text:
            doomed.append(item_id)
    return doomed


def _demo_order_ids(s) -> list[str]:
    doomed: list[str] = []
    for order_id, buyer, raw in s.query(
        DownstreamOrder.downstream_order_id,
        DownstreamOrder.buyer_name,
        DownstreamOrder.raw_json,
    ).all():
        if not order_id:
            continue
        # 商品名存在 raw_json 里（订单表本身没有 title 列）
        text = f"{buyer or ''} {raw or {}}"
        if str(order_id).startswith(DEMO_ORDER_PREFIXES) or DEMO_TITLE_MARK in text or DEMO_RECEIVER in text:
            doomed.append(order_id)
    return doomed


def _hub_demo_order_nos(s) -> list[str]:
    """渔枢系统（主系统）里由演示单同步过去的那几笔（origin=self 的 DEMO-*）。"""
    from app.models import Order

    return [
        str(no)
        for (no,) in s.query(Order.order_no).all()
        if str(no or "").startswith(DEMO_ORDER_PREFIXES)
    ]


def _demo_stock_order_ids(s) -> list[str]:
    doomed: list[str] = []
    for order_no, receiver in s.query(StockOrder.order_no, StockOrder.receiver_name).all():
        if not order_no:
            continue
        # 子单号是「母单号-N」，母单清掉时一起带上
        base = str(order_no).split("-")[0]
        if receiver == DEMO_RECEIVER and base not in doomed:
            doomed.append(base)
    # 子单：跟着母单一起清
    subs = [
        no for (no,) in s.query(StockOrder.order_no).all()
        if str(no or "").split("-")[0] in doomed
    ]
    return sorted(set(doomed) | set(subs))


def purge(apply: bool) -> int:
    removed = 0
    with SessionLocal() as s:
        items = _demo_item_ids(s)
        orders = _demo_order_ids(s)
        stock_orders = _demo_stock_order_ids(s)
        hub_orders = _hub_demo_order_nos(s)

        print("=== 预览（加 --apply 才真删）===")
        print(f"演示货源        {len(items)} 条")
        print(f"演示订单        {len(orders)} 笔")
        print(f"演示采购单      {len(stock_orders)} 张（含拆出来的子单）")
        print(f"渔枢系统演示单  {len(hub_orders)} 笔 {hub_orders[:5]}")
        if not (items or orders or stock_orders or hub_orders):
            print("没有要清理的数据，结束。")
            return 0
        if items:
            print(f"  货源样例：{items[:5]}")
        if orders:
            print(f"  订单样例：{orders[:5]}")
        if stock_orders:
            print(f"  采购单样例：{stock_orders[:5]}")

        if not apply:
            return 0

        # 1) 演示采购单：先明细后主单
        for chunk in _chunks(stock_orders):
            s.query(StockOrderItem).filter(
                StockOrderItem.order_id.in_(
                    s.query(StockOrder.id).filter(StockOrder.order_no.in_(chunk))
                )
            ).delete(synchronize_session=False)
            removed += s.query(StockOrder).filter(StockOrder.order_no.in_(chunk)).delete(
                synchronize_session=False
            )
        # 1.5) 渔枢系统里由演示单同步过去的那几笔（连带它名下的流水 / 日志 / 语音事件）
        if hub_orders:
            from app.models import Order, OrderImage, OrderLog, Settlement, VoiceEvent

            ids = [
                row.id for row in s.query(Order.id).filter(Order.order_no.in_(hub_orders)).all()
            ]
            for chunk in _chunks(ids):
                s.query(Settlement).filter(Settlement.order_id.in_(chunk)).delete(
                    synchronize_session=False
                )
                s.query(OrderLog).filter(OrderLog.order_id.in_(chunk)).delete(
                    synchronize_session=False
                )
                s.query(OrderImage).filter(OrderImage.order_id.in_(chunk)).update(
                    {"order_id": None}, synchronize_session=False
                )
                removed += s.query(Order).filter(Order.id.in_(chunk)).delete(
                    synchronize_session=False
                )
            s.query(VoiceEvent).filter(VoiceEvent.order_no.in_(hub_orders)).delete(
                synchronize_session=False
            )

        # 2) 演示订单
        for chunk in _chunks(orders):
            removed += s.query(DownstreamOrder).filter(
                DownstreamOrder.downstream_order_id.in_(chunk)
            ).delete(synchronize_session=False)
        # 3) 演示货源 + 挂在它下面的关联
        for chunk in _chunks(items):
            for model in (SkuMapping, ChannelListing, ShopGoods, PurchaseCartItem):
                s.query(model).filter(model.item_id.in_(chunk)).delete(
                    synchronize_session=False
                )
            removed += s.query(SupplierItem).filter(
                SupplierItem.item_id.in_(chunk)
            ).delete(synchronize_session=False)

        s.commit()

        print(f"已删除 {removed} 行")
        print(f"剩余：货源 {s.query(SupplierItem).count()} 条 · "
              f"订单 {s.query(DownstreamOrder).count()} 笔 · "
              f"采购单 {s.query(StockOrder).count()} 张")
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="清理渔货栈演示数据")
    parser.add_argument("--apply", action="store_true", help="真的删（默认只预览）")
    args = parser.parse_args()
    purge(bool(args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
