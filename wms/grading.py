"""分级引擎 + 定价：规则存在库里，改分级标准**不改代码**。

引擎必须返回「依据」，不能只返回档位 —— 检测员不知道为什么就无法学习与复核。
人工覆盖引擎建议时**必须填理由**（那是规则调优的唯一依据：长期与引擎不符，
说明规则该改了，而不是人该听系统的）。

facts 字段（检测工作台算好后传进来）：
  all_items_pass   必检项是否全过
  fail_count       不合格项数
  flaw_count       外观瑕疵处数
  safety_hit       是否命中安全红线
  inhouse_fixable  故障是否在「厂内可修清单」里
  must_official    是否命中「必须官方修清单」
  repair_cost      预估修复成本
  repair_value     修后价值
  missing_parts    是否缺件严重
"""
from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    GRADE_A,
    GRADE_B,
    GRADE_C,
    GRADE_D,
    GRADE_N,
    GRADE_S,
    GRADE_W,
    WmsGradeRule,
    WmsPriceRule,
    grade_label,
)

def floor_price(value) -> float:
    """价格向下取整到元（2026-10-16 用户拍板）：¥184.06 → ¥184.00。

    全渔集统一走这一个函数（分级定价、人工改价、建档 / 编辑填价、历史数据批处理），
    免得各处各写一份取整、价格对不上。小数是浮点算出来的（基准价 × 系数），
    先按 6 位小数抹掉浮点误差再取整，避免 184.00000001 这种被判成 185。
    """
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return float(math.floor(round(amount, 6)))


def manual_suggest_of(item) -> float | None:
    """这台货的「建议售价」算不算**人工值**（2026-10-16）。

    系统按该档系数自动算出来的（``suggest_auto=True``）下次定级要跟着新档重算 ——
    不然老档算出来的建议售价会把新档的渔货价压住（渔货价不得高于建议售价）；
    **人填的一律尊重**（人工值优先于系数），返回它本身。
    """
    if item is None:
        return None
    if getattr(item, "suggest_auto", False):
        return None
    return float(item.suggest_price or 0.0) or None


# 检测定级时**会写渔货价**的档位（其余档位屏幕上的数字只是参考）：
# N 全新 / S 完美 / A 微瑕直接上架；B 修完重判、C/D 走报修与拆机件估值。
# 检测台与货品详情页改价都按这套口径，别各写一份。
PRICED_GRADES = (GRADE_N, GRADE_S, GRADE_A, GRADE_B)

# ------------------------------------------------------------------ 条件的「人话」
# 2026-10-10：页面上原来只甩一句 JSON 加一串英文字段名（battery_health / all_of…），
# 看规则的人根本读不懂。这里把字段名、运算符、示例都登记成中文：页面用它渲染
# 下拉（不懂 JSON 也能拼条件），并把库里的 JSON 翻回一句话给人看。
CONDITION_FIELDS: tuple[dict, ...] = (
    # 电池健康度已于 2026-10-11 下线（现场：主营小家电，电池饱和度是手机品类专属，
    # 检测台不再采集、规则里也不再给这一项）。老规则里万一还留着，仍能正常显示与判级。
    {"field": "all_items_pass", "label": "必检项是否全过", "kind": "bool",
     "hint": "检测项里有没有没过的"},
    {"field": "fail_count", "label": "不合格项数", "kind": "number", "unit": " 项", "hint": "功能项没过的个数"},
    {"field": "flaw_count", "label": "外观瑕疵处数", "kind": "number", "unit": " 处", "hint": "划痕 / 磕碰记了几处"},
    {"field": "safety_hit", "label": "是否命中安全红线", "kind": "bool",
     "hint": "鼓包 / 漏液 / 异味 / 漏电 / 焦糊 / 火烧"},
    {"field": "inhouse_fixable", "label": "故障是否厂内可修", "kind": "bool",
     "hint": "在「厂内可修清单」里就算可修"},
    {"field": "must_official", "label": "是否必须官方修", "kind": "bool",
     "hint": "命中「必须官方修清单」"},
    {"field": "sealed", "label": "是否原封未拆", "kind": "bool",
     "hint": "原厂封条 / 塑封完好，没开过封（全新）"},
    {"field": "opened_unused", "label": "是否拆封未使用", "kind": "bool",
     "hint": "开过封但没用过：无使用痕迹、配件齐全（拆封全新）"},
    {"field": "missing_parts", "label": "是否严重缺件", "kind": "bool", "hint": "缺件到没法正常用"},
    # 2026-10-13：面板 / 显示有划痕缺笔画、少说明书遥控器这类「不影响使用」的缺陷
    # 与真正影响使用的功能故障分开判 —— 前者是 B（可卖），后者才走维修 / 报修 / 报废
    {"field": "minor_defect", "label": "是否有不影响使用的缺陷", "kind": "bool",
     "hint": "面板 / 显示有问题、缺非关键配件（说明书 / 遥控器）"},
    # 2026-10-13 统一检测项：外壳成色 / 内腔成色单独算「外观类」——
    # 判 × 只降成色走 A 微瑕，不算功能故障（与 minor_defect 的 B 可卖区分开）
    {"field": "cosmetic_defect", "label": "是否有外观 / 成色瑕疵", "kind": "bool",
     "hint": "外壳成色、内腔成色这两项里有没有判 × 的"},
    {"field": "functional_fail", "label": "是否有影响使用的故障", "kind": "bool",
     "hint": "除上面那两项之外的功能项没过"},
    {"field": "repair_cost", "label": "预估修复成本", "kind": "number", "unit": " 元"},
    {"field": "repair_value", "label": "修后价值", "kind": "number", "unit": " 元"},
)

