"""渔货号历史数据回填（2026-10-14 一次性脚本）。

做三件事，默认**只看不改**（加 ``--apply`` 才真写）：

  1. **给老货品补渔货号**：按商品名指纹认亲（同一款货自动聚到一个渔货号），
     渔货号的日期段用**这批货最早的建档日**（不是今天 —— 号里能看出什么时候进的货）。
  2. **把渔枢商品档案的关联键换成渔货号**：以前「商家编码」（``products.code``）
     存的是渔集那台货的条码，现在改成这一款的渔货号，并跟着
     · 重排 SKU 编码（``商家编码-01``，见 ``product.renumber_sku_codes``）；
     · 同步已上架到渔货栈那条货源的 ``item_code``。
     两台过去各自建档、其实是同一款货的（理论上同款就该并成一款）会**撞号**，
     脚本不硬并，报出来让人在渔枢里合并，第二个商品保持不动。
  3. ``--sync-stock``：把商品库存**校准成这一款在渔集的合计可售台数**
     （这就是用户要的「库存取这个货号的合集库存数」）。默认不跑，先看看差多少。

用法（容器里）：:

    docker compose cp /tmp/x.py app:/tmp/x.py
    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 /tmp/x.py
    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 /tmp/x.py --apply
    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 /tmp/x.py --apply --sync-stock
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

# 商品 / 货源同步过去时一次多少条提交一次
CHUNK = 100


def _wms_groups(db, intake, only_missing: bool = True):
    """把待补号的货品按「商品名指纹」聚成一堆一堆，返回 ``[(指纹, [货品...])]``。"""
    from wms.models import WmsItem

    stmt = select(WmsItem)
    if only_missing:
        # 兼容新列还没写出来时的 NULL
        stmt = stmt.where(
            (WmsItem.fish_code == "") | (WmsItem.fish_code.is_(None))
        )
    rows = list(db.scalars(stmt.order_by(WmsItem.id)).all())
    buckets: dict[str, list] = {}
    for item in rows:
        key = intake.fish_name_key(item.product_name or item.title)
        buckets.setdefault(key, []).append(item)
    # 没名字的货认不了亲，一台一个（放在最后，别掺进有名字的组里）
    nameless = buckets.pop("", [])
    out = [(k, v) for k, v in buckets.items()]
    for item in nameless:
        out.append((f"__solo_{item.id}", [item]))
    return out


def backfill_items(db, intake, apply: bool) -> dict:
    """① 给货品发渔货号。返回统计。"""
    from wms.models import WmsItem

    groups = _wms_groups(db, intake)
    index = intake.fish_index(db)          # 已经有号的那一批亲
    plan: list[tuple[object, str, bool]] = []   # (货品, 渔货号, 是不是新发)
    used: set[str] = set()                     # 本次已经发出去、还没落库的号
    for key, items in groups:
        head = items[0]
        if key in index and not key.startswith("__solo_"):
            fish = index[key]
            fresh = False
        else:
            # 批量补号时这一批还没写库 —— 把已经发出去的号一起算进流水，否则几百行
            # 会全挤在同一个渔货号上（next_fish_code 只会查库里已落地的最大号）
            fish = intake.next_fish_code(
                db, at=getattr(head, "created_at", None), used=used
            )
            used.add(fish)
            if not key.startswith("__solo_"):
                index[key] = fish
            fresh = True
        for it in items:
            plan.append((it, fish, fresh))

    stat = {"items": len(plan), "new_codes": 0, "reused": 0, "samples": []}
    fresh_codes = {p[1] for p in plan if p[2]}
    stat["new_codes"] = len(fresh_codes)
    stat["reused"] = len(plan) - sum(1 for p in plan if p[2])
    for _, fish, _ in plan[:5]:
        stat["samples"].append(fish)

    if apply:
        for item, fish, _ in plan:
            item.fish_code = fish
        db.commit()

    try:  # 留一条痕，以后查得出来这批号是这次补的
        from wms import audit

        audit.log(db, "item", 0, "fish_code_backfill",
                  after=f"补渔货号 {stat['items']} 台 / 新发 {stat['new_codes']} 个号",
                  operator="script", note="scripts/wms_fish_code_backfill.py")
        if apply:
            db.commit()
    except Exception:  # noqa: BLE001
        pass
    return stat


def _sync_supplier_code(db, old_code: str, new_code: str) -> int:
    """渔货栈那条货源的商品编码跟着改（`item_id` 是 hub-prd-<id>，不动）。"""
    try:
        from src.models import SupplierItem
    except Exception:  # noqa: BLE001
        return 0
    rows = list(db.scalars(
        select(SupplierItem).where(SupplierItem.item_code == old_code)
    ).all())
    for row in rows:
        row.item_code = new_code
    return len(rows)


def migrate_products(wdb, adb, apply: bool, sync_stock: bool) -> dict:
    """② 商家编码：条码 → 渔货号；③ 可选把库存校准成这一款的合计可售台数。"""
    from app.models import Product
    from app.services import product as product_service
    from wms import intake as wms_intake
    from wms.models import STATUS_SELLABLE, WmsItem

    stat = {"scanned": 0, "migrated": 0, "conflict": [], "no_item": [],
            "no_fish": [], "stock": [], "supplier": 0}
    for p in list(adb.scalars(select(Product).order_by(Product.id)).all()):
        stat["scanned"] += 1
        old = str(p.code or "").strip()
        if not old:
            continue
        item = wdb.scalar(select(WmsItem).where(WmsItem.barcode == old.upper()))
        if item is None:
            # 第二次跑进来时 code 已经是渔货号了 —— 按渔货号反查这一款的代表货品
            item = wdb.scalar(
                select(WmsItem).where(WmsItem.fish_code == old).order_by(WmsItem.id)
            )
        if item is None:
            # 不是渔集带出来的商品（自己建的 / 手改过的脏码）：不动它
            stat["no_item"].append(old)
            continue
        fish = str(item.fish_code or "").strip().upper()
        # 库存差异**先算**（换没换键都要算）—— 否则第二次跑进来（code 已经是渔货号）
        # 会在下面的 continue 里提前退出，--sync-stock 就永远校准不了
        total = 0
        for row in wms_intake.find_items_by_fish(wdb, fish):
            if row.status == STATUS_SELLABLE:
                total += int(row.qty or 0) or 1
        if sync_stock and total != int(p.stock or 0):
            stat["stock"].append(f"{p.name}：{int(p.stock or 0)} → {total}")
        if not fish or fish == old:
            stat["no_fish"].append(old)
            continue
        clash = adb.scalar(
            select(Product).where(Product.code == fish, Product.id != p.id)
        )
        if clash is not None:
            stat["conflict"].append(
                f"{p.name}（{old}）与「{clash.name}」其实是同一款货（渔货号 {fish}），"
                f"请在渔枢里合并后再跑一次"
            )
            continue
        stat["migrated"] += 1
        stat["supplier"] += _sync_supplier_code(adb, old, fish)
        if apply:
            p.code = fish
            product_service.renumber_sku_codes(adb, p)
            if sync_stock and total != int(p.stock or 0):
                p.stock = total
            adb.commit()
    return stat


def main() -> int:
    ap = argparse.ArgumentParser(description="渔货号历史数据回填")
    ap.add_argument("--apply", action="store_true", help="真写（默认只预览）")
    ap.add_argument("--sync-stock", action="store_true",
                    help="把商品库存校准成这一款在渔集的合计可售台数")
    args = ap.parse_args()

    from wms.db import SessionLocal as WmsSession, init_tables
    from wms import intake

    init_tables()                      # 保证 wms_items.fish_code 列在

    print("=" * 60)
    print("渔货号回填", "（正式执行）" if args.apply else "（预演，加 --apply 才真写）")
    print("=" * 60)

    with WmsSession() as wdb:
        # ② migrate 要用到补完号之后的 fish_code，所以顺序固定：先补号
        stat = backfill_items(wdb, intake, args.apply)

    print(f"① 货品补号：{stat['items']} 台待补 → 新发 {stat['new_codes']} 个渔货号，"
          f"并入已有 {stat['reused']} 台")
    print(f"   样例：{'、'.join(stat['samples']) or '—'}")

    from app.db import SessionLocal as AppSession

    with WmsSession() as wdb, AppSession() as adb:
        m = migrate_products(wdb, adb, args.apply, args.sync_stock)

    print(f"② 渔枢商品档案：扫了 {m['scanned']} 款，换键 {m['migrated']} 款，"
          f"货源 item_code 同步 {m['supplier']} 条")
    if m["no_item"]:
        print(f"   跳过（不是渔集条码 / 认不到货品）：{len(m['no_item'])} 款 —— "
              f"{'、'.join(m['no_item'][:5])}")
    if m["no_fish"]:
        print(f"   跳过（那台货还没渔货号）：{len(m['no_fish'])} 款")
    if m["conflict"]:
        print("   ⚠ 需要人工合并（两个渔枢商品其实是同一款货）：")
        for line in m["conflict"]:
            print("     -", line)
    if args.sync_stock:
        print(f"③ 库存校准：{len(m['stock'])} 款")
        for line in m["stock"][:10]:
            print("     -", line)
    elif m["migrated"]:
        print("   提示：加 --sync-stock 会把商品库存改成这一款在渔集的合计可售台数")
    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
