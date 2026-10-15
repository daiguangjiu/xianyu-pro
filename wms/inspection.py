"""扫码检测工作台的后端：检测项按品类动态渲染 + 提交检测 + 引擎建议。

两条防错（方案 6.2）：
  · **不合格项未填描述不允许提交**（否则后面谁也不知道这台坏在哪）；
  · 命中安全红线（电池鼓包漏液 / 异味焦糊火烧 / 漏电触电）立即走危废流程。

存疑（?）不硬判：只标记进复检池，状态仍是 PENDING，**不隔夜**。
人工覆盖引擎建议必须填理由 —— 那是规则调优的唯一依据。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .timeutil import now as _now
from .models import (
    CATEGORY_APPLIANCE,
    CATEGORY_AUTO,
    CATEGORY_BABY,
    CATEGORY_BEDDING,
    CATEGORY_COMMERCIAL,
    CATEGORY_COMPONENT,
    CATEGORY_DIGITAL,
    CATEGORY_HARDWARE,
    CATEGORY_INDUSTRIAL,
    CATEGORY_OFFICE,
    CATEGORY_OTHER,
    CATEGORY_PHONE,
    CATEGORY_PET,
    GRADE_C,
    GRADE_D,
    GRADE_S,
    GRADE_A,
    GRADE_W,
    GRADE_LABELS,
    SAFETY_ITEM_KEYS,
    STATUS_GRADED,
    STATUS_HAZARD,
    STATUS_LABELS,
    STATUS_PENDING,
    TERMINAL_STATUSES,
    TEST_FAIL,
    TEST_NA,
    TEST_PASS,
    WH_HAZARD,
    WH_PENDING,
    WmsItem,
    grade_label,
    WmsTestItem,
    WmsTestRecord,
    category_label,
    l1_code_of,
)
from . import audit
from . import bridge
from . import grading
from . import labeling
from . import ledger
from . import state


# ---------------------------------------------------------------- 检测项模板
# 2026-10-13 用户拍板：**不再按品类分两套**（原来数码 11 项 / 家电 8 项），
# 所有货一套 10 项 + 3 项安全红线。带电池款与插电款的区别写在同一项的说明里
# （例：「充电接口 / 电源线插头」这一项，带电池看接口、插电款看线材插头），
# 检测员不用先判断这台货该走哪套。
UNIFIED_ITEMS: tuple[tuple[str, str], ...] = (
    ("appear", "外观成色"),
    ("inner", "内腔成色"),
    ("power", "开机 / 上电启动"),
    ("panel", "屏幕 / 面板显示"),
    ("touch", "触控 / 按键 / 指纹人脸"),
    ("charge", "充电接口 / 电源线插头"),
    ("camera", "摄像头 / 扬声器 / 麦克风"),
    ("wifi", "WiFi / 信号 / 蓝牙 / 定位"),
    ("function", "核心功能 & 运行异响"),
    ("accessory", "配件齐全"),
)

# 每一项怎么判（现场口径原文），检测台逐项下面那行小字。
ITEM_HINTS: dict[str, str] = {
    "appear": "外壳无裂纹、严重磕碰、掉漆、深划痕；无明显拆机痕迹。细微发丝划痕可标记微瑕",
    "inner": "内部是否有使用痕迹、成色",
    "power": "带电池：可正常开机重启、无死机反复重启；插电款：通电可正常启动、上电无打火",
    "panel": "屏幕 / 控制面板完好，无碎裂漏液黑斑；显示正常、无花屏乱码",
    "touch": "触控灵敏，实体按键回弹正常；指纹 / 人脸功能正常（如有）",
    "charge": "带电池：接口完好可正常充电、无烧蚀发黑；插电款：电源线无破皮、插头完好无烧蚀变形",
    "camera": "摄像头成像正常；扬声器 / 听筒出声无破音；麦克风收音通话正常",
    "wifi": "WiFi、蜂窝信号、蓝牙、定位功能正常",
    "function": "带电池：产品基础功能正常；插电家电：核心功能（制冷 / 加热 / 洗涤等）正常、运行无尖锐异响",
    "accessory": "对照清点配件，无缺失、错配（只缺说明书 / 遥控器这类非关键配件判「×」即可，别勾「缺件严重」）",
}

# 外观类（外壳 / 内腔成色）：判 × 只降成色 → A 微瑕，**不算功能故障**。
# 2026-10-13 新加 inner（内腔成色），与 appear 同一账。
COSMETIC_KEYS: tuple[str, ...] = ("appear", "inner")

# 老两套的别名：统一之后它们就是同一套，留着兼容老的 import。
GENERAL_ITEMS = UNIFIED_ITEMS
APPLIANCE_ITEMS = UNIFIED_ITEMS

# 「不影响使用」的降级项（2026-10-13 用户拍板）：屏幕上判了 ×，机器照样能卖能用
# —— 面板 / 显示有划痕或显示缺笔画、少一本说明书一个遥控器这类。
# 它们仍然算不合格（所以进不了 S / A / N），但**只降价值、不报废不报修**：
# 判 B 缺/瑕疵/可卖，照常上架。以前落在「没有规则命中 → 兜底判 B」，
# 检测员看到「未命中规则」四个字只会以为系统没判明白，还得自己猜。
MINOR_ITEM_KEYS: tuple[str, ...] = ("panel", "accessory")
MINOR_ITEM_LABELS: dict[str, str] = {
    "panel": "面板 / 显示有问题（不影响使用）",
    "accessory": "缺非关键配件（说明书 / 遥控器等，不影响使用）",
}

# 安全红线项：命中任意一项 → 直接危废（禁止充电与任何流转）
# 2026-10-13 由 6 项收敛成 3 项（用户拍板）—— 这三类看到就直接进危废，
# 拆成 6 格只是让检测员多点 3 下；key 与 models.SAFETY_ITEM_KEYS 必须一致。
SAFETY_ITEMS: tuple[tuple[str, str], ...] = (
    ("swell", "电池鼓包 / 漏液"),
    ("burn", "异味 / 焦糊 / 火烧痕迹"),
    ("shock", "漏电 / 触电"),
)

CATEGORY_TEMPLATES: dict[str, tuple[tuple[str, str], ...]] = {
    # 数码类（带电池）：手机通讯 / 数码 / 母婴电器；老数据 平板 NB/WT 走兜底也是这套
    CATEGORY_PHONE: GENERAL_ITEMS,
    CATEGORY_DIGITAL: GENERAL_ITEMS,
    CATEGORY_BABY: GENERAL_ITEMS,
    # 插电类（无电池）：家用电器 / 电脑、办公（显示器居多）/ 商用电器 / 工业品等
    CATEGORY_APPLIANCE: APPLIANCE_ITEMS,
    CATEGORY_OFFICE: APPLIANCE_ITEMS,
    CATEGORY_COMMERCIAL: APPLIANCE_ITEMS,
    CATEGORY_INDUSTRIAL: APPLIANCE_ITEMS,
    CATEGORY_HARDWARE: APPLIANCE_ITEMS,
    CATEGORY_BEDDING: APPLIANCE_ITEMS,
    CATEGORY_AUTO: APPLIANCE_ITEMS,
    CATEGORY_PET: APPLIANCE_ITEMS,
    CATEGORY_COMPONENT: APPLIANCE_ITEMS,
    CATEGORY_OTHER: GENERAL_ITEMS,
}

def template_of(category: str) -> tuple[tuple[str, str], ...]:
    """检测项模板：2026-10-13 起**所有品类同一套**（不再分数码 / 家电）。

    参数 category 留着没用（历史调用点多，一并改没必要），但三级品类回落的
    逻辑留着，哪天要按品类加项能直接接回来。
    """
    return UNIFIED_ITEMS


def full_template(category: str) -> tuple[tuple[str, str], ...]:
    """检测员看到的全套检项：品类检项 + 安全红线项。"""
    return template_of(category) + SAFETY_ITEMS


def _flaw_count(flaw_desc: str) -> int:
    """瑕疵处数：按行 / 分号 / 顿号拆，数非空片段。"""
    import re

    text = str(flaw_desc or "").strip()
    if not text:
        return 0
    parts = [p for p in re.split(r"[\n;；、,，]+", text) if p.strip()]
    return max(len(parts), 1)


def facts_of(
    item: WmsItem,
    results: dict[str, str],
    battery_health,   # 2026-10-11 已下线（主营小家电，电池是手机品类专属）：
                      # 参数留着兼容老调用，不再进 facts、不参与判级
    flaw_desc: str,
    *,
    inhouse_fixable: bool = False,
    must_official: bool = False,
    missing_parts: bool = False,
    sealed: bool = False,
    opened_unused: bool = False,
) -> dict:
    """把一次检测翻译成引擎能吃的 facts。"""
    template = template_of(item.category)
    # 不影响使用的降级项（面板 / 显示、非关键配件）：见 MINOR_ITEM_KEYS 的说明。
    # 它们是**不合格**（所以 S / A / N 都进不去），但只降价值、不报废不报修 → 判 B。
    minor = [key for key in MINOR_ITEM_KEYS
             if str(results.get(key) or "").lower() == TEST_FAIL]
    # 外观类（外壳成色 / 内腔成色）：只降成色，判 × 走 A 微瑕，**不算功能故障** ——
    # 不能让 all_items_pass 变 False，否则 A/S/N 三条规则（硬前提都是
    # all_items_pass == True）永远命中不了，只能掉进「未命中规则」兜底判 B。
    cosmetic = [key for key in COSMETIC_KEYS
                if str(results.get(key) or "").lower() == TEST_FAIL]
    fails = [
        key for key, _label in template
        if key not in COSMETIC_KEYS
        and str(results.get(key) or "").lower() == TEST_FAIL
    ]
    answered = [
        key for key, _label in template
        if str(results.get(key) or "").lower() in (TEST_PASS, TEST_FAIL)
    ]
    safety = [
        key for key in SAFETY_ITEM_KEYS
        if str(results.get(key) or "").lower() == TEST_FAIL
    ]
    return {
        "all_items_pass": bool(answered) and not fails,
        "fail_count": len(fails),
        # 外观判 × 但没写描述时兜 1 处：否则 flaw_count == 0 会掉回 S 完美
        "flaw_count": max(_flaw_count(flaw_desc), 1) if cosmetic else _flaw_count(flaw_desc),
        "safety_hit": bool(safety),
        # 外观类（外壳 / 内腔成色）判 ×（2026-10-13 新加，配合「内腔成色」这一项）
        "cosmetic_defect": bool(cosmetic),
        # 降级 vs 真故障（2026-10-13）：只降价值的缺陷 / 有影响使用的功能故障。
        # 分级规则里「B 缺/瑕疵/可卖」靠这两条区分：有降级项且没有真故障 → 判 B 照常卖。
        "minor_defect": bool(minor),
        "functional_fail": bool([k for k in fails if k not in MINOR_ITEM_KEYS]),
        "inhouse_fixable": bool(inhouse_fixable),
        "must_official": bool(must_official),
        "missing_parts": bool(missing_parts),
        # 全新（2026-10-10）：原封未拆 / 拆封未使用 —— N 档的两个入口
        "sealed": bool(sealed),
        "opened_unused": bool(opened_unused),
    }


def preview(session: Session, item: WmsItem, payload: dict) -> dict:
    """实时建议：检测员每勾一项就刷新（不落库，只算建议档位与依据）。"""
    results = payload.get("results") or {}
    facts = facts_of(
        item,
        results,
        payload.get("battery_health"),
        str(payload.get("flaw_desc") or ""),
        inhouse_fixable=bool(payload.get("inhouse_fixable")),
        must_official=bool(payload.get("must_official")),
        missing_parts=bool(payload.get("missing_parts")),
        sealed=bool(payload.get("sealed")),
        opened_unused=bool(payload.get("opened_unused")),
    )
    out = grading.evaluate(session, item.category, facts)
    out["facts"] = facts
    # 人工指定档位（2026-10-15 用户拍板）：检测员在「最终档位」下拉选了档，
    # 屏幕上的评级 / 价格 / 标签等级立刻全部按所选档重算 —— 引擎建议照旧展示，
    # 谁说了算一眼分清。选回「跟随引擎建议」就回到引擎的建议档。
    manual = str(payload.get("grade") or "").strip().upper()
    if manual and manual in GRADE_LABELS and manual != out["suggest_grade"]:
        out["effective_grade"] = manual
        out["manual_grade"] = True
    else:
        out["effective_grade"] = out["suggest_grade"]
        out["manual_grade"] = False
    out["grade_label_text"] = grade_label(out["effective_grade"])
    out["price"] = grading.price_of(
        session, out["effective_grade"], float(item.base_price or 0.0),
        float(item.purchase_cost or 0.0),
        # 建档 / 编辑时人工填过建议售价的（2026-10-12）以人填的为准：屏幕上的建议售价
        # 与封顶口径必须和提交时一致
        manual_suggest=float(item.suggest_price or 0.0) or None,
    )
    # 与 submit 的落价口径对齐：只有 N / S / A 会写渔货价（B 修完重判、C/D 按故障估值）。
    # 页面靠这个标记决定「直接提交就按这个价」还是「该档不写价」——别让人以为
    # 屏幕上那个数字提交后会存进商品。
    out["price"]["priced"] = out["effective_grade"] in grading.PRICED_GRADES
    # 没有启用中的价格系数时 price_of 按 1.0 兜底（毛利会算成 100%），页面要说清为什么
    out["price"]["rule_missing"] = out["price"]["rule_id"] is None
    return out


def submit(
    session: Session,
    item: WmsItem,
    *,
    tester: str,
    results: dict[str, str],
    fail_descs: dict[str, str] | None = None,
    battery_health=None,
    flaw_desc: str = "",
    photos: list[str] | None = None,
    final_grade: str = "",
    override_reason: str = "",
    uncertain: bool = False,
    inhouse_fixable: bool = False,
    must_official: bool = False,
    missing_parts: bool = False,
    sealed: bool = False,
    opened_unused: bool = False,
    note: str = "",
    manual_price: float | None = None,
    recheck_reason: str = "",
) -> WmsTestRecord:
    """提交一次检测：留痕 + 拍照 + 引擎建议 + 人工确认。"""
    fail_descs = fail_descs or {}
    template = full_template(item.category)
    if uncertain:
        # 存疑不硬判：只进复检池，状态不动
        item.need_recheck = True
        item.updated_at = _now()
        record = WmsTestRecord(
            item_id=item.id, barcode=item.barcode, category=item.category,
            round=_next_round(session, item.id), tester=str(tester or ""),
            uncertain=True, note=str(note or "") or "检测员标记存疑，待复检",
            result_json=dict(results or {}), tested_at=_now(), created_at=_now(),
        )
        session.add(record)
        session.flush()
        _save_items(session, record, item, template, results, fail_descs, allow_empty=True)
        audit.log(session, "item", item.id, "grade", after="存疑待复检",
                  operator=tester, note="检测员判「?」，进复检池")
        session.commit()
        return record

    # ① 不合格项必须填描述（安全红线项也要）
    missing = []
    for key, label in template:
        if str(results.get(key) or "").lower() == TEST_FAIL and not str(
            fail_descs.get(key) or ""
        ).strip():
            missing.append(label)
    if missing:
        raise ValueError("以下不合格项还没填描述：" + "、".join(missing[:5])
                         + ("…" if len(missing) > 5 else ""))

    # 外观瑕疵描述（2026-10-13 用户拍板：独立的「外观瑕疵描述」输入框撤掉）——
    # 判 A 需要的那段「差在哪」现在直接取外观两项（外观成色 / 内腔成色）判 × 时
    # 填的不合格原因，照样带进渔枢新增商品的「供应商友情提示」，一段两用。
    cosmetic_desc = "；".join(
        str(fail_descs.get(key) or "").strip()
        for key in COSMETIC_KEYS
        if str(fail_descs.get(key) or "").strip()
    )
    flaw_text = str(flaw_desc or "").strip() or cosmetic_desc

    facts = facts_of(
        item, results, battery_health, flaw_text,
        inhouse_fixable=inhouse_fixable, must_official=must_official,
        missing_parts=missing_parts, sealed=sealed, opened_unused=opened_unused,
    )
    engine = grading.evaluate(session, item.category, facts)
    suggest = str(engine.get("suggest_grade") or "").upper()
    want = str(final_grade or "").strip().upper()
    round_no = _next_round(session, item.id)
    prev_grade = str(item.grade or "W").upper()
    status_now = str(item.status or "").upper()
    # 价格同步要跟「改之前」比，先抄下来（提交成功后推给渔枢 / 渔货栈）
    old_selling = round(float(item.selling_price or 0.0), 2)
    old_suggest = round(float(item.suggest_price or 0.0), 2)

    # 覆盖理由收口（2026-10-15）：复检理由与改档理由是同一个「覆盖理由」——
    # 页面一个框两头交；接口直调时填了复检理由也能顶替改档理由（都是「为什么覆盖」）。
    # 顺序：先按 recheck_reason 兜底 override，再各自校验必填。
    recheck_reason = str(recheck_reason or "").strip()
    if not str(override_reason or "").strip() and recheck_reason:
        override_reason = recheck_reason
    if want and want != suggest and not str(override_reason or "").strip():
        raise ValueError(
            f"覆盖引擎建议（{suggest} → {want}）必须填覆盖理由："
            "这是规则调优的唯一依据"
        )

    # 复检必填覆盖理由（2026-10-15 用户拍板）：第 2 轮起就是覆盖上一轮结论 ——
    # 为什么重判必须说清楚，后面翻记录的人才知道哪轮可信、规则该不该跟着改。
    # 检测台在复检模式下摆了输入框，这里是后端兜底（接口直调也拦）。
    if round_no > 1 and not recheck_reason:
        raise ValueError(
            f"这是复检（第 {round_no} 轮），会覆盖上一轮（第 {round_no - 1} 轮）"
            "的结论 —— 必须填覆盖理由（为什么重新判 / 哪里变了）"
        )

    record = WmsTestRecord(
        item_id=item.id,
        barcode=item.barcode,
        category=item.category,
        round=round_no,
        result_json=dict(results or {}),
        fail_count=int(facts.get("fail_count") or 0),
        flaw_count=int(facts.get("flaw_count") or 0),
        all_items_pass=bool(facts.get("all_items_pass")),
        safety_hit=bool(facts.get("safety_hit")),
        suggest_grade=suggest,
        final_grade="",
        reasons=list(engine.get("reasons") or []),
        rule_id=engine.get("rule_id"),
        override_reason=str(override_reason or ""),
        is_recheck=round_no > 1,
        tester=str(tester or ""),
        photo_urls=list(photos or []),
        note=(str(note or "") + ("；" if note else "") + "复检覆盖理由：" + recheck_reason)
        if round_no > 1 and recheck_reason else str(note or ""),
        tested_at=_now(),
        created_at=_now(),
    )
    session.add(record)
    session.flush()
    _save_items(session, record, item, template, results, fail_descs)

    # ② 安全红线：2026-10-13 用户拍板改为**先走报修**（进报修仓，禁止充电待检修），
    # 不再直接转危废 —— 电池鼓包 / 异味 / 漏电这些先送去检修，修得好照样卖。
    # 只有「缺件严重 / 无维修价值」（人工勾）或「功能故障严重」时引擎已经判了 D，
    # 那就按 D 的路走进拆机件仓 —— 档位由引擎说了算，这里只负责落位。
    if facts.get("safety_hit"):
        final = str(engine.get("suggest_grade") or GRADE_C).upper()
        wh, st = state.route_of(final)
        record.final_grade = final
        item.grade = final
        item.safety_hit = True
        item.grade_reason = "；".join(engine.get("reasons") or [])
        item.updated_at = _now()
        ledger.apply(
            session, item,
            biz_type="GRADE",
            biz_id=str(record.id),
            to_warehouse=wh,
            to_status=st,
            operator=tester,
            remark="命中安全红线，转报修（禁止充电，待检修）" if final == GRADE_C else
                   "命中安全红线且无维修价值，转报废",
            idempotent_key=f"SAFETY:{item.id}:{record.id}",
            check_transition=False,
        )
        audit.log(session, "item", item.id, "safety", before=prev_grade, after=final,
                  operator=tester, note="安全红线")
        session.commit()
        return record

    final = want or suggest
    record.final_grade = final
    item.grade = final
    item.flaw_desc = flaw_text
    item.grade_reason = "；".join(engine.get("reasons") or [])
    item.need_recheck = False
    item.graded_at = _now()

    # ③ 定级后出价（N / S / A 默认按系数出价；B/C/D 默认不写价，但检测员可以人工定价）
    # 默认价 = 基准价 × 该档价格系数（「分级规则」页维护）；检测台上填了就以人填的为准，
    # 但仍按系数算毛利与底价（低于底价要审批）—— 2026-10-08：检测页要把这个默认价
    # 亮出来给人看、给人改，定价不该是个藏起来的数字。
    # 2026-10-11（用户：「价格支持修改」）：B/C/D 也放开改价 —— 系统默认还是不出价
    # （要修完重判 / 逐件估值），但检测员填了就按人工定价落库（同一套底价 / 建议售价上限校验）。
    manual = float(manual_price) if manual_price and float(manual_price) > 0 else 0.0
    if final in grading.PRICED_GRADES:
        price = grading.price_of(
            session, final, float(item.base_price or 0.0), float(item.purchase_cost or 0.0),
            manual_price=manual_price,
            manual_suggest=float(item.suggest_price or 0.0) or None,
        )
        # 2026-10-10：改价只许往上调 —— 低于「低于基准价此比例需审批」那条线直接拒绝，
        # 不是提醒一句就放行（真要低价卖就去找主管审，不能在检测台随手改）
        if price.get("manual_below_floor"):
            raise ValueError(
                f"改价只能往上调：¥{float(manual_price or 0):.2f} 低于审批线 "
                f"¥{float(price.get('floor') or 0):.2f}"
                f"（{grade_label(final)} 档「低于基准价 "
                f"{float(price.get('approve_below') or 0) * 100:.0f}% 需审批」）—— "
                f"系统默认 ¥{round(float(item.base_price or 0.0) * float(price.get('factor') or 1.0), 2):.2f}，"
                f"要更低就得走审批"
            )
        item.selling_price = float(price.get("price") or 0.0)
        if manual:
            default = round(float(item.base_price or 0.0) * float(price.get("factor") or 1.0), 2)
            clamped = bool(price.get("clamped_by_suggest"))
            audit.log(
                session, "item", item.id, "price",
                before=f"¥{default:.2f}", after=f"¥{item.selling_price:.2f}",
                operator=tester,
                note=f"检测定价：人工改价（{final} 档系数 {price.get('factor')}，系统默认 ¥{default:.2f}）"
                     + (f"；填的比建议售价 ¥{float(price.get('suggest_price') or 0):.2f} 高，"
                        f"已压到建议售价" if clamped else ""),
            )
    elif manual:
        price = grading.price_of(
            session, final, float(item.base_price or 0.0), float(item.purchase_cost or 0.0),
            manual_price=manual,
            manual_suggest=float(item.suggest_price or 0.0) or None,
        )
        if price.get("manual_below_floor"):
            raise ValueError(
                f"人工定价不能低于审批线：¥{manual:.2f} 低于 "
                f"¥{float(price.get('floor') or 0):.2f}"
                f"（{grade_label(final)} 档「低于基准价 "
                f"{float(price.get('approve_below') or 0) * 100:.0f}% 需审批」）—— "
                f"要更低就得走审批"
            )
        item.selling_price = float(price.get("price") or 0.0)
        clamped = bool(price.get("clamped_by_suggest"))
        audit.log(
            session, "item", item.id, "price",
            before="¥0.00", after=f"¥{item.selling_price:.2f}",
            operator=tester,
            note=f"检测定价：{grade_label(final)} 档系统默认不写价，检测员人工定价"
                 f"（基准价 ¥{float(item.base_price or 0.0):.2f}）"
                 + (f"；填的比建议售价 ¥{float(price.get('suggest_price') or 0):.2f} 高，"
                    f"已压到建议售价" if clamped else ""),
        )
    else:
        # B / C / D 没填价：按「这档不写价」走（要卖就修完重判 / 逐件估值）
        item.selling_price = 0.0

    # ④ 状态流转：定级即按等级路由入仓（2026-10-13 用户拍板「等级和状态以及所在仓也要同步更新」）
    # —— S / A / B 直进可售仓「可售」、C 进报修仓、D 进拆机件仓。以前只把状态拨到
    # 「已定级」留在原仓，仓与状态都得人再手工搬一遍。
    target_warehouse, target_status = state.route_of(final)
    if status_now not in (STATUS_GRADED,):
        ledger.apply(
            session, item,
            biz_type="GRADE",
            biz_id=str(record.id),
            to_warehouse=target_warehouse,
            to_status=target_status,
            operator=tester,
            remark=f"检测定级 {final}（第 {round_no} 次检测），入 {target_warehouse} 仓",
            idempotent_key=f"GRADE:{item.id}:{record.id}",
            check_transition=False,
        )
    else:
        # 状态还停在旧口径「已定级」的老货再提交（改档重判）：仓与状态一并搬到位
        item.ver = int(item.ver or 0) + 1
        item.updated_at = _now()
        ledger.apply(
            session, item,
            biz_type="REGRADE",
            biz_id=str(record.id),
            to_warehouse=target_warehouse,
            to_status=target_status,
            operator=tester,
            remark=f"改档重判 {prev_grade} → {final}",
            idempotent_key=f"REGRADE:{item.id}:{record.id}",
            check_transition=False,
        )
    audit.log(session, "item", item.id, "grade", before=prev_grade, after=final,
              operator=tester,
              note=("人工覆盖：" + str(override_reason)) if want and want != suggest else "引擎建议")
    # 定级了：队列里那张还没打的标签要跟着改成新等级（不然纸上还是入队时的老等级，
    # 跟实际分级对不上 —— 2026-10-13）
    labeling.sync_queue_grade(session, item)
    session.commit()
    # 价格同步（2026-10-15 用户拍板）：定级落了渔货价就推给渔枢商品档案 + 渔货栈货源，
    # 真变了还在渔货栈「通知」里落一条「商品价格变动」。桥断只降级，不影响检测提交。
    if item.fish_code and (
        round(float(item.selling_price or 0.0), 2) != old_selling
        or round(float(item.suggest_price or 0.0), 2) != old_suggest
    ):
        try:
            bridge.push_price_change(
                item.fish_code,
                name=str(item.product_name or item.title or ""),
                selling_price=float(item.selling_price or 0.0),
                suggest_price=float(item.suggest_price or 0.0),
                base_price=float(item.base_price or 0.0),
            )
        except Exception:  # noqa: BLE001  同步是账，检测是现场 —— 失败不拖提交
            import logging

            logging.getLogger("wms.inspection").warning(
                "检测定价同步渔枢 / 渔货栈失败", exc_info=True
            )
    return record


def reset_to_inbound(
    session: Session, item: WmsItem, *, operator: str = "", reason: str = ""
) -> WmsItem:
    """把货品退回「刚入库的原始数据状态」（2026-10-08 用户要求）。

    **什么时候用**：这台货的检测结果不该算数 —— 误检 / 拿它练手 / 单据录错，
    要让它回到「刚收进来、还没检测」的样子重走一遍。

    **做什么**：状态回「待检」、等级回 W（未定级）、渔货价与定级痕迹清空、回待检仓。
    **不做什么**：检测记录一条不删（复检轮次接着往下走，历史查得到）、库位保留
    （货还搁在那个架子上）、组号与货号一律不动。

    最后 ``ledger.rebuild()`` 重算余额 —— 等级从 X 变回 W，库存桶要跟着搬；
    这是纠偏动作，宁可全量重算也不要留一个对不上的桶。
    """
    status_now = str(item.status or "").upper()
    if status_now in TERMINAL_STATUSES:
        raise ValueError(
            f"「{STATUS_LABELS.get(status_now, status_now)}」是终态，不能再退回入库"
            "（已售只能走召回，危废 / 拆机件不可逆）"
        )
    if item.locked_by:
        raise ValueError(f"这台货被「{item.locked_by}」锁着（订单占用），先取消锁货再退回")
    before = f"{STATUS_LABELS.get(status_now, status_now or '—')} / {grade_label(item.grade)}"

    # ① 按当前事实把货移回待检仓：流水只记「从哪来、回哪去」，等级桶随后由 rebuild 搬正
    ledger.apply(
        session, item,
        biz_type="RESET",
        biz_id=str(item.id),
        to_warehouse=WH_PENDING,
        to_status=STATUS_PENDING,
        operator=operator,
        remark=f"退回入库原始状态{('：' + reason) if reason else ''}",
        idempotent_key=f"RESET:{item.id}:{int(item.ver or 0)}",
        check_transition=False,
    )
    # ② 清掉定级与定价的痕迹（照片 / 检项明细在 WmsTestRecord 里，一条没丢）
    item.grade = GRADE_W
    item.selling_price = 0.0
    item.residual_value = 0.0
    item.grade_reason = ""
    item.graded_at = None
    item.need_recheck = False
    item.safety_hit = False
    item.flaw_desc = ""
    item.battery_health = None
    item.photos = []
    item.updated_at = _now()
    # ③ 余额重算：等级变了，桶要跟着搬
    ledger.rebuild(session)
    audit.log(session, "item", item.id, "reset", before=before,
              after=f"待检 / {grade_label(GRADE_W)}", operator=operator,
              note=reason or "退回入库的原始数据状态")
    # 退回入库 = 等级变回 W：队列里没打的标签也跟着回去（同上，别打出错的等级）
    labeling.sync_queue_grade(session, item)
    session.commit()
    return item


def _next_round(session: Session, item_id) -> int:
    value = session.scalar(
        select(func.coalesce(func.max(WmsTestRecord.round), 0)).where(
            WmsTestRecord.item_id == item_id
        )
    )
    return int(value or 0) + 1


def _save_items(
    session: Session,
    record: WmsTestRecord,
    item: WmsItem,
    template: tuple[tuple[str, str], ...],
    results: dict[str, str],
    fail_descs: dict[str, str],
    allow_empty: bool = False,
) -> None:
    for idx, (key, label) in enumerate(template):
        result = str(results.get(key) or "").lower()
        if result not in (TEST_PASS, TEST_FAIL, TEST_NA):
            if allow_empty:
                result = TEST_NA
            else:
                # 没勾的项按「不适用」存，但功能项漏勾会在 all_items_pass 里体现出来
                result = TEST_NA
        session.add(
            WmsTestItem(
                record_id=record.id,
                item_id=item.id,
                item_key=key,
                item_label=label,
                result=result,
                fail_desc=str(fail_descs.get(key) or ""),
                sort=idx,
                created_at=_now(),
            )
        )
    session.flush()


def records_of(session: Session, item_id) -> list[WmsTestRecord]:
    """一台货的全部检测记录（多次：初检 / 复检 / 修回重检），只追加不覆盖。"""
    return list(
        session.scalars(
            select(WmsTestRecord)
            .where(WmsTestRecord.item_id == item_id)
            .order_by(WmsTestRecord.id.asc())
        ).all()
    )


def items_of(session: Session, record_id: int) -> list[WmsTestItem]:
    return list(
        session.scalars(
            select(WmsTestItem)
            .where(WmsTestItem.record_id == record_id)
            .order_by(WmsTestItem.sort.asc(), WmsTestItem.id.asc())
        ).all()
    )


def recheck_pool(session: Session, limit: int = 50) -> list[WmsItem]:
    """复检池：存疑 / 待复检的货，**不隔夜**（列表按时间排序，越早越急）。"""
    return list(
        session.scalars(
            select(WmsItem)
            .where(WmsItem.need_recheck.is_(True))
            .order_by(WmsItem.updated_at.asc())
            .limit(limit)
        ).all()
    )


def pending_pool(session: Session, limit: int = 50) -> list[WmsItem]:
    """待检池：已建档未检测（检测工作台扫进来就是这批）。"""
    return list(
        session.scalars(
            select(WmsItem)
            .where(WmsItem.status == state.STATUS_PENDING)
            .order_by(WmsItem.id.asc())
            .limit(limit)
        ).all()
    )


def tester_stats(session: Session, limit: int = 20) -> list[dict]:
    """检测产能 + 准确率：人均台数、一次通过率、复检率、与引擎的一致率。"""
    rows = list(
        session.execute(
            select(
                WmsTestRecord.tester,
                func.count(WmsTestRecord.id).label("total"),
            )
            .where(WmsTestRecord.tester != "")
            .group_by(WmsTestRecord.tester)
            .order_by(func.count(WmsTestRecord.id).desc())
        ).all()
    )
    out = []
    for tester, total in rows:
        records = list(
            session.scalars(
                select(WmsTestRecord).where(WmsTestRecord.tester == tester)
            ).all()
        )
        consistent = sum(
            1 for r in records
            if r.final_grade and r.suggest_grade and r.final_grade == r.suggest_grade
        )
        graded = sum(1 for r in records if r.final_grade)
        rechecked = sum(1 for r in records if r.is_recheck)
        out.append(
            {
                "tester": tester,
                "total": int(total or 0),
                "graded": graded,
                "recheck": rechecked,
                "consistent": consistent,
                "rate": round(consistent / graded, 4) if graded else 0.0,
            }
        )
    out.sort(key=lambda row: row["total"], reverse=True)
    return out[:limit]


def category_name(value: str) -> str:
    return category_label(value)