# 已经下线、但老规则里可能还写着的条件项：只留「翻成人话」用的中文名
# （页面下拉里不会再出现，也就没法再新建这类条件了）
LEGACY_FIELD_LABELS: dict[str, str] = {"battery_health": "电池健康度"}

FIELD_LABELS: dict[str, str] = {
    **{f["field"]: f["label"] for f in CONDITION_FIELDS}, **LEGACY_FIELD_LABELS,
}
FIELD_KINDS: dict[str, str] = {f["field"]: f["kind"] for f in CONDITION_FIELDS}

# 代码里的运算符 ↔ 给人看的符号
OP_SYMBOL: dict[str, str] = {
    ">=": "≥", "<=": "≤", ">": ">", "<": "<", "==": "=", "!=": "≠",
    "in": "属于", "not_in": "不属于",
}

# 页面下拉用：value 是写进 JSON 的运算符，label 是人话
CONDITION_OPS: tuple[tuple[str, str], ...] = (
    (">=", "大于等于 ≥"),
    ("<=", "小于等于 ≤"),
    (">", "大于 >"),
    ("<", "小于 <"),
    ("==", "等于 ="),
    ("!=", "不等于 ≠"),
)

CONDITION_EXAMPLES: tuple[dict, ...] = (
    {
        "title": "N 全新：原封未拆 或 拆封未使用 + 全项通过 + 零瑕疵",
        # grade / priority / safety 是给页面的：点「照抄」时**连档位、优先级、规则说明一起填上**
        # （2026-10-11）。以前只填条件 → 在「D 报废」的编辑页里照抄 N 的条件，看着条条都对，
        # 档位却还是 D，现场会以为「条件满足就该判 N、系统没反应」。
        "grade": GRADE_N,
        "priority": 5,
        "json": {"all_of": [
            {"field": "all_items_pass", "op": "==", "value": True},
            {"field": "flaw_count", "op": "==", "value": 0},
            {"field": "missing_parts", "op": "==", "value": False},
            {"field": "must_official", "op": "==", "value": False},
            {"field": "safety_hit", "op": "==", "value": False},
        ], "any_of": [
            {"field": "sealed", "op": "==", "value": True},
            {"field": "opened_unused", "op": "==", "value": True},
        ]},
    },
    {
        "title": "S 完美：全项通过 + 零瑕疵",
        "grade": GRADE_S,
        "priority": 10,
        "json": {"all_of": [
            {"field": "all_items_pass", "op": "==", "value": True},
            {"field": "flaw_count", "op": "==", "value": 0},
            {"field": "missing_parts", "op": "==", "value": False},
            {"field": "must_official", "op": "==", "value": False},
            {"field": "safety_hit", "op": "==", "value": False},
        ]},
    },
    {
        "title": "A 微瑕：全项通过 + 有外观瑕疵",
        "grade": GRADE_A,
        "priority": 20,
        "json": {"all_of": [
            {"field": "all_items_pass", "op": "==", "value": True},
            {"field": "flaw_count", "op": ">", "value": 0},
            {"field": "missing_parts", "op": "==", "value": False},
            {"field": "must_official", "op": "==", "value": False},
            {"field": "safety_hit", "op": "==", "value": False},
        ]},
    },
    {
        # 2026-10-13 用户拍板：面板显示有问题、少个说明书 —— 不影响使用，
        # 只是降了本身的价值，照常上架卖，判 B（可卖）
        "title": "B 缺/瑕疵/可卖：只有不影响使用的缺陷（面板 / 显示、非关键配件）",
        "grade": GRADE_B,
        "priority": 25,
        "json": {"all_of": [
            {"field": "minor_defect", "op": "==", "value": True},
            {"field": "functional_fail", "op": "==", "value": False},
            {"field": "missing_parts", "op": "==", "value": False},
            {"field": "must_official", "op": "==", "value": False},
        ]},
    },
    {
        # 2026-10-13 用户拍板「每个等级只留一条」：C 的三种来路合一条、D 的两种来路合一条。
        "title": "C 报修：安全红线 / 功能故障 / 必须官方修",
        "grade": GRADE_C,
        "priority": 40,
        "safety": True,
        "json": {"all_of": [
            {"field": "missing_parts", "op": "==", "value": False},
            {"field": "fail_count", "op": "<", "value": 3},
        ], "any_of": [
            {"field": "safety_hit", "op": "==", "value": True},
            {"field": "functional_fail", "op": "==", "value": True},
            {"field": "must_official", "op": "==", "value": True},
        ]},
    },
    {
        "title": "D 报废：功能故障严重（3 项及以上）或缺件严重 / 无维修价值",
        "grade": GRADE_D,
        "priority": 50,
        "json": {"any_of": [
            {"field": "fail_count", "op": ">=", "value": 3},
            {"field": "missing_parts", "op": "==", "value": True},
        ]},
    },
)


