#!/usr/bin/env python3
"""已导入的渔货栈 YS 商品：按方案 B 刷新销售价（2026-10-15 用户拍板）。

口径（与 app.services.product.SUGGEST_FALLBACK_RATIO 一致）：
    销售价 = 渔集建议售价；没填 → 市场价（基准价）× 0.5；市场价也没有 → 渔货价。
同时把多 SKU 商品（渔货号名下多条码）的规格行按同口径重建（幂等，先删后建）。

在部署目录的容器里跑：

    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 \\
        /app/scripts/refresh_ys_product_prices.py            # 预览
    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 \\
        /app/scripts/refresh_ys_product_prices.py --apply    # 真刷
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Product  # noqa: E402
from app.services.product import SUGGEST_FALLBACK_RATIO, split_product_skus  # noqa: E402


def price_of(selling: float, suggest: float, base: float) -> float:
    return (suggest or round(base * SUGGEST_FALLBACK_RATIO, 2) or selling)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="真刷（默认只预览）")
    ap.add_argument("--all", action="store_true",
                    help="全部 YS 商品都刷（默认只刷「有问题的款」：销售价=渔货价、或销售价=0）"
                         "—— 运营手动定过的价不动")
    args = ap.parse_args()

    from wms import intake as wms_intake

    changed = unchanged = 0
    with SessionLocal() as s:
        products = list(s.scalars(
            select(Product).where(Product.code.like("YS%")).order_by(Product.code)))
        for p in products:
            items = wms_intake.find_items_by_fish(s, str(p.code))
            if not items:
                print(f"[跳过] {p.code}：渔集里查不到，不动")
                continue
            head = items[0]
            selling = float(head.selling_price or 0.0)
            suggest = float(head.suggest_price or 0.0)
            base = float(head.base_price or 0.0)
            new_price = price_of(selling, suggest, base)
            old_price = float(p.price or 0.0)
            # 默认只动「有问题的款」：销售价=渔货价（本次要修的 bug）、或压根没价。
            # 其它（运营定过的价 / 老口径）不碰，要全刷加 --all
            problematic = (old_price == 0) or (selling > 0 and abs(old_price - selling) < 0.005)
            if not args.all and not problematic:
                print(f"[留] {p.code} price={old_price} fish={selling}（运营已定价，不动；--all 才刷）")
                unchanged += 1
                continue
            note = ""
            if abs(new_price - old_price) > 0.005:
                changed += 1
                p.price = new_price
                note = f"price {old_price} -> {new_price}"
            else:
                unchanged += 1
            skus = split_product_skus(s, p, items=items)
            print(f"[{'刷' if note else '同'}] {p.code} fish={selling} "
                  f"suggest={suggest} base={base} -> {new_price} "
                  f"(规格 {skus}) {note}")
        if args.apply:
            s.commit()
            print(f"完成（--apply）：刷新 {changed} 款 / 一致 {unchanged} 款")
        else:
            s.rollback()
            print(f"预览：将刷新 {changed} 款 / 一致 {unchanged} 款（加 --apply 真刷）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
