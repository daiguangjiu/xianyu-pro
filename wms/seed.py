"""启动播种：仓库 / 分级规则 / 价格系数。

分级标准文档（《二手货品检测分级标准 V1.0》）的判定条件直接转成本表初始数据，
**保证纸质标准与系统规则完全一致** —— 避免出现「纸上写 85%、系统判 82%」。

幂等：已有的不重复建；管理员在后台改过的（enabled=False / 改了阈值）不会被覆盖。
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    BATCH_SOURCE_LABELS,
    GRADE_A,
    GRADE_B,
    GRADE_C,
    GRADE_D,
    GRADE_N,
    GRADE_S,
    PART_CHOICES,
    PART_LABELS,
    WAREHOUSE_DEFAULTS,
    WmsGradeRule,
    WmsPriceRule,
    WmsWarehouse,
    register_warehouse_names,
)

logger = logging.getLogger(__name__)

# 分级规则：2026-10-13 用户拍板「每个等级只留一条规则」——
# C 的三种来路（安全红线 / 功能故障 / 必须官方修）合成一条 C 规则，
# D 的两种来路（功能故障严重 / 缺件严重）合成一条 D 规则，规则页不再一档多行。
GRADE_RULE_SEED: tuple[dict, ...] = (
    {
        # C 报修（合并了原来的「安全红线 → 报修」「有影响使用的功能故障」「必须官方修」三条）：
        # safety_flag=1 = 永远第一匹配。三个闸门：
        #   · missing_parts == False：勾了「缺件严重 / 无维修价值」的不归 C —— 让下面的 D 接住；
        #   · fail_count < 3：坏满三项属于「完全不能用」，也归 D；
        #   · any_of 三选一：红线 / 真故障 / 必须官方修。
        "category": "*",
        "priority": 40,
        "grade": GRADE_C,
        "safety_flag": True,
        "title": "C 报修：安全红线 / 功能故障 / 必须官方修",
        "reason": "先修不先废（2026-10-13）：修得好照样卖，修完重新评级；不具备价值、不可售",
        "conditions": {"all_of": [
            {"field": "missing_parts", "op": "==", "value": False},
            {"field": "fail_count", "op": "<", "value": 3},
        ], "any_of": [
            {"field": "safety_hit", "op": "==", "value": True},
            {"field": "functional_fail", "op": "==", "value": True},
            {"field": "must_official", "op": "==", "value": True},
        ]},
    },
    {
        # N 全新（2026-10-10）：原封未拆 / 拆封未使用 —— 排在 S 前面（优先级更小先匹配）
        "category": "*",
        "priority": 5,
        "grade": GRADE_N,
        "title": "N 全新：原封未拆 或 拆封未使用 + 全项通过 + 零瑕疵",
        "reason": "全新机：原封未拆或拆封未使用，零使用痕迹，可直接上架",
        # 各档条件项现在完全一致（必检项 + 外观瑕疵）—— 2026-10-11 起电池健康度整项下线，
        # 不要再往任何一档里加电池条件（规则页的「规则体检」会把用了下线条件项的老规则点出来）。
        "conditions": {
            "all_of": [
                {"field": "all_items_pass", "op": "==", "value": True},
                {"field": "flaw_count", "op": "==", "value": 0},
                # 2026-10-13：全新 / 完美这两档必须排除「勾了缺件严重 / 必须官方修 /
                # 命中安全红线」的货 —— 以前只看「检项全过 + 零瑕疵」，勾了那两个勾
                # 也会被判 S 完美（检项确实全过），让人以为这台是好的。
                {"field": "missing_parts", "op": "==", "value": False},
                {"field": "must_official", "op": "==", "value": False},
                {"field": "safety_hit", "op": "==", "value": False},
            ],
            "any_of": [
                {"field": "sealed", "op": "==", "value": True},
                {"field": "opened_unused", "op": "==", "value": True},
            ],
        },
    },
    {
        "category": "*",
        "priority": 10,
        "grade": GRADE_S,
        "title": "S 完美：全项通过 + 零瑕疵",
        "reason": "零使用痕迹、零外观瑕疵，可直接上架",
        "conditions": {"all_of": [
            {"field": "all_items_pass", "op": "==", "value": True},
            {"field": "flaw_count", "op": "==", "value": 0},
            # 同 N 全新：勾了「缺件严重 / 必须官方修」或命中红线就不是完美
            {"field": "missing_parts", "op": "==", "value": False},
            {"field": "must_official", "op": "==", "value": False},
            {"field": "safety_hit", "op": "==", "value": False},
        ]},
    },
    {
        "category": "*",
        "priority": 20,
        "grade": GRADE_A,
        "title": "A 微瑕：功能全过 + 仅外观瑕疵",
        "reason": "仅外观瑕疵，强制填瑕疵描述",
        "conditions": {"all_of": [
            {"field": "all_items_pass", "op": "==", "value": True},
            {"field": "flaw_count", "op": ">", "value": 0},
            {"field": "missing_parts", "op": "==", "value": False},
            {"field": "must_official", "op": "==", "value": False},
            {"field": "safety_hit", "op": "==", "value": False},
        ]},
    },
    {
        # 2026-10-13 用户拍板：面板显示有问题、少本说明书 —— 不影响使用，只降价值，
        # 判 B（缺/瑕疵/可卖）照常上架。优先级 25 = 排在 A 之后、其它 B / C / D 之前。
        "category": "*",
        "priority": 25,
        "grade": GRADE_B,
        "title": "B 缺/瑕疵/可卖：只有不影响使用的缺陷（面板 / 显示、非关键配件）",
        "reason": "面板 / 显示有问题或缺说明书遥控器这类非关键配件：不影响正常使用，降价值后照常售卖",
        "conditions": {"all_of": [
            {"field": "minor_defect", "op": "==", "value": True},
            {"field": "functional_fail", "op": "==", "value": False},
            {"field": "missing_parts", "op": "==", "value": False},
            {"field": "must_official", "op": "==", "value": False},
        ]},
    },
    {
        # D 报废（合并了原来的「功能故障严重（≥3 项）」「缺件严重 / 无维修价值」两条）：
        # 二者满足其一就报废 —— 坏到修不了，或缺到没法用。
        "category": "*",
        "priority": 50,
        "grade": GRADE_D,
        "title": "D 报废：功能故障严重（3 项及以上）或缺件严重 / 无维修价值",
        "reason": "无维修价值，不具备价值、不可售，按拆机件逐件估值",
        "conditions": {"any_of": [
            {"field": "fail_count", "op": ">=", "value": 3},
            {"field": "missing_parts", "op": "==", "value": True},
        ]},
    },
)

# 价格系数（方案 1.2 / 8.3）：S 1.00，A 0.85~0.92 取 0.88
# 建议售价系数（2026-10-10）：**建议售价 = 基准价 × suggest_factor**，渔枢 / 渔货栈建档时的
# 建议售价就用它。硬规矩是「渔货价不能高于建议售价」，所以播种值一律 ≥ 渔货价系数
# （全新机零售还能再溢价一点，这也是渔货栈卖给终端的价格空间）。
PRICE_RULE_SEED: tuple[dict, ...] = (
    {"grade": GRADE_N, "factor": 1.05, "suggest_factor": 1.15, "min_margin": 0.15,
     "need_approval_below": 0.90, "note": "N 全新：渔货价 基准价 ×1.05，建议售价 ×1.15，九折以下需主管审批"},
    {"grade": GRADE_S, "factor": 1.00, "suggest_factor": 1.10, "min_margin": 0.15,
     "need_approval_below": 0.85, "note": "S 完美：渔货价 基准价 ×1.00，建议售价 ×1.10，八五折以下需主管审批"},
    {"grade": GRADE_A, "factor": 0.88, "suggest_factor": 1.00, "min_margin": 0.12,
     "need_approval_below": 0.80, "note": "A 微瑕：渔货价 基准价 ×0.88，建议售价 ×1.00，八折以下需主管审批"},
    {"grade": GRADE_B, "factor": 0.30, "suggest_factor": 0.40, "min_margin": 0.10,
     "need_approval_below": 0.25, "note": "B 缺/瑕疵/可卖：渔货价 基准价 ×0.30，建议售价 ×0.40，低于基准价 25% 需审批（2026-10-13 拍板）"},
    # C / D 系数全部为 0（2026-10-13 用户拍板）：报修 / 报废的货**不具备价值、不可售**，
    # 不出参考价 —— C 修好重新检测评级后才按新档出价；D 按拆机件逐件估值（parts.py）。
    {"grade": GRADE_C, "factor": 0.00, "suggest_factor": 0.00, "min_margin": 0.00,
     "need_approval_below": 0.00, "note": "C 报修：不具备价值（系数 0）不可售，维修后重新评级出价"},
    {"grade": GRADE_D, "factor": 0.00, "suggest_factor": 0.00, "min_margin": 0.00,
     "need_approval_below": 0.00, "note": "D 报废：不具备价值（系数 0）不可售，按拆机件逐件估值（见 parts.py）"},
)


def ensure_warehouses(session: Session) -> int:
    created = 0
    for idx, (code, name, desc, can_out) in enumerate(WAREHOUSE_DEFAULTS):
        row = session.scalar(select(WmsWarehouse).where(WmsWarehouse.code == code))
        if row:
            continue
        session.add(
            WmsWarehouse(
                code=code, name=name, level=1, parent_code="", full_path=name,
                grade_zone="", can_out=can_out, enabled=True, sort=(idx + 1) * 10,
                note=desc,
            )
        )
        created += 1
    if created:
        session.flush()
    # 内置仓建完顺手把名字灌进缓存（页面上一改名就即时生效，靠这份缓存）
    register_warehouse_names(session.scalars(select(WmsWarehouse)).all())
    return created


def ensure_grade_rules(session: Session) -> int:
    """内置分级规则只在**表还空着**（首次初始化）时播种一次。

    以前是「缺什么补什么」（按品类 + 说明判重），结果现场把一条内置规则删掉之后，
    下一次保存规则 / 重启服务又被原样补回来 —— 看起来就是「删了没用，条件还在」
    （2026-10-10 现场反馈）。表里已经有规则就一条都不补，谁删的谁说了算。
    """
    if session.scalar(select(func.count()).select_from(WmsGradeRule)):
        return 0
    created = 0
    for seed in GRADE_RULE_SEED:
        session.add(WmsGradeRule(**seed))
        created += 1
    if created:
        session.flush()
    return created


def ensure_price_rules(session: Session) -> int:
    """价格系数同分级规则：只在表空着时播种一次（删掉的档位不再自动补回来）。"""
    if session.scalar(select(func.count()).select_from(WmsPriceRule)):
        return 0
    created = 0
    for seed in PRICE_RULE_SEED:
        session.add(WmsPriceRule(**seed))
        created += 1
    if created:
        session.flush()
    return created


def ensure_defaults(session: Session) -> dict:
    """启动播种：仓库缺什么补什么；分级规则 / 价格系数只在表空着时播种一次。"""
    out = {
        "warehouses": ensure_warehouses(session),
        "grade_rules": ensure_grade_rules(session),
        "price_rules": ensure_price_rules(session),
    }
    if any(out.values()):
        session.commit()
        logger.info("WMS 播种完成：%s", out)
    return out


def part_choices() -> tuple[tuple[str, str], ...]:
    """拆机件类型下拉（残值明细逐件估值用）。"""
    return tuple((key, PART_LABELS[key]) for key in PART_CHOICES)


def batch_source_choices() -> tuple[tuple[str, str], ...]:
    return tuple(BATCH_SOURCE_LABELS.items())
