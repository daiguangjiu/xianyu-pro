#!/usr/bin/env python3
"""清理渔货栈货源里的演示 / 1688 同步数据（2026-10-04，一次性脚本）。

背景：渔货栈货源广场（supplier_items）里 2546 条货源有 2542 条是假的 ——
    1688_*   2442 条：以前「1688 货源同步」灌进来的演示货（sup_logi / sup_home 等假供应商）
    self_0001..0100  100 条：演示期造的「渔货栈自营严选」
现在不再同步 1688 了，这批货留着会把真实货源（渔枢系统上架 / 商家发布）彻底淹没。

保留：
    hub-prd-*（渔枢系统商品档案上架过来的）
    M1-*（商家自己发布的）
    SPU<日期><6 位>（自营端手工录入的）
这三类的前缀写在 KEEP_PREFIXES 里，脚本会再兜一道：命中保留前缀的一律不动。

顺带清掉挂在被删货源下面的孤儿关联（sku_mappings / channel_listings /
shop_goods / purchase_cart_items），避免列表里出现「货没了但记录还在」。
stock_order_items 不动 —— 采购单是已发生的事实，明细行自带标题快照。

用法（默认只预览，加 --apply 才真删）：
    python scripts/purge_demo_items.py
    python scripts/purge_demo_items.py --apply

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
    PurchaseCartItem,
    ShopGoods,
    SkuMapping,
    SupplierItem,
)

# 演示 / 1688 同步数据的数据源前缀
DEMO_PREFIXES = ("1688_", "self_", "DEMO-")
# 真实货源前缀（双保险：命中这些的一律不删）
KEEP_PREFIXES = ("hub-prd-", "M1-", "SPU")
# 挂在货源下面的从表（按这个顺序删）
CHILD_MODELS = (SkuMapping, ChannelListing, ShopGoods, PurchaseCartItem)
# 一次最多带多少个 item_id（防 SQL 参数超限）
BATCH = 400


def _collect(session, only: set[str] | None = None) -> list[str]:
    """挑出要删的 item_id（演示 / 1688 同步那批）。

    `only` 只给测试用：把范围再收窄到指定 item_id，避免跑测试时把别人的数据删了。
    """
    doomed: list[str] = []
    for (item_id,) in session.query(SupplierItem.item_id).all():
        if not item_id:
            continue
        if only is not None and item_id not in only:
            continue
        if item_id.startswith(KEEP_PREFIXES):
            continue
        if item_id.startswith(DEMO_PREFIXES):
            doomed.append(item_id)
    return doomed


def _chunks(ids: list[str]):
    for i in range(0, len(ids), BATCH):
        yield ids[i:i + BATCH]


def purge(apply: bool, only: set[str] | None = None) -> int:
    with SessionLocal() as s:
        doomed = _collect(s, only=only)
        total = s.query(SupplierItem).count()
        print(f"当前货源总数：{total} 条")
        print(f"命中演示 / 1688 同步数据：{len(doomed)} 条（保留 {total - len(doomed)} 条）")
        if doomed:
            print(f"  样例：{doomed[:5]}")
        if not doomed:
            print("没有要清理的数据，结束。")
            return 0

        removed = 0
        for chunk in _chunks(doomed):
            for model in CHILD_MODELS:
                n = (
                    s.query(model)
                    .filter(model.item_id.in_(chunk))
                    .delete(synchronize_session=False)
                )
                if n:
                    print(f"  {model.__tablename__}: 清理 {n} 条")
            removed += (
                s.query(SupplierItem)
                .filter(SupplierItem.item_id.in_(chunk))
                .delete(synchronize_session=False)
            )

        if apply:
            s.commit()
            print(f"已删除：supplier_items {removed} 条")
            print(f"剩余货源：{s.query(SupplierItem).count()} 条")
        else:
            s.rollback()
            print(f"（预览）将删除 supplier_items {removed} 条 —— 加 --apply 才真删")
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="清理渔货栈演示 / 1688 同步货源")
    parser.add_argument("--apply", action="store_true", help="真的执行删除（默认只预览）")
    args = parser.parse_args()
    purge(args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
