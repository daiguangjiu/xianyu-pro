"""渔集 v1.0.31：检测项统一成一套（不再按品类分数码 / 家电两套）。

用户 2026-10-13 给的口径：

· 一套 10 项（外观成色 / 内腔成色 / 开机上电 / 屏幕面板显示 / 触控按键指纹人脸 /
  充电接口电源线插头 / 摄像扬声器麦克风 / WiFi 信号蓝牙定位 / 核心功能与异响 /
  配件齐全）+ 3 项安全红线（电池鼓包漏液 / 异味焦糊火烧 / 漏电触电）；
· 每项三态：可用 / 不可用 / 不适用，**「不适用」不参与评级判定**；
· 评级：N 全新、S 完美（全项通过 + 零瑕疵）、A 微瑕（功能全过 + 仅外观瑕疵）、
  B（只有不影响使用的瑕疵：面板 / 显示、非关键配件）、C 报修、D 报废。

顺带把评级逻辑补齐：功能项判 × 就是「有影响使用的故障」→ 判 C 报修。
以前没有这条规则：真故障又没勾「必须官方修」时不命中任何规则，会掉进兜底
判 B（照常卖），这是最危险的一种漏判。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402
from wms import grading, inspection, intake, seed as wms_seed  # noqa: E402
from wms import permissions as wms_permissions  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import (  # noqa: E402
    GRADE_C,
    GRADE_D,
    GRADE_N,
    GRADE_S,
    TEST_FAIL,
    TEST_NA,
    TEST_PASS,
    WmsBatch,
    WmsGradeRule,
    WmsItem,
    WmsPriceRule,
    WmsStockBalance,
    WmsStockLedger,
    WmsTestItem,
    WmsTestRecord,
)

# 与 scripts/wms_unify_items_2026.py（线上一次性迁移）同一份
B_TITLE = "B 缺/瑕疵/可卖：缺件、微瑕（非外观）不影响售卖使用"
C_TITLE = "C 报修：有影响使用的功能故障"
MINOR_TITLE = "B 缺/瑕疵/可卖：只有不影响使用的缺陷（面板 / 显示、非关键配件）"

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
MINOR_CONDITIONS = {"all_of": [
    {"field": "minor_defect", "op": "==", "value": True},
    {"field": "functional_fail", "op": "==", "value": False},
    {"field": "missing_parts", "op": "==", "value": False},
    {"field": "must_official", "op": "==", "value": False},
]}


def _ensure_rule(session, title: str, conditions: dict, grade: str,
                 priority: int, reason: str = "", create: bool = True) -> None:
    row = session.scalar(select(WmsGradeRule).where(WmsGradeRule.title == title))
    if row is None:
        if not create:
            return          # 名字被改过的存量规则不硬造一条，免得同一档出现两条
        row = WmsGradeRule(category="*", priority=priority, grade=grade,
                           title=title, reason=reason or title,
                           conditions=conditions)
        session.add(row)
    else:
        row.conditions = conditions
    session.commit()


def _ensure_rules(session) -> None:
    """规则表不是空的，启动播种不会再补（2026-10-10 起的老规矩）。

    2026-10-13 用户拍板「每个等级只留一条规则」后，手工逐条补齐太容易跟 seed 走岔
    （C / D 各合成一条、价格系数 C/D 归 0）—— 直接清空重播，测试库与线上一字不差。
    """
    for row in session.scalars(select(WmsGradeRule)).all():
        session.delete(row)
    for row in session.scalars(select(WmsPriceRule)).all():
        session.delete(row)
    session.commit()
    wms_seed.ensure_grade_rules(session)
    wms_seed.ensure_price_rules(session)
    session.commit()


class WmsUnifiedItemsTest(unittest.TestCase):
    """一套检测项 + 三态 + 评级映射。"""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
            wms_permissions.ensure_default_roles(s)
            _ensure_rules(s)
        ensure_default_admin()
        cls.client = TestClient(app)

    def setUp(self) -> None:
        with SessionLocal() as s:
            s.query(WmsTestItem).delete(synchronize_session=False)
            s.query(WmsTestRecord).delete(synchronize_session=False)
            s.query(WmsStockLedger).delete(synchronize_session=False)
            s.query(WmsStockBalance).delete(synchronize_session=False)
            s.query(WmsItem).delete(synchronize_session=False)
            s.query(WmsBatch).delete(synchronize_session=False)
            s.commit()
        self.client.cookies.set(COOKIE_NAME, make_session(1)[0])

    # ------------------------------------------------------------------ helper
    def _item(self, category: str = "HA") -> WmsItem:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": category, "source_type": "recycle",
                    "qty_expected": 1, "supplier_name": "v1.0.31 测试"},
                operator="tester")
            item = intake.register(
                s, batch.id,
                [{"sku_code": "U-1031", "product_name": "测试电风扇",
                  "category": category, "purchase_cost": 40, "base_price": 100}],
                operator="tester")[0]
            s.commit()
            return s.get(WmsItem, item.id)

    def _all_pass(self, category: str = "HA") -> dict:
        return {k: TEST_PASS for k, _l in inspection.full_template(category)}

    def _suggest(self, results: dict, **kw) -> str:
        """走引擎问一句「这套判下来建议哪一档」。"""
        with SessionLocal() as s:
            item = self._item()
            facts = inspection.facts_of(item, results, None, kw.pop("flaw_desc", ""), **kw)
            return str(grading.evaluate(s, item.category, facts).get("suggest_grade") or "")

    # ------------------------------------------------------------------ 用例
    def test_10_one_template_for_all_categories(self):
        """所有品类同一套：10 项业务 + 3 项安全红线 = 13 项。"""
        keys = [k for k, _l in inspection.UNIFIED_ITEMS]
        self.assertEqual(keys, ["appear", "inner", "power", "panel", "touch",
                                "charge", "camera", "wifi", "function", "accessory"])
        for cat in ("HA.KC.DFB", "PH", "SZ", "OT", "CS", ""):
            self.assertEqual(inspection.template_of(cat), inspection.UNIFIED_ITEMS)
            self.assertEqual([k for k, _l in inspection.full_template(cat)],
                             keys + ["swell", "burn", "shock"])
        # 每项都要有「怎么判」的说明（检测台逐项下面的那行小字）
        for key in keys:
            self.assertTrue(inspection.ITEM_HINTS.get(key, "").strip(), key)
        # 老两套的别名还在（老 import 不至于炸）
        self.assertEqual(inspection.GENERAL_ITEMS, inspection.UNIFIED_ITEMS)
        self.assertEqual(inspection.APPLIANCE_ITEMS, inspection.UNIFIED_ITEMS)

    def test_20_na_does_not_count(self):
        """「不适用」不参与判定：既不算不合格，也不拖累全项通过。"""
        results = self._all_pass()
        results["camera"] = TEST_NA      # 这台没有摄像头
        results["wifi"] = TEST_NA        # 也没有无线
        with SessionLocal() as s:
            item = self._item()
            facts = inspection.facts_of(item, results, None, "")
            self.assertTrue(facts["all_items_pass"])
            self.assertEqual(facts["fail_count"], 0)
        self.assertEqual(self._suggest(results), GRADE_S)

    def test_30_grade_map(self):
        """评级映射：S 完美 / A 微瑕 / B 可卖 / C 报修 / D 报废。"""
        base = self._all_pass()

        # S：全项通过 + 零瑕疵
        self.assertEqual(self._suggest(dict(base)), GRADE_S)
        # N：全项通过 + 原封未拆（全新靠人工勾）
        self.assertEqual(self._suggest(dict(base), sealed=True), GRADE_N)

        # A：外观 / 内腔成色判 ×（只降成色，功能照常）
        facts_a = {**base, "appear": TEST_FAIL}
        self.assertEqual(self._suggest(facts_a), "A")
        self.assertEqual(self._suggest({**base, "inner": TEST_FAIL}), "A")

        # B：面板 / 显示、配件齐全这类「不影响使用」的缺陷
        self.assertEqual(self._suggest({**base, "panel": TEST_FAIL}), "B")
        self.assertEqual(self._suggest({**base, "accessory": TEST_FAIL}), "B")

        # C：功能项判 ×（开机 / 触控 / 充电 / 摄像 / 信号 / 核心功能）
        self.assertEqual(self._suggest({**base, "power": TEST_FAIL}), GRADE_C)
        self.assertEqual(self._suggest({**base, "function": TEST_FAIL}), GRADE_C)

        # D：缺件严重 / 无维修价值（人工勾）—— 检项全过也得判 D，不能还是 S
        self.assertEqual(self._suggest(dict(base), missing_parts=True), GRADE_D)
        # C：勾了「必须官方修」
        self.assertEqual(self._suggest(dict(base), must_official=True), GRADE_C)
        # C：安全红线（2026-10-13 口径：先修不先废）
        self.assertEqual(self._suggest({**base, "shock": TEST_FAIL}), GRADE_C)
        # D：红线 + 缺件严重 = 彻底没法修
        self.assertEqual(self._suggest({**base, "shock": TEST_FAIL}, missing_parts=True),
                         GRADE_D)

    def test_40_cosmetic_is_not_functional(self):
        """外观类（外壳 / 内腔）判 × 不算「影响使用的故障」，不会掉去报修。"""
        with SessionLocal() as s:
            item = self._item()
            facts = inspection.facts_of(
                item, {**self._all_pass(), "appear": TEST_FAIL, "inner": TEST_FAIL},
                None, "外壳有划痕")
            self.assertTrue(facts["cosmetic_defect"])
            self.assertFalse(facts["functional_fail"])
            self.assertFalse(facts["minor_defect"])
            self.assertTrue(facts["all_items_pass"], "外观判 × 不影响「功能项全过」")
            self.assertGreaterEqual(facts["flaw_count"], 1)
            # 反过来：面板（不影响使用）不是外观类
            facts2 = inspection.facts_of(
                item, {**self._all_pass(), "panel": TEST_FAIL}, None, "")
            self.assertTrue(facts2["minor_defect"])
            self.assertFalse(facts2["cosmetic_defect"])

    def test_50_submit_grade_and_warehouse(self):
        """提交实测：面板判 × → B 且进可售仓；功能项判 × → C 且进维修仓。"""
        item = self._item()
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            inspection.submit(s, row, tester="t",
                              results={**self._all_pass(), "panel": TEST_FAIL},
                              fail_descs={"panel": "面板有划痕"})
            self.assertEqual(row.grade, "B")
            self.assertEqual(row.status, "SELLABLE")

        item = self._item("PH")
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            inspection.submit(s, row, tester="t",
                              results={**self._all_pass(), "power": TEST_FAIL},
                              fail_descs={"power": "不开机"})
            self.assertEqual(row.grade, GRADE_C, "真故障 → 判 C 报修")
            self.assertEqual(row.status, "OUT_REPAIR")

    def test_60_api_and_page(self):
        """接口与检测台：13 项、每项带说明与分类标记。"""
        item = self._item()
        with SessionLocal() as s:
            barcode = s.get(WmsItem, item.id).barcode
        data = self.client.get("/wms/api/item.json",
                               params={"code": barcode}).json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["template"]), 13)
        self.assertEqual(data["safety_keys"], ["swell", "burn", "shock"])
        by_key = {t["key"]: t for t in data["template"]}
        self.assertTrue(by_key["panel"]["minor"])
        self.assertTrue(by_key["accessory"]["minor"])
        self.assertTrue(by_key["appear"]["cosmetic"])
        self.assertTrue(by_key["inner"]["cosmetic"])
        self.assertIn("发丝划痕", by_key["appear"]["hint"])
        self.assertIn("电源线", by_key["charge"]["hint"])

        page = self.client.get("/wms/inspect").text
        self.assertIn("不适用", page, "三态：不可用 / 不适用 要分清楚")

    def test_70_history_merge_covers_old_keys(self):
        """历史归并脚本：老 key 全部有归宿，映射结果都在新模板里。"""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        try:
            import wms_unify_items_2026 as mig
        finally:
            sys.path.pop(0)

        new_keys = {k for k, _l in inspection.UNIFIED_ITEMS} | set(inspection.SAFETY_ITEM_KEYS)
        old_keys = {"boot", "screen", "touch", "charge", "camera", "speaker", "mic",
                    "button", "wifi", "bluetooth", "appear", "power", "function",
                    "panel", "noise", "accessory", "plug", "safety"}
        for key in old_keys:
            self.assertIn(key, mig.OLD_TO_NEW, key)
            self.assertIn(mig.OLD_TO_NEW[key], new_keys, key)
        for key in new_keys:
            self.assertIn(key, mig.NEW_LABELS, key)