def _value_text(field: str, value) -> str:
    kind = FIELD_KINDS.get(str(field or ""), "")
    if kind == "bool" or isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "空"
    if str(field or "") == "battery_health":
        return f"{value}%"
    return str(value)


def conditions_text(conditions) -> str:
    """把 conditions 这个 JSON 翻成一句人话（列表的「条件」列与提示条都用它）。

    老页面上那一列直接印 ``{'all_of': [{'field': 'battery_health', ...}]}``，
    只有写它的人看得懂。现在印「全部满足：电池健康度 ≥ 85%」。
    """
    if not isinstance(conditions, dict):
        return str(conditions or "")
    parts: list[str] = []
    for key, lead in (("all_of", "全部满足"), ("any_of", "满足任一")):
        texts = []
        for cond in conditions.get(key) or []:
            if not isinstance(cond, dict):
                continue
            field = str(cond.get("field") or "")
            label = FIELD_LABELS.get(field) or field or "?"
            op = OP_SYMBOL.get(str(cond.get("op") or "").strip(), "=")
            texts.append(f"{label} {op} {_value_text(field, cond.get('value'))}")
        if texts:
            parts.append(f"{lead}：{'、'.join(texts)}")
    if not parts:
        return "没写条件（前面几条都没命中，就判这一档）"
    return "；".join(parts)


# ------------------------------------------------------------------ 规则体检（2026-10-11）
# 现场问：「照抄的 S 完美 有电池这一条 —— 把各档的条件项统一一下？」
# （2026-10-11 结论：电池健康度整项下线，各档改成同一套条件项，不再有电池）
# 这一节就是**循环所有规则**，把「每条用了哪些条件项」摆成一张对照表，并挑出三类毛病：
#   ① 条件里用了已下线的条件项（电池健康度）→ 那一条永远不满足，会挡着这一档；
#   ② 两条规则条件一模一样、档位却不同 → 判级只命中先匹配的那条，另一条永远轮不到；
#   ③ 空条件（兜底档）排在别的规则前面 → 后面的规则永远轮不到。
# 顺带回答「条件对不对」：条件与**别的档位**的内置口径一模一样时，多半是抄了条件没改档位。
CHECK_FIELDS: tuple[str, ...] = (
    "all_items_pass", "flaw_count", "sealed", "opened_unused",
    "inhouse_fixable", "must_official", "missing_parts", "safety_hit",
    "minor_defect", "functional_fail", "cosmetic_defect",
)

