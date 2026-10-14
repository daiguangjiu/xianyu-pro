"""补上「不影响使用的缺陷判 B 可卖」这条分级规则（2026-10-13 一次性迁移）。

起因（用户现场口径）：面板 / 显示有问题、少本说明书一个遥控器 —— **不影响使用**，
只是这台货本身降了点价值，照样是能卖的，就该判 **B 缺/瑕疵/可卖**。

以前没有这条规则：这两项判了「×」→ 必检项不全过 → S / A / N 全落空 →
掉到「没有规则完全命中，保守判 B（需人工确认）」。档位碰巧对了，但检测员看到的
是「未命中规则」四个字，只会以为系统没判明白，还得自己猜要不要报修。

为什么不靠启动播种：**``seed.ensure_grade_rules`` 只在规则表空着时播种一次**
（2026-10-10 起，免得现场删掉的内置规则被自动补回来）。线上表里早就有规则，
所以这条只能这样一次性插入。

幂等：按规则「说明」判重，已经存在就什么都不做（可以反复跑）。

用法（服务器上，容器内跑）::

    docker compose exec -T -e PYTHONPATH=/app -w /app app \\
        python3 /tmp/wms_minor_defect_rule.py            # 真写库
    docker compose exec -T -e PYTHONPATH=/app -w /app app \\
        python3 /tmp/wms_minor_defect_rule.py --dry-run  # 只看会发生什么
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from wms.db import SessionLocal
from wms.models import GRADE_B, WmsGradeRule

TITLE = "B 缺/瑕疵/可卖：只有不影响使用的缺陷（面板 / 显示、非关键配件）"
SEED = {
    "category": "*",
    "priority": 25,
    "grade": GRADE_B,
    "safety_flag": False,
    "title": TITLE,
    "reason": "面板 / 显示有问题或缺说明书遥控器这类非关键配件：不影响正常使用，降价值后照常售卖",
    "conditions": {
        "all_of": [
            {"field": "minor_defect", "op": "==", "value": True},
            {"field": "functional_fail", "op": "==", "value": False},
            {"field": "missing_parts", "op": "==", "value": False},
            {"field": "must_official", "op": "==", "value": False},
        ]
    },
}


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    session = SessionLocal()
    try:
        exists = session.scalar(select(WmsGradeRule).where(WmsGradeRule.title == TITLE))
        if exists is not None:
            print(f"已经存在（id={exists.id} priority={exists.priority}）：无需重复插入")
            print("RULE_READY=1 RULE_ID=%s" % exists.id)
            return 0
        rows = session.scalars(
            select(WmsGradeRule).order_by(WmsGradeRule.priority.asc())
        ).all()
        print("现有规则：")
        for r in rows:
            print(f"  priority={r.priority} grade={r.grade} {r.title}")
        print(f"\n准备插入：priority={SEED['priority']} grade={SEED['grade']} {TITLE}")
        if dry:
            print("（--dry-run，没写库）")
            return 0
        row = WmsGradeRule(**SEED)
        session.add(row)
        session.commit()
        print(f"已插入：id={row.id}")
        print(f"RULE_READY=1 RULE_ID={row.id}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
