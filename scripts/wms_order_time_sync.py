"""把渔集里「下单时间」那一列同步给渔货栈货源（2026-10-16 一次性脚本）。

背景（用户要求）：
  · 渔集系统要在**商品维度**（货品台账 /wms/goods、商品详情 /wms/goods/{渔货号}）
    把导入清单里的「下单时间」显示出来；
  · 这个时间也要能在渔枢 / 渔货栈的**规格参数**里看到。

值从哪来：导入 Excel 时认不出的列会沉淀进 ``WmsItem.extra``（「下单时间」就是其中一列），
``wms/intake.py::order_time_of`` 按渔货号把多台货的值聚成一句（多台不一样给区间）。

同步链：``wms.bridge.push_profile_change`` → ``app.services.wms_bridge.sync_profile_from_wms``
→ 写渔货栈 ``supplier_items.order_time``（item_id = hub-prd-<商品id>）。
渔枢商品档案不存这一列，详情页按「商家编码 == 渔货号」实时从渔集读，所以这里不用管。

默认**只看不改**，加 ``--apply`` 才真写。

用法（容器里）::

    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 /app/scripts/wms_order_time_sync.py
    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 /app/scripts/wms_order_time_sync.py --apply
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

SAMPLE_LIMIT = 20


def plan(session) -> list[dict]:
    """只读预检：按渔货号聚合，列出「有下单时间」的款。"""
    from wms import intake
    from wms.models import WmsItem

    buckets: dict[str, list] = {}
    for item in session.scalars(
        select(WmsItem).where(WmsItem.fish_code != "").order_by(WmsItem.id)
    ).all():
        buckets.setdefault(str(item.fish_code or ""), []).append(item)

    out: list[dict] = []
    for code, rows in buckets.items():
        if not code:
            continue
        text = intake.order_time_of(rows)
        if not text:
            continue
        out.append({
            "fish_code": code,
            "order_time": text,
            "name": str(rows[0].product_name or rows[0].title or ""),
            "items": len(rows),
        })
    out.sort(key=lambda r: r["fish_code"])
    return out


def apply_rows(rows: list[dict]) -> dict:
    """逐款推给渔货栈（桥断只降级，失败不牵连别的款）。"""
    from wms import bridge

    stat = {"synced": 0, "skipped": 0, "failed": 0}
    for row in rows:
        try:
            report = bridge.push_profile_change(row["fish_code"], order_time=row["order_time"])
        except Exception:  # noqa: BLE001  桥断 / 桥上报错都不影响别的款
            stat["failed"] += 1
            continue
        if (report or {}).get("updated"):
            stat["synced"] += 1
        else:
            stat["skipped"] += 1
    return stat


def main() -> int:
    ap = argparse.ArgumentParser(description="渔集「下单时间」同步到渔货栈")
    ap.add_argument("--apply", action="store_true", help="真写（默认只预览）")
    args = ap.parse_args()

    from wms.db import SessionLocal, init_tables

    init_tables()

    print("=" * 64)
    print("下单时间同步", "（正式执行）" if args.apply else "（预演，加 --apply 才真写）")
    print("=" * 64)

    with SessionLocal() as s:
        rows = plan(s)
        print(f"有下单时间的款：{len(rows)} 款")
        for row in rows[:SAMPLE_LIMIT]:
            print(f"  {row['fish_code']}（{row['items']} 个条码商品）"
                  f" {row['order_time']} ← {row['name'][:28]}")
        if len(rows) > SAMPLE_LIMIT:
            print(f"  …还有 {len(rows) - SAMPLE_LIMIT} 款")
        if not args.apply or not rows:
            print("完成（没写库）。" if not args.apply else "没有要同步的。")
            return 0
        stat = apply_rows(rows)

    print(f"已同步：{stat['synced']} 款；渔货栈那边没这个商品（跳过）：{stat['skipped']} 款；"
          f"失败：{stat['failed']} 款")
    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
