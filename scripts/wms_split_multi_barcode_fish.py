"""一次性脚本：把台账里多条码的渔货号全部拆开（2026-10-16 用户委托）。

口径（2026-10-15 拍板）：一个渔货号只对应一个条码商品 —— 一款名下挂了多个条码时，
最早建档那台留在原号（渔枢商品档案的商家编码锚定它，不动），其余每台各自拆出去
发一个新渔货号。逐台写审计（action=fish_code，note=多条码拆分），可追溯。

用法（容器内跑）：
    python3 scripts/wms_split_multi_barcode_fish.py            # 预览：列出要拆的款
    python3 scripts/wms_split_multi_barcode_fish.py --apply    # 真拆
    python3 scripts/wms_split_multi_barcode_fish.py --apply --only YS202610150001,YS202610150002

点名不拆的款走 ``wms.web.router._GOODS_NO_SPLIT``（当前 = YS202610120001）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from wms.db import SessionLocal, init_tables  # noqa: E402
from wms.models import WmsItem  # noqa: E402

# 与台账页同一份「暂不拆」名单（用户点名 YS202610120001 保持整款）
try:
    from wms.web.router import _GOODS_NO_SPLIT
except Exception:  # noqa: BLE001  独立环境兜底
    _GOODS_NO_SPLIT = {"YS202610120001"}


def groups() -> list[tuple[str, int]]:
    """渔货号 → 名下条码商品数（只看多码的，按条码数倒序）。"""
    with SessionLocal() as s:
        rows = (
            s.execute(
                select(WmsItem.fish_code, func.count(WmsItem.id))
                .where(WmsItem.fish_code != "")
                .group_by(WmsItem.fish_code)
                .having(func.count(WmsItem.id) > 1)
                .order_by(func.count(WmsItem.id).desc())
            )
            .all()
        )
    return [(str(code), int(cnt)) for code, cnt in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description="多条码渔货号批量拆分")
    ap.add_argument("--apply", action="store_true", help="真拆（默认只预览）")
    ap.add_argument("--only", default="", help="只拆这几个渔货号（逗号分隔）")
    args = ap.parse_args()

    init_tables()
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}
    todo = [
        (code, cnt)
        for code, cnt in groups()
        if cnt > 1 and code not in _GOODS_NO_SPLIT and (not only or code in only)
    ]
    skipped = [code for code, _ in groups() if code in _GOODS_NO_SPLIT]

    total_new = 0
    print(f"要拆的款：{len(todo)} 个（不拆名单：{sorted(_GOODS_NO_SPLIT)}"
          + (f"，其中在库里：{skipped}" if skipped else "") + "）")
    for code, cnt in todo:
        print(f"  {code}：{cnt} 个条码 → 1 台留原号 + {cnt - 1} 台拆出新号")
        if not args.apply:
            continue
        from wms import intake

        with SessionLocal() as s:
            new_codes = intake.split_fish(s, code, operator="script")
        total_new += len(new_codes)
        print(f"    → 拆出 {len(new_codes)} 个新渔货号：{'、'.join(new_codes[:6])}"
              + ("…" if len(new_codes) > 6 else ""))

    if not args.apply:
        print("\n（预览模式，没有改动任何数据 —— 加 --apply 真拆）")
    else:
        print(f"\n完成：拆了 {len(todo)} 款，发出 {total_new} 个新渔货号。"
              "渔货号页刷新即见（每号一个条码商品）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