# 已经下线、检测台不再采集的条件项 —— 老规则里还留着的话，那一条永远不满足
DEPRECATED_FIELDS: dict[str, str] = {
    "battery_health": "电池健康度（2026-10-11 下线：主营小家电，电池是手机品类专属）",
}


def deprecated_note(conditions) -> str:
    """用了已下线的条件项就提醒清理（空串 = 没用到）。"""
    hits = [text for field, text in DEPRECATED_FIELDS.items() if _cell_text(conditions, field)]
    if not hits:
        return ""
    return ("这条规则还在用已经下线的「" + "」「".join(hits) + "」—— "
            "检测台已经不采这一项了，等于永远不满足、会挡着这一档，点「编辑」把它删掉")

# 对照表的表头用短名（列多，长名会把表撑爆）
CHECK_LABELS: dict[str, str] = {
    "all_items_pass": "必检项全过",
    "flaw_count": "外观瑕疵",
    "sealed": "原封未拆",
    "opened_unused": "拆封未使用",
    "inhouse_fixable": "厂内可修",
    "must_official": "必须官方修",
    "missing_parts": "缺件严重",
    "safety_hit": "安全红线",
    "minor_defect": "不影响用的缺陷",
    "functional_fail": "影响用的故障",
    "cosmetic_defect": "外观/成色瑕疵",
}


def _cell_text(conditions, field: str) -> str:
    """这条规则里「某个条件项」是怎么写的：``且=是`` / ``或≥85%`` / 空串（没写这一项）。"""
    if not isinstance(conditions, dict):
        return ""
    out: list[str] = []
    for key, lead in (("all_of", "且"), ("any_of", "或")):
        for cond in conditions.get(key) or []:
            if isinstance(cond, dict) and str(cond.get("field") or "") == field:
                op = OP_SYMBOL.get(str(cond.get("op") or "").strip(), "=")
                out.append(f"{lead}{op}{_value_text(field, cond.get('value'))}")
    return "/".join(out)


def conditions_key(conditions) -> str:
    """条件归一化成一个字符串，只用来判「两条规则的条件是不是一模一样」。"""
    if not isinstance(conditions, dict):
        return ""
    parts: list[str] = []
    for key in ("all_of", "any_of"):
        items = []
        for cond in conditions.get(key) or []:
            if not isinstance(cond, dict):
                continue
            items.append(f"{cond.get('field')}|{cond.get('op')}|{cond.get('value')!r}")
        if items:
            parts.append(key + ":" + ",".join(sorted(items)))
    return " & ".join(parts)


def _canonical_conditions() -> list[tuple[str, str, dict]]:
    """内置口径（播种）的「档位 → 条件」：抓「条件抄了别档、档位没改」。"""
    try:
        from .seed import GRADE_RULE_SEED  # 局部 import：只有体检 / 保存校验才用得上
    except Exception:  # pragma: no cover - 播种表读不到不该拖垮页面
        return []
    return [
        (str(row.get("grade") or "").upper(), str(row.get("title") or ""), row.get("conditions") or {})
        for row in GRADE_RULE_SEED
    ]


def mismatch_note(conditions, grade, safety: bool = False) -> str:
    """条件与**别的档位**一模一样时给一句提醒（空串 = 没看出问题）。

    这就是「判断条件是否对」：命中档位是 D，条件却跟「N 全新」一字不差 ——
    这条规则的意思会变成「满足这些条件的判 D」，几乎肯定是照抄时没改档位。
    """
    key = conditions_key(conditions)
    if not key:
        return ""
    want = str(grade or "").upper()
    for ex in CONDITION_EXAMPLES:
        if conditions_key(ex.get("json")) == key and str(ex.get("grade") or "").upper() != want:
            other = grade_label(str(ex.get("grade") or ""))
            return (f"条件与「{ex.get('title')}」一模一样，命中档位却是「{grade_label(want)}」—— "
                    f"这条的意思变成「满足这些条件的判 {grade_label(want)}」（那条示例是判 {other}），"
                    f"确认下是不是抄错了地方")
    for canon_grade, canon_title, conds in _canonical_conditions():
        if canon_grade and canon_grade != want and conditions_key(conds) == key:
            return (f"条件与内置的「{canon_title}」（{grade_label(canon_grade)} 档）一模一样，"
                    f"命中档位却是「{grade_label(want)}」—— 确认下是不是抄错了地方")
    return ""


