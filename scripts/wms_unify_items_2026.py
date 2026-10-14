"""检测项统一成一套后的历史数据归并 + 规则同步（2026-10-13 一次性迁移）。

背景（用户 2026-10-13 拍板）：检测项**不再按品类分两套**（数码 11 项 / 家电 8 项），
统一成一套 10 项 + 3 项安全红线::

    appear 外观成色 / inner 内腔成色 / power 开机上电 / panel 屏幕面板显示 /
    touch 触控按键指纹人脸 / charge 充电接口电源线插头 / camera 摄像扬声器麦克风 /
    wifi 信号蓝牙定位 / function 核心功能与异响 / accessory 配件齐全
    + swell 电池鼓包漏液 / burn 异味焦糊火烧 / shock 漏电触电（安全红线）

两件事要做（**改代码不会自动带上**）：

1. 历史 ``wms_test_items`` 里存的是老 key（boot / screen / mic / plug / noise…），
   不改就是一群孤儿：「哪项最常坏」统计里出现页面上已经不存在的项。
   这里按映射表归并，同一条记录里多个老 key 并到同一个新 key 上的
   （如 camera + speaker + mic → camera）**留最严重的那一条**（fail 优先），其余删掉。

2. 分级规则：线上表里有规则时 ``seed.ensure_grade_rules`` 一条都不补，
   所以新口径的两条规则只能这样插：
   · 「B 小修（厂内可修）」加条件「且没有影响使用的故障」—— 真坏了得去修；
   · 新增「C 报修：有影响使用的功能故障」—— 以前真故障又没勾「必须官方修」
     时没有规则命中，会掉进兜底判 B（照常卖），这是最危险的一种漏判。

幂等：按 key 归并完就没有老 key 了；规则按「说明」判重，反复跑不会重复插入。
默认只预览，加 --apply 才写库。

用法（服务器上，容器内跑）::

    docker compose exec -T -e PYTHONPATH=/app -w /app app \\
        python3 /tmp/wms_unify_items_2026.py            # 真写库
    docker compose exec -T -e PYTHONPATH=/app -w /app app \\
        python3 /tmp/wms_unify_items_2026.py --dry-run  # 只看会发生什么
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from wms.db import SessionLocal
from wms.models import TEST_FAIL, WmsGradeRule, WmsTestItem

# ---------------------------------------------------------------- 1. 历史检项归并
# 老 key → 新 key（新 key 与 wms/inspection.py 的 UNIFIED_ITEMS / SAFETY_ITEMS 对齐）
OLD_TO_NEW: dict[str, str] = {
    "boot": "power",        # 开机 / 重启
    "power": "power",       # 通电启动（家电）
    "screen": "panel",      # 屏幕显示
    "panel": "panel",       # 面板 / 显示（家电）
    "touch": "touch",       # 触控
    "button": "touch",      # 按键 / 指纹人脸
    "charge": "charge",     # 充电 / 接口
    "plug": "charge",       # 电源线 / 插头
    "camera": "camera",     # 摄像头
    "speaker": "camera",    # 扬声器 / 听筒
    "mic": "camera",        # 麦克风 / 通话
    "wifi": "wifi",         # WiFi / 信号
    "bluetooth": "wifi",    # 蓝牙 / 定位
    "function": "function",  # 主要功能
    "noise": "function",    # 运行异响
    "safety": "shock",      # 安全检查（漏电 / 异味）→ 漏电 / 触电
    "accessory": "accessory",
    "appear": "appear",
}

NEW_LABELS: dict[str, str] = {
    "appear": "外观成色",
    "inner": "内腔成色",
    "power": "开机 / 上电启动",
    "panel": "屏幕 / 面板显示",
    "touch": "触控 / 按键 / 指纹人脸",
    "charge": "充电接口 / 电源线插头",
    "camera": "摄像头 / 扬声器 / 麦克风",
    "wifi": "WiFi / 信号 / 蓝牙 / 定位",
    "function": "核心功能 & 运行异响",
    "accessory": "配件齐全",
    "swell": "电池鼓包 / 漏液",
    "burn": "异味 / 焦糊 / 火烧痕迹",
    "shock": "漏电 / 触电",
}


def merge_items(apply: bool) -> dict:
    s = SessionLocal()
    try:
        old_keys = tuple(OLD_TO_NEW.keys())
        rows = s.scalars(
            select(WmsTestItem).where(WmsTestItem.item_key.in_(old_keys))
        ).all()
        plan: dict[str, int] = {}
        for r in rows:
            plan[r.item_key] = plan.get(r.item_key, 0) + 1
        print("待归并的历史检测项记录：")
        for k in old_keys:
            print("  %-10s → %-10s  %d 条" % (k, OLD_TO_NEW[k], plan.get(k, 0)))
        print("合计 %d 条" % len(rows))

        if not apply:
            print("\n（预览模式，没写库。加 --apply 才真改）")
            return {"planned": plan, "merged": 0, "dropped": 0}

        # 同一条记录里「新 key 已经存在」的行：(record_id, key) → 那一行
        existing: dict[tuple, WmsTestItem] = {}
        for r in s.scalars(
            select(WmsTestItem).where(WmsTestItem.item_key.in_(tuple(NEW_LABELS)))
        ).all():
            existing.setdefault((r.record_id, r.item_key), r)

        merged = dropped = 0
        for row in rows:
            new = OLD_TO_NEW[row.item_key]
            target = existing.get((row.record_id, new))
            if target is None:
                row.item_key = new
                row.item_label = NEW_LABELS[new]
                existing[(row.record_id, new)] = row
                merged += 1
                continue
            # 同一条记录里已经有这一项：留更严重的（fail 优先），老的那条删掉
            if str(row.result or "").lower() == TEST_FAIL and \
                    str(target.result or "").lower() != TEST_FAIL:
                target.result = row.result
                if (row.fail_desc or "").strip():
                    target.fail_desc = row.fail_desc
            s.delete(row)
            dropped += 1

        # 剩下的（含本来就是新 key 的）统一刷成现在这套文案
        renamed = 0
        for r in s.scalars(select(WmsTestItem)).all():
            label = NEW_LABELS.get(r.item_key)
            if label and r.item_label != label:
                r.item_label = label
                renamed += 1

        s.commit()
        print("\n已归并 %d 条、重复删掉 %d 条、文案刷新 %d 条"
              % (merged, dropped, renamed))
        return {"planned": plan, "merged": merged, "dropped": dropped}
    finally:
        s.close()


# ---------------------------------------------------------------- 2. 分级规则同步
B_TITLE = "B 缺/瑕疵/可卖：缺件、微瑕（非外观）不影响售卖使用"
C_TITLE = "C 报修：有影响使用的功能故障"
SAFETY_TITLE_OLD = "安全红线（鼓包 / 漏液 / 异味 / 漏电 / 焦糊 / 火烧）"
SAFETY_TITLE_NEW = "安全红线（电池鼓包 / 漏液、异味 / 焦糊 / 火烧、漏电 / 触电）"

# 2026-10-13 顺手堵的漏洞：三档「好货」以前只看「检项全过 + 零瑕疵」，
# 勾了「缺件严重」（该判 D）或「必须官方修」（该判 C）也照样判 S 完美 ——
# 检项确实全过，但这两个勾是人工判断，必须挡住。
N_TITLE = "N 全新：原封未拆 或 拆封未使用 + 全项通过 + 零瑕疵"
S_TITLE = "S 完美：全项通过 + 零瑕疵"
A_TITLE = "A 微瑕：功能全过 + 仅外观瑕疵"
_NOT_BAD = [
    {"field": "missing_parts", "op": "==", "value": False},
    {"field": "must_official", "op": "==", "value": False},
    {"field": "safety_hit", "op": "==", "value": False},
]
N_CONDITIONS = {"all_of": [
    {"field": "all_items_pass", "op": "==", "value": True},
    {"field": "flaw_count", "op": "==", "value": 0},
    *_NOT_BAD,
], "any_of": [
    {"field": "sealed", "op": "==", "value": True},
    {"field": "opened_unused", "op": "==", "value": True},
]}
S_CONDITIONS = {"all_of": [
    {"field": "all_items_pass", "op": "==", "value": True},
    {"field": "flaw_count", "op": "==", "value": 0},
    *_NOT_BAD,
]}
A_CONDITIONS = {"all_of": [
    {"field": "all_items_pass", "op": "==", "value": True},
    {"field": "flaw_count", "op": ">", "value": 0},
    *_NOT_BAD,
]}

SAFETY_CONDITIONS = {"any_of": [{"field": "safety_hit", "op": "==", "value": True}]}

B_CONDITIONS = {"all_of": [
    {"field": "inhouse_fixable", "op": "==", "value": True},
    {"field": "functional_fail", "op": "==", "value": False},
    {"field": "missing_parts", "op": "==", "value": False},
    {"field": "safety_hit", "op": "==", "value": False},
]}
C_CONDITIONS = {"all_of": [
    {"field": "functional_fail", "op": "==", "value": True},
    {"field": "safety_hit", "op": "==", "value": False},
    {"field": "missing_parts", "op": "==", "value": False},
]}


def sync_rules(apply: bool) -> dict:
    s = SessionLocal()
    try:
        rules = s.scalars(select(WmsGradeRule)).all()
        out = {"b_updated": 0, "c_created": 0, "safety_renamed": 0, "good_updated": 0,
               "safety_created": 0, "b_dropped": 0}
        b_row = next((r for r in rules if (r.title or "") == B_TITLE), None)
        c_row = next((r for r in rules if (r.title or "") == C_TITLE), None)
        safe_row = next((r for r in rules if (r.title or "") in
                         (SAFETY_TITLE_OLD, SAFETY_TITLE_NEW)), None)
        good_rows = [(t, c) for t, c in ((N_TITLE, N_CONDITIONS), (S_TITLE, S_CONDITIONS),
                                         (A_TITLE, A_CONDITIONS))
                     if any((r.title or "") == t for r in rules)]

        print("\n分级规则：")
        print("  「%s」%s" % (B_TITLE, "找到，将删除（重复规则，见 wms_grade_rules_repair_2026）"
                              if b_row else "已不存在，跳过"))
        print("  「%s」%s" % (C_TITLE, "已存在，跳过" if c_row else "将新增（priority 35）"))
        for title, _cond in good_rows:
            print("  「%s」将补上「且没勾缺件严重 / 必须官方修、没命中红线」" % title)
        if safe_row is None:
            # 线上这条可能被删过（判 D 一直靠引擎内置的红线兜底，功能没错），
            # 但规则页看不见就没法维护 —— 补一条，safety_flag=1 = 永远第一匹配。
            print("  安全红线规则**缺失**，将补一条（priority 1，永远第一匹配）")
        elif (safe_row.title or "") == SAFETY_TITLE_OLD:
            print("  安全红线规则改名：%s → %s" % (SAFETY_TITLE_OLD, SAFETY_TITLE_NEW))

        if not apply:
            return out

        for title, cond in good_rows:
            row = next(r for r in rules if (r.title or "") == title)
            row.conditions = cond
            out["good_updated"] += 1
        # 注意：这条「B 缺/瑕疵/可卖（priority 30）」后来在 2026-10-13 的体检里
        # **删掉了**（跟 priority 25 那条 B 同档同结果，纯重复）—— 规则同步现在归
        # scripts/wms_grade_rules_repair_2026.py 管，这里不再加回来。
        if b_row is not None:
            s.delete(b_row)
            out["b_dropped"] = 1
        if c_row is None:
            s.add(WmsGradeRule(
                category="*", priority=35, conditions=C_CONDITIONS, grade="C",
                title=C_TITLE,
                reason="功能项没过，需要报修 / 自修后再卖（2026-10-13 口径）",
                enabled=True,
            ))
            out["c_created"] = 1
        if safe_row is None:
            s.add(WmsGradeRule(
                category="*", priority=1, conditions=SAFETY_CONDITIONS, grade="D",
                safety_flag=True, title=SAFETY_TITLE_NEW,
                reason="命中安全红线，直接判 D 并转危废，禁止充电与流转",
                enabled=True,
            ))
            out["safety_created"] = 1
        elif (safe_row.title or "") == SAFETY_TITLE_OLD:
            safe_row.title = SAFETY_TITLE_NEW
            out["safety_renamed"] = 1
        s.commit()
        print("  已更新 %d 条、新增 %d 条、改名 %d 条"
              % (out["b_updated"], out["c_created"], out["safety_renamed"]))
        return out
    finally:
        s.close()


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    merge_items(apply)
    sync_rules(apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
