"""按商品名补一轮「型号 / 规格」（2026-10-16 一次性脚本）。

用户口径：**渔集自己根据商品信息补一轮型号、规格，能补的就补，补不了的保持现状；
补出来的要在渔枢 / 渔货栈的对应字段显示出来**。

规则（``wms/profile_guess.py``）：

  · **只补空缺** —— 已经有型号 / 规格的一律不动（人填的永远优先）；
  · **认不出来就留空** —— 猜错的型号比空着更害人（会贴到闲鱼描述里）；
  · 型号认「字母 + 数字混合串」（X100proUltra / BF-HL302 / MJ-PB40E254D）；
  · 规格认容量 / 尺寸 / 套装 / 颜色（4L、10 寸、橄榄绿、3 件套）；
  · 清单里沉淀的「型号 / 规格」列（``extra``）最可信，优先用。

补完按渔货号推给渔枢商品档案 + 渔货栈货源（``wms.bridge.push_profile_change``），
两边商品详情页的「型号 / 规格」两行立刻带得过去。

默认**只看不改**，加 ``--apply`` 才真写。

用法（容器里）::

    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 /app/scripts/wms_model_spec_guess.py
    docker compose exec -T -e PYTHONPATH=/app -w /app app python3 /app/scripts/wms_model_spec_guess.py --apply
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

OPERATOR = "script:wms_model_spec_guess"
SAMPLE_LIMIT = 20


def plan(session) -> list[dict]:
    """只读预检：返回「能补出点什么」的货品清单（补不出来的不进清单）。"""
    from wms import profile_guess
    from wms.models import WmsItem

    out: list[dict] = []
    for item in session.scalars(select(WmsItem).order_by(WmsItem.id)).all():
        got = profile_guess.fill_of(item)
        if not got["model"] and not got["spec"]:
            continue
        out.append({
            "item": item,
            "barcode": str(item.barcode or ""),
            "fish_code": str(item.fish_code or ""),
            "name": str(item.product_name or item.title or ""),
            "model": got["model"],
            "spec": got["spec"],
        })
    return out


def apply_rows(session, rows: list[dict]) -> dict:
    """逐台补 + 同步渔枢 / 渔货栈。每台自己提交（一处失败不牵连别台）。"""
    from wms import audit, bridge

    stat = {"items": 0, "synced": 0, "failed": 0}
    for row in rows:
        item = row["item"]
        bits = []
        if row["model"]:
            bits.append(f"型号：{row['model']}")
        if row["spec"]:
            bits.append(f"规格：{row['spec']}")
        try:
            if row["model"]:
                item.model = row["model"]
            if row["spec"]:
                item.spec = row["spec"]
            session.add(item)
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            stat["failed"] += 1
            continue
        stat["items"] += 1
        try:
            audit.log(
                session, "item", int(item.id), "profile_guess",
                after="；".join(bits), operator=OPERATOR,
                note="按商品名自动补型号 / 规格（scripts/wms_model_spec_guess.py）",
            )
            session.commit()
        except Exception:  # noqa: BLE001  留痕失败不影响资料
            session.rollback()
        if not row["fish_code"]:
            continue
        try:
            report = bridge.push_profile_change(
                row["fish_code"],
                brand=str(item.brand or ""),
                model=str(item.model or ""),
                spec=str(item.spec or ""),
            )
            if (report or {}).get("updated"):
                stat["synced"] += 1
        except Exception:  # noqa: BLE001  桥断只降级
            pass
    return stat


def main() -> int:
    ap = argparse.ArgumentParser(description="按商品名补一轮型号 / 规格")
    ap.add_argument("--apply", action="store_true", help="真写（默认只预览）")
    args = ap.parse_args()

    from wms.db import SessionLocal as WmsSession, init_tables

    init_tables()

    print("=" * 60)
    print("型号 / 规格补算", "（正式执行）" if args.apply else "（预演，加 --apply 才真写）")
    print("=" * 60)

    with WmsSession() as s:
        rows = plan(s)
        got_model = sum(1 for r in rows if r["model"])
        got_spec = sum(1 for r in rows if r["spec"])
        print(f"能补的：{len(rows)} 台（型号 {got_model} 台、规格 {got_spec} 台）")
        for row in rows[:SAMPLE_LIMIT]:
            print(f"  {row['barcode']} {row['fish_code']} "
                  f"型号「{row['model'] or '—'}」 规格「{row['spec'] or '—'}」 "
                  f"← {row['name'][:32]}")
        if len(rows) > SAMPLE_LIMIT:
            print(f"  …还有 {len(rows) - SAMPLE_LIMIT} 台")
        if not args.apply or not rows:
            print("完成（没写库）。" if not args.apply else "没有要补的。")
            return 0
        stat = apply_rows(s, rows)

    print(f"已补：{stat['items']} 台；同步到渔枢 / 渔货栈：{stat['synced']} 款；"
          f"失败：{stat['failed']} 台")
    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