# 「说明里提到的词 → 对应的条件项」：说明（title / reason）提了某项、条件里却没写，
# 就是「说明与条件各说各话」—— 检测员按说明理解、引擎按条件判，现场最容易吵起来。
_TITLE_HINTS: tuple[tuple[str, str], ...] = (
    ("原封", "sealed"),
    ("拆封", "opened_unused"),
    ("缺件", "missing_parts"),
    ("厂内可修", "inhouse_fixable"),
    ("官方修", "must_official"),
    ("安全红线", "safety_hit"),
    ("必检", "all_items_pass"),
    ("瑕疵", "flaw_count"),
)


def title_note(rule) -> str:
    """「规则说明」写了某一项、条件里却没写 → 提醒一句（空串 = 对得上）。

    「全项通过（all_items_pass）」本身就已经蕴含「零瑕疵 / 功能全过」——
    说明里写「零瑕疵」而条件里只有 all_items_pass 不算各说各话，别误报
    （2026-10-13 用户截图：S 规则被这条误报顶出「体检发现 1 处处理」黄条）。
    """
    words = str(getattr(rule, "title", "") or "") + " " + str(getattr(rule, "reason", "") or "")
    conds = rule.conditions if isinstance(rule.conditions, dict) else {}
    covered_by_all_pass = bool(_cell_text(conds, "all_items_pass"))
    # 「瑕疵 / 缺陷」这类词在 B 档说明里是泛指（缺/瑕疵/可卖 —— 有瑕疵但照常卖），
    # 条件是用 minor_defect（不影响使用的缺陷）表达的，不算各说各话
    covered_by_minor = bool(_cell_text(conds, "minor_defect"))
    missing = [
        kw for kw, field in _TITLE_HINTS
        if kw in words and not _cell_text(conds, field)
        and not (field == "flaw_count" and (covered_by_all_pass or covered_by_minor))
    ]
    if not missing:
        return ""
    return ("「规则说明 / 依据」里提到了「" + "」「".join(missing)
            + "」，但条件里没有这一项 —— 说明与条件对不上：按条件判级时这一项不生效。"
            + "要么把条件补上，要么把说明改掉，别让它俩各说各话")


def _rule_name(rule) -> str:
    return f"「{rule.title or grade_label(rule.grade)}」（优先级 {rule.priority}）"


def rule_checklist(rules) -> dict:
    """规则体检：逐条列出用到的条件项 + 挑毛病（页面「规则体检」卡用它）。"""
    rows = []
    for rule in rules or []:
        conds = rule.conditions if isinstance(rule.conditions, dict) else {}
        note = " ".join(t for t in (
            mismatch_note(conds, rule.grade, bool(rule.safety_flag)),
            title_note(rule),
            deprecated_note(conds),
        ) if t)
        rows.append({
            "rule": rule,
            "cells": [_cell_text(conds, f) for f in CHECK_FIELDS],
            "note": note,
        })
    return {
        "fields": [(f, CHECK_LABELS.get(f, FIELD_LABELS.get(f, f))) for f in CHECK_FIELDS],
        "rows": rows,
        "notes": _check_notes(rules),
        # 单条规则的毛病（抄错档位 / 说明与条件对不上）：页面在对照表里逐条列出来
        "row_notes": [r for r in rows if r["note"]],
    }


def _check_notes(rules) -> list[str]:
    live = [r for r in (rules or []) if r.enabled]
    notes: list[str] = []
    # 条件一模一样却不同档位（抄了条件没改档位的典型）
    grouped: dict[str, list] = {}
    for r in live:
        key = conditions_key(r.conditions or {})
        if key:
            grouped.setdefault(key, []).append(r)
    for group in grouped.values():
        if len(group) < 2:
            continue
        if len({str(r.grade or "").upper() for r in group}) < 2:
            continue  # 同档位重影在规则表里已经标了「同档还有 N 条」
        notes.append(
            "这些规则的条件一模一样、档位却不同（"
            + "、".join(f"{_rule_name(r)} → {grade_label(r.grade)}" for r in group)
            + "）：判级只会命中先匹配的那一条，另一条永远轮不到 —— 多半是抄了条件没改档位"
        )
    # 兜底档（没写条件）排在别人前面：后面那几条永远轮不到
    for idx, rule in enumerate(live):
        if conditions_key(rule.conditions or {}):
            continue
        later = live[idx + 1:]
        if later:
            notes.append(
                f"{_rule_name(rule)} 没写条件 = 兜底档（前面几条都没命中才判它），"
                f"但它排在 " + "、".join(_rule_name(r) for r in later)
                + " 前面 —— 后面这几条永远轮不到"
            )
    return notes


