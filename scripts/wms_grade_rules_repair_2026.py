"""渔集分级规则体检 + 合并规则 + C/D 系数归 0（2026-10-13 v1.0.33 一次性迁移）。

本次三件事（**改代码不会自动带上**，因为 `seed.ensure_grade_rules` 只在规则表
空着时才播种，线上早有规则）：

① **每个等级只留一条规则**（用户拍板，检测台截图里 C 出现 3 次、D 出现 2 次）：
   · C 的三条（安全红线 → 报修 / 有影响使用的功能故障 / 必须官方修）合成一条
     「C 报修：安全红线 / 功能故障 / 必须官方修」（priority 40，safety_flag=1）；
   · D 的两条（功能故障严重 ≥3 项 / 缺件严重 / 无维修价值）合成一条
     「D 报废：功能故障严重（3 项及以上）或缺件严重 / 无维修价值」（priority 50）；
   · 合并后的 C 规则带两道闸门（没勾缺件严重、坏不满三项），勾了缺件严重
     或坏满三项的会落到 D —— 判级结果与合并前**一字不差**，只是规则页不再一档多行。

② **C / D 价格系数全部为 0**（用户拍板：报修 / 报废不具备价值、不可售）：
   C 原来配的 0.55 / 0.70 直接置 0；D 无论原来是多少也置 0。
   注意「配置成 0」与「没配规则」是两回事 —— `grading.price_of` 已区分，
   没配规则的档仍按 1.0 兜底，配了 0 就是 0，不再被兜底逻辑吃掉。

③ **价格系数体检**（各档是否启用 + 阶梯倒挂）照旧跑一遍，N / S / A / B 用户
   自己填的值一个字不动。

幂等：全按「说明」判重，反复跑不会重复插。默认只预览，加 --apply 才写库。

用法（服务器上，容器内跑）::

    docker compose exec -T -e PYTHONPATH=/app -w /app app \\
        python3 /tmp/wms_grade_rules_repair_2026.py            # 真写库
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from wms.db import SessionLocal
from wms.models import WmsGradeRule, WmsPriceRule

# ---------------------------------------------------------------- 合并后的规则
MERGED_C_TITLE = "C 报修：安全红线 / 功能故障 / 必须官方修"
MERGED_C = {
    "category": "*",
    "priority": 40,
    "grade": "C",
    "safety_flag": True,
    "title": MERGED_C_TITLE,
    "reason": "先修不先废（2026-10-13）：修得好照样卖，修完重新评级；不具备价值、不可售",
    "conditions": {"all_of": [
        {"field": "missing_parts", "op": "==", "value": False},
        {"field": "fail_count", "op": "<", "value": 3},
    ], "any_of": [
        {"field": "safety_hit", "op": "==", "value": True},
        {"field": "functional_fail", "op": "==", "value": True},
        {"field": "must_official", "op": "==", "value": True},
    ]},
}
MERGED_D_TITLE = "D 报废：功能故障严重（3 项及以上）或缺件严重 / 无维修价值"
MERGED_D = {
    "category": "*",
    "priority": 50,
    "grade": "D",
    "title": MERGED_D_TITLE,
    "reason": "无维修价值，不具备价值、不可售，按拆机件逐件估值",
    "conditions": {"any_of": [
        {"field": "fail_count", "op": ">=", "value": 3},
        {"field": "missing_parts", "op": "==", "value": True},
    ]},
}

# 要删掉的旧规则（C 的三条 + D 的两条，含历史改名，全部并入上面两条）
GONE_TITLES = (
    # C 的三种来路
    "安全红线（电池鼓包 / 漏液、异味 / 焦糊 / 火烧、漏电 / 触电）",
    "安全红线（电池鼓包 / 漏液、异味 / 焦糊 / 火烧、漏电 / 触电）→ 报修",
    "安全红线（电池鼓包 / 漏液、异味 / 焦糊 / 火烧、漏电 / 触电） → 报修",
    "C 报修：有影响使用的功能故障",
    "C 报修：必须官方修",
    # D 的两种来路
    "D 报废：功能故障严重（3 项及以上功能没过）",
    "D 报废：缺件严重 / 无维修价值",
    # 更早的重复规则（2026-10-13 体检删过的，防有人又加回来）
    "B 缺/瑕疵/可卖：缺件、微瑕（非外观）不影响售卖使用",
    "B 小修：故障厂内可修",
    "B 可卖：厂内可修的小瑕疵，不影响使用",
)


def sync_rules(session, apply: bool) -> None:
    print("== 分级规则（每档只留一条）==")
    for title in GONE_TITLES:
        for gone in session.scalars(
                select(WmsGradeRule).where(WmsGradeRule.title == title)).all():
            print("  - 删「%s」（并入合并规则）" % title)
            if apply:
                session.delete(gone)
    for seed in (MERGED_C, MERGED_D):
        row = session.scalar(select(WmsGradeRule).where(WmsGradeRule.title == seed["title"]))
        if row is None:
            print("  + 新增「%s」（priority %s，%s 档）" % (seed["title"], seed["priority"], seed["grade"]))
            if apply:
                session.add(WmsGradeRule(**seed))
        else:
            same = (row.conditions or {}) == seed["conditions"] and row.grade == seed["grade"]
            print("  %s「%s」%s" % ("跳过（没变化）" if same else "更新", seed["title"],
                                  "" if same else "（条件 / 档位有出入，以脚本为准）"))
            if apply and not same:
                row.conditions = seed["conditions"]
                row.grade = seed["grade"]
                row.priority = seed["priority"]
                row.safety_flag = seed["safety_flag"]
    if apply:
        session.commit()
    # 终检：每个等级必须只出现一次
    grades = [r.grade for r in session.scalars(
        select(WmsGradeRule).order_by(WmsGradeRule.priority)).all()]
    dupes = sorted({g for g in grades if grades.count(g) > 1})
    print("  现存规则档位：%s%s" % (",".join(grades),
                                   ("　⚠ 重复档：%s" % ",".join(dupes)) if dupes else " ✓ 每档一条"))


def zero_cd_prices(session, apply: bool) -> None:
    print("\n== C / D 价格系数归 0（2026-10-13 用户拍板：不具备价值、不可售）==")
    for g in ("C", "D"):
        row = session.scalar(select(WmsPriceRule).where(WmsPriceRule.grade == g))
        if row is None:
            print("  ! %s 档没有系数行 —— 加 --fix-prices 会按 seed 默认补（也是 0）" % g)
            continue
        if float(row.factor or 0) == 0.0 and float(row.suggest_factor or 0) == 0.0 and row.enabled:
            print("  %s 档：已经是 0 ✓" % g)
            continue
        print("  ~ %s 档：系数 %s → 0、建议售价系数 %s → 0（原启用状态保留）"
              % (g, row.factor, row.suggest_factor))
        if apply:
            row.factor = 0.0
            row.suggest_factor = 0.0
            row.min_margin = 0.0
            row.need_approval_below = 0.0
            row.enabled = True
    if apply:
        session.commit()


def check_prices(session, apply: bool, fix: bool) -> None:
    print("\n== 价格系数体检 ==")
    rows = {str(r.grade or "").upper(): r for r in session.scalars(select(WmsPriceRule)).all()}
    ladder = [g for g in ("N", "S", "A", "B", "C", "D") if g in rows]
    problems = []
    for g in ladder:
        row = rows[g]
        flag = "启用" if row.enabled else "**停用**"
        print("  %s 档：渔货价 ×%s / 建议售价 ×%s · %s"
              % (g, row.factor, row.suggest_factor, flag))
        if not row.enabled and g not in ("C", "D"):
            problems.append("%s 档系数是停用的（按 1.0 兜底）" % g)
    print("  档位阶梯：" + " ≥ ".join("%s %s" % (g, rows[g].factor) for g in ladder))
    if problems:
        print("\n  发现 %d 处问题：" % len(problems))
        for p in problems:
            print("   - %s" % p)
        if not fix:
            print("   （要自动补缺失档就加 --fix-prices；停用的请在「分级规则」页自己开）")


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    fix = "--fix-prices" in argv
    s = SessionLocal()
    try:
        sync_rules(s, apply)
        zero_cd_prices(s, apply)
        check_prices(s, apply, fix)
        if not apply:
            print("\n（预览模式，没写库。加 --apply 才真改）")
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
