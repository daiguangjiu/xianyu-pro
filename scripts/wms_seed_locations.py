"""按现场实际摆位，把「区 → 货架 → 层」补齐（2026-10-10 用户给的布局）。

线上现状（写这个脚本时）：7 个仓与各自的区已经建好了（用户自己在页面上建的，
名字是「负一楼待检区」「可售区1区」「可售区2区」这种），可售区1 下面已经摆了
货架1~5 —— **所以这个脚本不新建区，只按现有区补齐货架与层**，并且沿用现场的
命名习惯（货架叫「货架1」，层叫「1 层」）。

要建的（只到第 4 级「层」，第 5 级「位」等现场真用到再补）：

  可售仓 第 1 个区（可售区1区）→ 5 个货架 × 4 层   ← 货架已建，补层
  可售仓 第 2 个区（可售区2区）→ 12 个货架 × 4 层
  待检仓 第 1 个区（负一楼待检区）→ 2 个货架 × 4 层
  维修仓 第 1 个区（负一楼维修区）→ 4 个货架 × 4 层
  其余 4 个仓（报修 / 拆机件 / 危废 / 退货暂存）按用户口径「默认就是一个区，
  没有再做下级分区」→ 一个都不动。

编码继续由 service 自动生成（``上级码-01``）；**名字随便改，编码别动** ——
货品 / 流水 / 批次 / 盘点单上都存着码。

用法（服务器上，容器内跑）::

    docker compose exec -T -e PYTHONPATH=/app -w /app app \\
        python3 /tmp/wms_seed_locations.py            # 只打印将要建什么
    docker compose exec -T -e PYTHONPATH=/app -w /app app \\
        python3 /tmp/wms_seed_locations.py --apply    # 真写库

重复执行安全：同一上级下已经有同名同级的一条就跳过，不会建出第二份。
"""
from __future__ import annotations

import sys

from sqlalchemy import func, select

from wms import warehouses
from wms.db import SessionLocal
from wms.models import WmsWarehouse

# 仓码 → 该仓下第几个区（1 起，按排序号）→ 货架数 / 每架层数
STRUCTURE = (
    ("SALE", 1, 5, 4),
    ("SALE", 2, 12, 4),
    ("PEND", 1, 2, 4),
    ("FIX", 1, 4, 4),
)

SKIPPED = "报修仓 / 拆机件仓 / 危废区 / 退货暂存区（按用户口径：各一个区，不再往下分）"


def _warehouse(session, code: str) -> WmsWarehouse | None:
    return session.scalar(
        select(WmsWarehouse).where(func.upper(WmsWarehouse.code) == code.upper())
    )


def _zones(session, wh_code: str) -> list[WmsWarehouse]:
    return list(
        session.scalars(
            select(WmsWarehouse)
            .where(
                WmsWarehouse.level == 2,
                func.upper(func.coalesce(WmsWarehouse.parent_code, "")) == wh_code.upper(),
            )
            .order_by(WmsWarehouse.sort.asc(), WmsWarehouse.id.asc())
        ).all()
    )


def _find(session, level: int, parent_code: str, name: str) -> WmsWarehouse | None:
    return session.scalar(
        select(WmsWarehouse).where(
            WmsWarehouse.level == level,
            func.upper(func.coalesce(WmsWarehouse.parent_code, "")) == str(parent_code or "").upper(),
            WmsWarehouse.name == name,
        )
    )


def _ensure(session, level: int, parent_code: str, name: str) -> tuple[WmsWarehouse, bool]:
    """有就直接用（不重复建），没有才建。返回 ``(行, 是不是新建的)``。"""
    hit = _find(session, level, parent_code, name)
    if hit is not None:
        return hit, False
    row, _msg = warehouses.save(session, name=name, level=level, parent_code=parent_code)
    return row, True


def run(apply: bool) -> int:
    session = SessionLocal()
    created = 0
    skipped = 0
    try:
        for wh_code, zone_idx, shelf_n, layer_n in STRUCTURE:
            wh = _warehouse(session, wh_code)
            if wh is None:
                print(f"!! 库里没有仓库 {wh_code}，跳过")
                continue
            zones = _zones(session, wh.code)
            if len(zones) < zone_idx:
                print(f"!! {wh.name} 下还没有第 {zone_idx} 个区，跳过（先去页面加一个）")
                continue
            zone = zones[zone_idx - 1]
            print(f"\n{zone.full_path or zone.name}（{zone.code}）→ {shelf_n} 个货架 × {layer_n} 层")
            if not apply:
                continue
            for i in range(1, shelf_n + 1):
                shelf, new = _ensure(session, 3, zone.code, f"货架{i}")
                created += 1 if new else 0
                skipped += 0 if new else 1
                for j in range(1, layer_n + 1):
                    _layer, new = _ensure(session, 4, shelf.code, f"{j} 层")
                    created += 1 if new else 0
                    skipped += 0 if new else 1
        print(f"\n没动的仓：{SKIPPED}")
        if apply:
            print(f"\n完成：新建 {created} 项，已存在跳过 {skipped} 项")
        else:
            print("\n这是预演（没写库），加 --apply 才真建")
        return 0
    finally:
        session.close()


def main(argv: list[str]) -> int:
    return run(apply="--apply" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