def _compare(left, op: str, right) -> bool:
    op = str(op or "").strip()
    try:
        if op == ">=":
            return left >= right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == "<":
            return left < right
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == "in":
            return left in (right if isinstance(right, (list, tuple)) else [right])
        if op == "not_in":
            return left not in (right if isinstance(right, (list, tuple)) else [right])
    except TypeError:
        return False
    return False


def _fact(facts: dict, field: str):
    value = facts.get(field)
    # 布尔字段缺了就当 False（2026-10-13）：S / A / N 三档都带了「没勾缺件严重、
    # 没勾必须官方修、没命中红线」这类排除条件，而老调用方传进来的 facts 可能
    # 压根没有这几个键 —— 缺键不等于「勾了」，按 False 处理才不会把一台好货
    # 卡在这几条上（表现为「没有规则命中 → 兜底判 B」）。
    if FIELD_KINDS.get(str(field or "")) == "bool" and value in (None, ""):
        return False
    if field == "battery_health" and value in (None, ""):
        # 电池项已下线（2026-10-11），检测台不再采集 —— 老规则里万一还留着电池条件，
        # 当成「不参与判级」（按满电算），别把一台好好的货卡在这一条上。
        return 100
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    return value


def _match(conditions: dict, facts: dict) -> bool:
    """conditions: {"all_of":[...], "any_of":[...]}，all_of 全中且 any_of 至少中一条。"""
    if not isinstance(conditions, dict):
        return False
    all_of = conditions.get("all_of") or []
    any_of = conditions.get("any_of") or []
    for cond in all_of:
        if not _compare(_fact(facts, cond.get("field")), cond.get("op"), cond.get("value")):
            return False
    if any_of:
        return any(
            _compare(_fact(facts, cond.get("field")), cond.get("op"), cond.get("value"))
            for cond in any_of
        )
    return True


def _effective(rule: WmsGradeRule, at=None) -> bool:
    """生效期：换标准时历史数据按当时规则保留，不被追溯重判。"""
    if rule.effective_from and at and at < rule.effective_from:
        return False
    if rule.effective_to and at and at > rule.effective_to:
        return False
    return True


def _rule_depth(rule_cat: str, cat: str) -> int:
    """规则品类与货品品类的匹配层级：0=通用、1=一级、2=二级、3=三级，-1=不匹配。

    品类现在是三级码（``HA.KC.DFB``），规则可以挂在任意一层：
    挂在三级的只对这一个叶子生效，挂在一级的整类生效，``*`` 通用。
    """
    rc = str(rule_cat or "*").upper()
    if rc in ("*", ""):
        return 0
    if cat == rc:
        return rc.count(".") + 1
    if cat.startswith(rc + "."):
        return rc.count(".") + 1
    return -1


def load_rules(session: Session, category: str, at=None) -> list[WmsGradeRule]:
    """按品类取启用且在生效期内的规则，priority 小的先匹配。

    同优先级时**更具体的规则先匹配**（三级 > 二级 > 一级 > 通用）——
    否则挂在一级上的规则会盖掉某个三级品类专门写的标准。
    """
    rows = list(
        session.scalars(
            select(WmsGradeRule)
            .where(WmsGradeRule.enabled.is_(True))
            .order_by(WmsGradeRule.priority.asc(), WmsGradeRule.id.asc())
        ).all()
    )
    cat = str(category or "").upper()
    out: list[tuple[int, int, int, WmsGradeRule]] = []
    for row in rows:
        if not _effective(row, at):
            continue
        depth = _rule_depth(str(row.category or "*").upper(), cat)
        if depth < 0:
            continue
        out.append((int(row.priority or 0), -depth, int(row.id or 0), row))
    out.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in out]


