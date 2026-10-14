"""把货品退回「刚入库、还没检测」的原始数据状态（v1.0.11 应急/批量脚本）。

页面上的入口是**货品详情页 → 退回入库原始状态**；这个脚本是它的命令行孪生，
用于一次性处理若干台（比如用户 2026-10-08 点名的三台：
8617774630925303 / 8417787015020043 / 8617665947744253，它们在 wms_items.sku_code
与 extra 里都能命中）。

走的是同一个函数 ``wms.inspection.reset_to_inbound``：
状态回待检、回待检仓、等级回 W、渔货价清空、检测痕迹清掉；**检测记录一条不删**；
另记审计与库存流水，余额按等级重新归桶。

用法（服务器上，容器内跑）::

    docker compose exec -T -e PYTHONPATH=/app -w /app app \\
        python3 /tmp/wms_reset_to_inbound.py 8617774630925303 8417787015020043

不给货号就按上面那三台处理；加 ``--dry-run`` 只看看会发生什么、不写库。
"""
from __future__ import annotations

import sys

from sqlalchemy import cast, String, select

from wms import inspection
from wms.db import SessionLocal
from wms.models import WmsItem

DEFAULT_KEYS = ["8617774630925303", "8417787015020043", "8617665947744253"]
REASON = "退回入库的原始数据状态（脚本执行）"


def find_item(session, key: str) -> WmsItem | None:
    """货号 / 自己的码 / 序列号 / extra 里的任意文本都试着认一下。"""
    for column in (WmsItem.sku_code, WmsItem.own_barcode, WmsItem.serial_no, WmsItem.barcode):
        hit = session.scalar(select(WmsItem).where(column == key))
        if hit is not None:
            return hit
    return session.scalar(
        select(WmsItem).where(cast(WmsItem.extra, String).like("%" + key + "%"))
    )


def main(argv: list[str]) -> int:
    keys = [a for a in argv if not a.startswith("--")] or DEFAULT_KEYS
    dry = "--dry-run" in argv
    session = SessionLocal()
    for key in keys:
        item = find_item(session, key)
        if item is None:
            print("MISS ", key, "（没找到这台货）")
            continue
        before = (str(item.status), str(item.grade), float(item.selling_price or 0))
        if dry:
            print("DRY  ", key, item.barcode, item.display_code, "| 现在", *before)
            continue
        try:
            inspection.reset_to_inbound(session, item, operator="system", reason=REASON)
        except Exception as exc:  # noqa: BLE001 —— 一台失败不该拖住其它几台
            print("SKIP ", key, item.barcode, type(exc).__name__, exc)
            continue
        item = session.get(WmsItem, item.id)
        print("RESET", key, item.barcode, item.display_code,
              "| 之前", before[0], before[1], before[2],
              "-> 现在", item.status, item.grade, item.warehouse_code, item.selling_price)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
