"""补算「有渔货价、没建议售价」的历史货品（2026-10-16 一次性脚本）。

用户口径：**有渔货价却没建议售价的货，统一按它自己那一档的评级规则算建议售价，
并且向下取整到元**（¥184.06 → ¥184.00）。

背景：检测定级只把「渔货价」落库，建议售价那一列一直是空的（历史 866 台里 864 台
是 0）。渔枢商品档案拿不到建议售价就只能按「市场价 × 0.5」兜底，兜出来的价
有时比渔货价还低（YS202610080025：渔货价 139 / 售价 116）—— 卖价低于进货价。

算法（跟检测台定级时用的是同一套 ``grading.price_of``）：

  1. 有市场价（``base_price``）：建议售价 = floor(市场价 × 该档建议售价系数)；
  2. 没有市场价：按渔货价反推市场价（渔货价 ÷ 该档价格系数），再套建议售价系数；
  3. 建议售价不得小于渔货价（供货价高过零售价没法卖）；
  4. 一律 ``grading.floor_price`` 向下取整到元。

默认**只看不改**，加 ``--apply`` 才真写。写完逐款推给渔枢商品档案 + 渔货栈货源
（``wms.bridge.push_price_change``）。

用法（容器里）::

    docker compose cp /tmp/wms_suggest_price_backfill.py app:/tmp/x.py
    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 /tmp/x.py
    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 /tmp/x.py --apply
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

OPERATOR = "script:wms_suggest_price_backfill"
# 一次预览最多打印几行
SAMPLE_LIMIT = 15


def suggest_of(session, item) -> tuple[float, dict]:
    """按这台货当前等级的规则算建议售价，返回 ``(建议售价, price_of 结果)``。"""
    from wms import grading

    price = grading.price_of(
        session,
        str(item.grade or "").upper(),
        float(item.base_price or 0.0),
        float(item.purchase_cost or 0.0),
    )
    sug = float(price.get("suggest_price") or 0.0)
    if sug <= 0:
        # 没有市场价（或系数为 0 的档）：按渔货价反推市场价再套建议售价系数
        factor = float(price.get("factor") or 0.0)
        suggest_factor = float(price.get("suggest_factor") or 0.0)
        base = (float(item.selling_price or 0.0) / factor) if factor > 0 else float(item.selling_price or 0.0)
        sug = grading.floor_price(base * suggest_factor)
    selling = float(item.selling_price or 0.0)
    # 建议售价不能低于渔货价（卖价低于进货价就没法卖了）
    if sug > 0 and selling > 0 and sug < selling:
        sug = grading.floor_price(selling)
    return sug, price


def plan(session) -> list[dict]:
    """只读预检：找出「有渔货价、没建议售价」的货品，返回待改清单。"""
    from wms.models import WmsItem

    out: list[dict] = []
    for item in session.scalars(select(WmsItem).order_by(WmsItem.id)).all():
        selling = round(float(item.selling_price or 0.0), 2)
        if selling <= 0:
            continue
        if round(float(item.suggest_price or 0.0), 2) > 0:
            continue
        sug, price = suggest_of(session, item)
        if sug <= 0:
            continue
        out.append({
            "item": item,
            "barcode": str(item.barcode or ""),
            "fish_code": str(item.fish_code or ""),
            "name": str(item.product_name or item.title or ""),
            "grade": str(item.grade or ""),
            "base": round(float(item.base_price or 0.0), 2),
            "selling": selling,
            "suggest": sug,
            "factor": float(price.get("factor") or 0.0),
            "suggest_factor": float(price.get("suggest_factor") or 0.0),
        })
    return out


def apply_rows(session, rows: list[dict]) -> dict:
    """逐台写建议售价 + 同步渔枢 / 渔货栈。每台自己提交（一处失败不牵连别台）。"""
    from wms import audit, bridge

    stat = {"items": 0, "synced": 0, "failed": 0}
    for row in rows:
        item = row["item"]
        try:
            item.suggest_price = row["suggest"]
            # 这个是系统按该档系数算出来的（不是人填的）：下次定级要跟着新档重算
            item.suggest_auto = True
            session.add(item)
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            stat["failed"] += 1
            continue
        stat["items"] += 1
        try:
            audit.log(
                session, "item", int(item.id), "suggest_price",
                before="¥0.00", after=f"¥{row['suggest']:.2f}", operator=OPERATOR,
                note=(f"{row['grade']} 档：市场价 ¥{row['base']:.2f} × "
                      f"建议售价系数 {row['suggest_factor']} 向下取整"
                      f"（渔货价 ¥{row['selling']:.2f}）· scripts/wms_suggest_price_backfill.py"),
            )
            session.commit()
        except Exception:  # noqa: BLE001  留痕失败不影响价格
            session.rollback()
        if not row["fish_code"]:
            continue
        try:
            report = bridge.push_price_change(
                row["fish_code"],
                name=row["name"],
                selling_price=float(item.selling_price or 0.0),
                suggest_price=float(item.suggest_price or 0.0),
                base_price=float(item.base_price or 0.0),
            )
            if (report or {}).get("updated"):
                stat["synced"] += 1
        except Exception:  # noqa: BLE001  桥断只降级
            pass
    return stat


def main() -> int:
    ap = argparse.ArgumentParser(description="补算「有渔货价、没建议售价」的货品")
    ap.add_argument("--apply", action="store_true", help="真写（默认只预览）")
    args = ap.parse_args()

    from wms.db import SessionLocal as WmsSession, init_tables

    init_tables()

    print("=" * 60)
    print("建议售价补算", "（正式执行）" if args.apply else "（预演，加 --apply 才真写）")
    print("=" * 60)

    with WmsSession() as s:
        rows = plan(s)
        print(f"待补算：{len(rows)} 台（有渔货价、建议售价还是空的）")
        for row in rows[:SAMPLE_LIMIT]:
            print(f"  {row['barcode']} {row['fish_code']} {row['grade']} 档 "
                  f"市场价 ¥{row['base']:.2f} × {row['suggest_factor']} → "
                  f"建议售价 ¥{row['suggest']:.2f}（渔货价 ¥{row['selling']:.2f}）")
        if len(rows) > SAMPLE_LIMIT:
            print(f"  …还有 {len(rows) - SAMPLE_LIMIT} 台")
        if not args.apply or not rows:
            print("完成（没写库）。" if not args.apply else "没有要补的。")
            return 0
        stat = apply_rows(s, rows)

    print(f"已补算：{stat['items']} 台；同步到渔枢 / 渔货栈：{stat['synced']} 款；"
          f"失败：{stat['failed']} 台")
    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