def human_reasons(facts: dict, grade: str) -> list[str]:
    """把 facts 翻译成检测员看得懂的中文依据（引擎不能只返回档位）。"""
    out: list[str] = []
    # 全新（2026-10-10）：先说这台的新旧程度 —— 它是 N 档的唯一依据
    if facts.get("sealed"):
        out.append("原封未拆（封条 / 塑封完好）")
    elif facts.get("opened_unused"):
        out.append("拆封未使用（开过封但无使用痕迹）")
    fail = int(facts.get("fail_count") or 0)
    flaw = int(facts.get("flaw_count") or 0)
    if facts.get("all_items_pass"):
        out.append("功能检项全部通过")
    elif fail:
        out.append(f"功能不合格 {fail} 项")
    if flaw:
        out.append(f"外观瑕疵 {flaw} 处")
    else:
        out.append("零外观瑕疵")
    # 不影响使用的缺陷（2026-10-13）：面板花屏缺笔画、少说明书遥控器 —— 照样能用照卖，
    # 检测员看见这句话就知道为什么是 B 而不是 C / D
    if facts.get("minor_defect"):
        out.append("有不影响使用的缺陷（面板 / 显示、缺非关键配件）—— 降价值但照常售卖")
    if facts.get("safety_hit"):
        out.append("命中安全红线（鼓包 / 漏液 / 异味 / 漏电 / 焦糊 / 火烧）")
    if facts.get("inhouse_fixable"):
        out.append("故障在「厂内可修清单」内")
    if facts.get("must_official"):
        out.append("故障在「必须官方修清单」内")
    if facts.get("missing_parts"):
        out.append("缺件严重")
    if not out:
        out.append(f"综合判定为 {grade_label(grade)}")
    return out


def evaluate(session: Session, category: str, facts: dict, at=None) -> dict:
    """跑引擎，返回建议档位 + 依据。

    2026-10-13 用户拍板「每个等级只留一条规则」：C 报修（红线 / 功能故障 / 必须官方修
    三合一）与 D 报废（功能故障严重 / 缺件严重二合一）各一条；带 safety_flag 的规则
    **永远第一匹配**，这是唯一优先于 priority 的规则。红线先走报修（C），只有
    「缺件严重 / 无维修价值」（人工勾）或「功能故障严重（≥3 项）」才判 D 报废。
    """
    rules = load_rules(session, category, at)
    safety = bool(facts.get("safety_hit"))
    for rule in rules:
        if rule.safety_flag and _match(rule.conditions or {}, facts):
            return {
                "suggest_grade": rule.grade,
                "grade": rule.grade,
                "rule_id": rule.id,
                "reasons": human_reasons(facts, rule.grade) + [rule.reason or "安全红线规则命中"],
                "safety": True,
                "blocked": False,
                "rule_title": rule.title or "安全红线",
            }
    if safety and not _fact(facts, "missing_parts") and int(facts.get("fail_count") or 0) < 3:
        # 没配安全规则也兜住：安全红线先走报修（C）。
        # 勾了「缺件严重 / 无维修价值」或坏满三项的不走这条 —— 那是彻底没法修的，留给 D。
        return {
            "suggest_grade": GRADE_C,
            "grade": GRADE_C,
            "rule_id": None,
            "reasons": human_reasons(facts, GRADE_C) + ["安全红线未配规则，按兜底判 C 报修"],
            "safety": True,
            "blocked": False,
            "rule_title": "安全红线（兜底）",
        }
    for rule in rules:
        if rule.safety_flag:
            continue
        if _match(rule.conditions or {}, facts):
            return {
                "suggest_grade": rule.grade,
                "grade": rule.grade,
                "rule_id": rule.id,
                "reasons": human_reasons(facts, rule.grade) + ([rule.reason] if rule.reason else []),
                "safety": False,
                "blocked": False,
                "rule_title": rule.title or grade_label(rule.grade),
            }
    # 一条都没命中：保守判 B（进厂修，让人工看），而不是静默给个档位
    return {
        "suggest_grade": GRADE_B,
        "grade": GRADE_B,
        "rule_id": None,
        "reasons": human_reasons(facts, GRADE_B) + ["没有规则完全命中，保守判 B（需人工确认）"],
        "safety": False,
        "blocked": True,
        "rule_title": "未命中规则",
    }


# ---------------------------------------------------------------- 定价
def price_rule_of(session: Session, grade: str) -> WmsPriceRule | None:
    return session.scalar(
        select(WmsPriceRule).where(
            WmsPriceRule.grade == str(grade or "").upper(),
            WmsPriceRule.enabled.is_(True),
        )
    )


