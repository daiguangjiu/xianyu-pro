"""安全红线 6 项收敛成 3 项后，把历史检测记录里的老 key 归并掉（2026-10-13 一次性迁移）。

2026-10-13 用户拍板：安全红线从 6 项（鼓包 / 漏液 / 异味 / 漏电 / 焦糊 / 火烧）
收敛成 3 项 —— **电池鼓包 / 漏液**、**异味 / 焦糊 / 火烧痕迹**、**漏电 / 触电**。

新 key 沿用了 swell / burn / shock 三个老名，历史记录里这三项不用动；
真正需要归并的是已经下线的三个 key::

    leak  （漏液）  → swell
    smell （异味）  → burn
    fire  （火烧）  → burn    （焦糊痕迹本来就归 burn）

不改的话，「哪项最常坏」那类按 item_key 聚合的统计会多出三条孤儿，
页面上出现「漏液 28 次」这种已经不存在的项。

幂等：老 key 归并完就没了，反复跑只会输出「没有需要归并的记录」。
默认只预览，加 --apply 才写库。

用法（服务器上，容器内跑）::

    docker compose exec -T -e PYTHONPATH=/app -w /app app \\
        python3 /tmp/wms_safety_items_merge.py            # 真写库
    docker compose exec -T -e PYTHONPATH=/app -w /app app \\
        python3 /tmp/wms_safety_items_merge.py --dry-run  # 只看会发生什么
"""
from __future__ import annotations

import sys

from sqlalchemy import func, select, text

from wms.db import SessionLocal
from wms.models import WmsTestItem

# 老 key → 新 key（与 wms/inspection.py 的 SAFETY_ITEMS 对齐）
MERGE_MAP: dict[str, str] = {
    "leak": "swell",
    "smell": "burn",
    "fire": "burn",
}

# 新三项的文案，归并后连 label 一起刷成现在的说法
NEW_LABELS: dict[str, str] = {
    "swell": "电池鼓包 / 漏液",
    "burn": "异味 / 焦糊 / 火烧痕迹",
    "shock": "漏电 / 触电",
}


def merge(apply: bool = False) -> dict:
    """把历史记录里已下线的红线项 key 归并到新 key 上，返回干了多少活。"""
    s = SessionLocal()
    try:
        old_keys = tuple(MERGE_MAP.keys())
        rows = s.execute(
            select(WmsTestItem.item_key, func.count(WmsTestItem.id))
            .where(WmsTestItem.item_key.in_(old_keys))
            .group_by(WmsTestItem.item_key)
        ).all()
        plan = {k: n for k, n in rows}
        total = sum(plan.values())

        print("待归并的历史检测项记录：")
        for k in old_keys:
            print("  %-8s → %-8s  %d 条" % (k, MERGE_MAP[k], plan.get(k, 0)))
        print("合计 %d 条" % total)

        if not apply:
            print("\n（预览模式，没写库。加 --apply 才真改）")
            return {"planned": plan, "total": total, "changed": 0}

        changed = 0
        for old, new in MERGE_MAP.items():
            # 归并前先看这台货在那条记录里有没有已经判过新 key ——
            # 同一次检测里老 key 与新 key 都在（历史不可能，守住即可），
            # 直接改会撞「同一记录同一项两条」，这里就跳过老的。
            dup = s.execute(
                text(
                    "select distinct record_id from wms_test_items "
                    "where item_key = :new and record_id in "
                    "(select record_id from wms_test_items where item_key = :old)"
                ),
                {"new": new, "old": old},
            ).scalars().all()
            if dup:
                s.execute(
                    text(
                        "delete from wms_test_items "
                        "where item_key = :old and record_id = :rid"
                    ),
                    [{"old": old, "rid": rid} for rid in dup],
                )
                print("  %s：%d 条与新 key 撞车，已删除" % (old, len(dup)))
            n = s.execute(
                text(
                    "update wms_test_items set item_key = :new, item_label = :label "
                    "where item_key = :old"
                ),
                {"new": new, "label": NEW_LABELS[new], "old": old},
            ).rowcount or 0
            changed += n
            print("  %-8s → %-8s  %d 条已归并" % (old, new, n))

        # 顺手把留下来的三项刷成新文案（老记录 label 是「电池鼓包」这种旧说法）
        for key, label in NEW_LABELS.items():
            n = s.execute(
                text(
                    "update wms_test_items set item_label = :label "
                    "where item_key = :key and item_label <> :label"
                ),
                {"label": label, "key": key},
            ).rowcount or 0
            if n:
                print("  %-8s 文案刷新 %d 条" % (key, n))

        s.commit()
        print("\n已归并 %d 条" % changed)
        return {"planned": plan, "total": total, "changed": changed}
    finally:
        s.close()


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    merge(apply=apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
