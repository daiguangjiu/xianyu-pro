#!/usr/bin/env python3
"""把渔枢系统「已经上架」的商品售后政策统一成「非质量问题不退不换」（2026-10-16，一次性脚本）。

口径（用户拍板）：
    1. 渔枢系统新增商品的售后政策默认就是「非质量问题不退不换」
       （常量在 app/services/product.py::DEFAULT_AFTER_SALE，新建表单默认选中它）；
    2. **已经上架的商品**也一并更新 —— 我们卖的是尾货 / 二手检测机，只保质量问题，
       「7 天无理由退换」会把退货率抬上去。

范围：默认只动 ``status == on_sale``（在售）的商品；加 ``--all`` 才把未上架 / 已下架的一起改。
每改一个商品，顺手把渔货栈那条 ``hub-prd-<商品id>`` 货源的售后政策同步成同一个值
（渔货栈详情页「规格参数」页签里那一行就是这个字段）。

默认**只看不改**，加 ``--apply`` 才真写。

用法::

    python scripts/update_after_sale_policy.py            # 预演
    python scripts/update_after_sale_policy.py --apply    # 真写
    python scripts/update_after_sale_policy.py --all      # 连未上架的一起（预演）

容器里::

    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 /app/scripts/update_after_sale_policy.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal  # noqa: E402

SAMPLE_LIMIT = 20


def _supplier_of(session, product_id: int):
    """渔货栈那条同源货源（item_id = hub-prd-<商品id>），没有就返回 None。"""
    from src.models import SupplierItem

    item_id = f"hub-prd-{product_id}"
    return (
        session.query(SupplierItem)
        .filter(SupplierItem.item_id == item_id)
        .first()
    )


def plan(session, *, only_on_sale: bool = True) -> list[dict]:
    """只读预检：列出「售后政策还不是默认口径」的商品。"""
    from app.models import Product
    from app.services.product import DEFAULT_AFTER_SALE, PRODUCT_STATUS_ON

    stmt = session.query(Product).order_by(Product.id)
    if only_on_sale:
        stmt = stmt.filter(Product.status == PRODUCT_STATUS_ON)

    out: list[dict] = []
    for product in stmt.all():
        current = (product.after_sale or "").strip()
        if current == DEFAULT_AFTER_SALE:
            continue  # 已经是目标值就不用动
        out.append({
            "product": product,
            "code": str(product.code or ""),
            "name": str(product.name or ""),
            "status": str(product.status or ""),
            "old": current,
            "new": DEFAULT_AFTER_SALE,
        })
    return out


def apply_rows(session, rows: list[dict]) -> dict:
    """逐个改 + 同步渔货栈。每个商品自己提交（一处失败不牵连别个）。"""
    stat = {"products": 0, "synced": 0, "failed": 0}
    for row in rows:
        product = row["product"]
        try:
            product.after_sale = row["new"]
            session.add(product)
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            stat["failed"] += 1
            continue
        stat["products"] += 1
        try:
            item = _supplier_of(session, int(product.id))
            if item is not None and (item.after_sale or "").strip() != row["new"]:
                item.after_sale = row["new"]
                session.add(item)
                session.commit()
                stat["synced"] += 1
        except Exception:  # noqa: BLE001  渔货栈那张表不在（分端部署）就算了
            session.rollback()
    return stat


def main() -> int:
    ap = argparse.ArgumentParser(description="统一「已上架」商品的售后政策")
    ap.add_argument("--apply", action="store_true", help="真写（默认只预览）")
    ap.add_argument("--all", action="store_true", help="连未上架 / 已下架的商品一起改")
    args = ap.parse_args()

    from app.services.product import DEFAULT_AFTER_SALE

    print("=" * 64)
    print(f"售后政策统一为「{DEFAULT_AFTER_SALE}」",
          "（正式执行）" if args.apply else "（预演，加 --apply 才真写）")
    print("范围：", "全部商品" if args.all else "只改已上架（在售）的商品")
    print("=" * 64)

    with SessionLocal() as session:
        rows = plan(session, only_on_sale=not args.all)
        print(f"要改的：{len(rows)} 款")
        for row in rows[:SAMPLE_LIMIT]:
            print(f"  {row['code']} [{row['status']}] {row['name'][:28]} "
                  f"「{row['old'] or '空'}」→「{row['new']}」")
        if len(rows) > SAMPLE_LIMIT:
            print(f"  …还有 {len(rows) - SAMPLE_LIMIT} 款")
        if not args.apply or not rows:
            print("完成（没写库）。" if not args.apply else "没有要改的（都已经是目标口径）。")
            return 0
        stat = apply_rows(session, rows)

    print(f"已改：{stat['products']} 款；同步渔货栈：{stat['synced']} 条；失败：{stat['failed']} 款")
    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
