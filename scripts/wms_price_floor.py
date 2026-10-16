"""渔货价 / 建议售价历史数据向下取整（2026-10-16 一次性脚本）。

用户口径：**价格一律向下取整到元** —— ¥184.06 → ¥184.00。

  1. 新货：分级定价（市场价 × 该档系数）与人工填价，在 ``grading.floor_price`` 里取整，
     检测台 / 改价页 / 建档 / 编辑四条写入路径都走它（代码已改，本脚本不管新货）。
  2. 老货：库里有小数的渔货价（``selling_price``）与建议售价（``suggest_price``）
     批量取整，**只动有小数且取整后变小的那几台**。
  3. 同步：改完了按渔货号推给渔枢商品档案 + 渔货栈货源（``wms.bridge.push_price_change``），
     渔货栈「商品价格变动」通知里也能看到；桥断只降级不失败。

默认**只看不改**，加 ``--apply`` 才真写。

用法（容器里）::

    docker compose cp /tmp/wms_price_floor.py app:/tmp/x.py
    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 /tmp/x.py
    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 /tmp/x.py --apply
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

OPERATOR = "script:wms_price_floor"
# 一次预览最多打印几行
SAMPLE_LIMIT = 12


def _rows(session) -> list[dict]:
    """找出「渔货价 / 建议售价带小数」的货品，返回待改清单。"""
    from wms import grading
    from wms.models import WmsItem

    out: list[dict] = []
    for item in session.scalars(select(WmsItem).order_by(WmsItem.id)).all():
        old_sell = round(float(item.selling_price or 0.0), 2)
        old_sug = round(float(item.suggest_price or 0.0), 2)
        new_sell = grading.floor_price(old_sell)
        new_sug = grading.floor_price(old_sug)
        if abs(new_sell - old_sell) < 0.005 and abs(new_sug - old_sug) < 0.005:
            continue
        out.append({
            "item": item,
            "barcode": str(item.barcode or ""),
            "fish_code": str(item.fish_code or ""),
            "name": str(item.product_name or item.title or ""),
            "old_sell": old_sell,
            "new_sell": new_sell,
            "old_sug": old_sug,
            "new_sug": new_sug,
        })
    return out


def plan(session) -> list[dict]:
    """只读预检：返回待改清单（不写库）。"""
    return _rows(session)


def apply_rows(session, rows: list[dict]) -> dict:
    """逐台取整 + 同步渔枢 / 渔货栈。每台自己提交（一处失败不牵连别台）。"""
    from wms import audit, bridge

    stat = {"items": 0, "synced": 0, "failed": 0}
    for row in rows:
        item = row["item"]
        try:
            item.selling_price = row["new_sell"]
            item.suggest_price = row["new_sug"]
            session.add(item)
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            stat["failed"] += 1
            continue
        stat["items"] += 1
        # 审计留痕：为什么这台货的价格变了
        try:
            bits = []
            if abs(row["old_sell"] - row["new_sell"]) >= 0.005:
                bits.append(f"渔货价 ¥{row['old_sell']:.2f} → ¥{row['new_sell']:.2f}")
            if abs(row["old_sug"] - row["new_sug"]) >= 0.005:
                bits.append(f"建议售价 ¥{row['old_sug']:.2f} → ¥{row['new_sug']:.2f}")
            audit.log(session, "item", int(item.id), "price_floor",
                      after="；".join(bits) + "（向下取整到元）", operator=OPERATOR,
                      note="scripts/wms_price_floor.py")
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


def resync(session) -> dict:
    """把渔集当前的价格再推一次给渔枢 / 渔货栈。

    用处：渔枢商品的销售价在渔集「建议售价」为空时按「市场价 × 0.5」算 —— 老数据
    里这个兜底价是带小数的（¥404.55）。取整逻辑改完，老数据要靠这次重推才会变整数。
    """
    from wms import bridge
    from wms.models import WmsItem

    stat = {"pushed": 0, "updated": 0}
    for item in session.scalars(
        select(WmsItem).where(WmsItem.fish_code != "", WmsItem.fish_code.is_not(None))
    ).all():
        try:
            report = bridge.push_price_change(
                str(item.fish_code or ""),
                name=str(item.product_name or item.title or ""),
                selling_price=float(item.selling_price or 0.0),
                suggest_price=float(item.suggest_price or 0.0),
                base_price=float(item.base_price or 0.0),
            )
        except Exception:  # noqa: BLE001  桥断只降级
            continue
        stat["pushed"] += 1
        if (report or {}).get("updated"):
            stat["updated"] += 1
    return stat


def main() -> int:
    ap = argparse.ArgumentParser(description="渔货价 / 建议售价历史数据向下取整")
    ap.add_argument("--apply", action="store_true", help="真写（默认只预览）")
    ap.add_argument("--resync", action="store_true",
                    help="把渔集当前价格再推一次给渔枢 / 渔货栈（修老数据里带小数的兜底售价）")
    args = ap.parse_args()

    from wms.db import SessionLocal as WmsSession, init_tables

    init_tables()

    print("=" * 60)
    print("渔货价 / 建议售价向下取整", "（正式执行）" if args.apply else "（预演，加 --apply 才真写）")
    print("=" * 60)

    with WmsSession() as s:
        rows = plan(s)
        print(f"待取整：{len(rows)} 台（渔货价或建议售价带小数）")
        for row in rows[:SAMPLE_LIMIT]:
            print(f"  {row['barcode']} {row['fish_code']} "
                  f"渔货价 ¥{row['old_sell']:.2f} → ¥{row['new_sell']:.2f}"
                  + (f"；建议售价 ¥{row['old_sug']:.2f} → ¥{row['new_sug']:.2f}"
                     if abs(row['old_sug'] - row['new_sug']) >= 0.005 else ""))
        if len(rows) > SAMPLE_LIMIT:
            print(f"  …还有 {len(rows) - SAMPLE_LIMIT} 台")
        if args.resync:
            r = resync(s)
            print(f"③ 重推同步：{r['pushed']} 款，其中 {r['updated']} 款在渔枢有商品档案")
        if not args.apply or not rows:
            print("完成（没写库）。" if not args.apply else "没有要改的。")
            return 0
        stat = apply_rows(s, rows)

    print(f"已取整：{stat['items']} 台；同步到渔枢 / 渔货栈：{stat['synced']} 款；"
          f"失败：{stat['failed']} 台")
    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
