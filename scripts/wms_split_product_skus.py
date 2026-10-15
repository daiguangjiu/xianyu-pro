#!/usr/bin/env python3
"""一次性：给已导入的渔枢 YS 商品按渔货号分拆多 SKU（2026-10-15 用户拍板）。

口径（与 app.services.product.split_product_skus 一致）：
· 渔货号名下 **多个条码商品**（多个进货货号）→ 渔枢商品默认分拆成多条规格，
  规格名取 规格 → 型号 → 显示编码 → 条码，价格 / 库存各带各的；
· 一款只一条记录的（哪怕一台带 N 台）不拆；``YS202610120001`` 用户点名不拆；
· 商品已有规格行的**跳过不动**（不覆盖人工维护的数据）。

跑法（服务器容器内）：
    docker compose cp scripts/wms_split_product_skus.py app:/tmp/x.py
    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 /tmp/x.py           # 预览
    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 /tmp/x.py --apply   # 真拆
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="真写库（默认只预览）")
    args = ap.parse_args()

    from sqlalchemy import select

    from app.db import SessionLocal, init_db
    from app.models import Product
    from app.services import product as product_service
    from wms import intake as wms_intake

    init_db()
    with SessionLocal() as s:
        rows = list(s.scalars(
            select(Product).where(Product.code.like("YS%")).order_by(Product.id)
        ).all())
        split = skipped = 0
        for p in rows:
            code = str(p.code or "").strip().upper()
            if not code:
                continue
            if code in product_service.SKU_SPLIT_EXCLUDED:
                print(f"[例外] {code} 用户点名不拆")
                skipped += 1
                continue
            items = wms_intake.find_items_by_fish(s, code)
            if len(items) <= 1:
                continue
            existing = product_service.list_skus(s, int(p.id or 0))
            if existing:
                print(f"[跳过] {code} 已有 {len(existing)} 条规格，不动（不覆盖人工数据）")
                skipped += 1
                continue
            name = str(p.name or "")[:24]
            if not args.apply:
                print(f"[预览] {code} {name} → 可拆 {len(items)} 条规格")
                split += 1
                continue
            n = product_service.split_product_skus(s, p, items=items)
            s.commit()
            print(f"[已拆] {code} {name} → {n} 条规格（总库存 {p.stock}）")
            split += 1
        print(f"完成：{'分拆' if args.apply else '可分拆'} {split} 款 / 跳过 {skipped} 款"
              f"（apply={args.apply}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