def price_of(
    session: Session,
    grade: str,
    base_price: float,
    purchase_cost: float = 0.0,
    manual_price: float | None = None,
    manual_suggest: float | None = None,
) -> dict:
    """套系数出价 + 毛利校验 + 底价拦截 + 建议售价封顶。

    · 人工填了价就以人工价为准，但仍算毛利与底价（低于底价要审批）；
    · **建议售价 = 基准价 × 建议售价系数**（2026-10-10）：它是渔枢 / 渔货栈建档时
      「建议售价」的基础数据，渔集定级的同时就算出来，不让人再拍一次脑袋；
      ``manual_suggest``（2026-10-12 手工建档加了这格）填过就以人工填的为准 ——
      人填的数不该被系数盖掉；
    · **渔货价不得高于建议售价**：供货价比建议零售价还高就没法卖了 —— 算高了
      （或人工改价改超了）一律压到建议售价，并在 ``clamped_by_suggest`` 里说清楚；
    · 残值（D 档）不走这套 —— D 档按拆机件逐件估值，见 parts.py。
    """
    rule = price_rule_of(session, grade)
    factor = float(rule.factor) if rule else 1.0
    suggest_factor = float(getattr(rule, "suggest_factor", 0) or 0) if rule else 0.0
    # 建议售价系数没配（没有这条规则）才按 1.0 兜底；**配置成 0 是有意的**
    # （2026-10-13 用户拍板：C / D 不具备价值，系数全部为 0）—— 别把 0 当成「没配」。
    if rule is None and suggest_factor <= 0:
        suggest_factor = 1.0
    min_margin = float(rule.min_margin) if rule else 0.0
    approve_below = float(rule.need_approval_below) if rule else 0.0
    base = float(base_price or 0.0)
    cost = float(purchase_cost or 0.0)
    # 价格一律**向下取整到元**（2026-10-15 用户拍板）：¥218.99 这种小数没人会用，
    # 报价、打印、渔枢渔货栈三边对不上的祸根就是它。人工填的也照取整收。
    manual_suggest_value = floor_price(manual_suggest) if manual_suggest else 0.0
    suggest_price = manual_suggest_value or (
        floor_price(base * suggest_factor) if base > 0 else 0.0
    )
    price = floor_price(manual_price) if manual_price else floor_price(base * factor)
    if price <= 0:
        price = floor_price(base * factor)
    # 封顶：渔货价不能高过建议售价（人工改价改超了也一样压回来）
    clamped_by_suggest = bool(suggest_price > 0 and price > suggest_price)
    if clamped_by_suggest:
        price = suggest_price
    margin = round((price - cost) / price, 4) if price > 0 else 0.0
    floor = math.floor(base * approve_below) if approve_below > 0 else 0.0
    # 人工改价只许往上调（2026-10-10）：填的数低于「低于基准价此比例需审批」那条线
    # 就是**不许改**（不是提醒一下就算）—— 检测台与货品详情页都用这个标记拦提交。
    manual_below_floor = bool(
        manual_price and float(manual_price) > 0 and floor > 0
        and math.floor(float(manual_price)) < floor
    )
    return {
        "grade": str(grade or "").upper(),
        "factor": factor,
        "base_price": base,
        "cost": cost,
        "price": price,
        "margin": margin,
        "min_margin": min_margin,
        # 低于底价：需主管审批
        "need_approval": bool(floor > 0 and price < floor),
        "floor": floor,
        # 毛利率低于下限：告警 + 需填原因
        "low_margin": bool(min_margin > 0 and margin < min_margin),
        # 人工价低于审批线：调用方应当拦住（不能落库）
        "manual_below_floor": manual_below_floor,
        # 该档审批比例本身（页面上说「最低 ¥X」用的就是这个）
        "approve_below": approve_below,
        # 建议售价（2026-10-10）：渔枢 / 渔货栈建档时直接带过去用
        "suggest_price": suggest_price,
        "suggest_factor": suggest_factor,
        # 这个建议售价是人工填的（不是系数算的）—— 页面上换个说法，别说「市场价 × 系数」
        "manual_suggest": bool(manual_suggest_value),
        # 渔货价被建议售价压下来了（页面上要说明「为什么不是系数算出来的那个数」）
        "clamped_by_suggest": clamped_by_suggest,
        "rule_id": rule.id if rule else None,
    }


GRADE_ORDER = (GRADE_W, GRADE_S, GRADE_A, GRADE_B, GRADE_C, GRADE_D)
