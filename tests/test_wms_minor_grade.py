"""渔集 v1.0.29：列表一格一行 + 「不影响使用的缺陷」判 B 缺/瑕疵/可卖。

用户 2026-10-13 两件事：

① **列表不要换行** —— 台账一行拉成三屏高根本没法报数 / 对账；只有「产品名称」
   这类特别长的字段留两行（第三行省略）。
② **面板 / 显示有问题、少本说明书这类「不影响使用」的缺陷判 B（照常卖）**
   —— 以前会掉到「没有规则完全命中 → 兜底判 B」，档位碰巧对了，但检测员看到的
   是「未命中规则」四个字，只会以为系统没判明白。现在有一条专门的规则把它说清楚。
"""
from __future__ import annotations

import pathlib

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
from wms.models import (
    STATUS_SELLABLE,
    WmsBatch,
    WmsGradeRule,
    WmsItem,
    WmsStockBalance,
    WmsStockLedger,
    WmsTestRecord,
    WmsTestItem,
)

# 与 scripts/wms_minor_defect_rule.py（线上一次性迁移）同一份
MINOR_RULE_TITLE = "B 缺/瑕疵/可卖：只有不影响使用的缺陷（面板 / 显示、非关键配件）"
MINOR_CONDITIONS = {
    "all_of": [
        {"field": "minor_defect", "op": "==", "value": True},
        {"field": "functional_fail", "op": "==", "value": False},
        {"field": "missing_parts", "op": "==", "value": False},
        {"field": "must_official", "op": "==", "value": False},
    ]
}


def _ensure_minor_rule(session) -> WmsGradeRule:
    """规则表不是空的，启动播种不会再补（2026-10-10 起的老规矩）—— 这里手动补上，
    与 scripts/wms_minor_defect_rule.py（线上一次性迁移）同一份条件。"""
    row = session.scalar(select(WmsGradeRule).where(WmsGradeRule.title == MINOR_RULE_TITLE))
    if row is None:
        row = WmsGradeRule(
            category="*", priority=25, grade="B", safety_flag=False,
            title=MINOR_RULE_TITLE, reason="不影响使用：降价值后照常售卖",
            conditions=MINOR_CONDITIONS,
        )
        session.add(row)
        session.commit()
    return row


class WmsMinorDefectGradeTest(unittest.TestCase):
    """「面板 / 显示、缺非关键配件」这类不影响使用的问题 → B 缺/瑕疵/可卖。"""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
            wms_permissions.ensure_default_roles(s)
            _ensure_minor_rule(s)
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

    # ------------------------------------------------------------ 建货（家电类：有 panel / accessory）
    def _appliance(self) -> WmsItem:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "recycle", "qty_expected": 1,
                    "supplier_name": "v1.0.29 测试"},
                operator="tester",
            )
            item = intake.register(
                s, batch.id,
                [{"sku_code": "A-1029", "product_name": "测试电风扇",
                  "category": "HA", "purchase_cost": 40, "base_price": 100}],
                operator="tester",
            )[0]
            s.commit()
            return s.get(WmsItem, item.id)

    def test_10_facts_minor_vs_functional(self):
        """facts：面板 / 配件归「不影响使用」，其余功能项归「影响使用」。"""
        item = self._appliance()
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            all_pass = {k: "pass" for k, _l in inspection.full_template(row.category)}
            facts = inspection.facts_of(
                row, {**all_pass, "panel": "fail"}, None, "")
            self.assertTrue(facts["minor_defect"], "面板不合格 → 有不影响使用的缺陷")
            self.assertFalse(facts["functional_fail"], "只有面板坏 → 没有影响使用的故障")
            # 通电也坏了就是真故障
            facts2 = inspection.facts_of(
                row, {**all_pass, "panel": "fail", "power": "fail"}, None, "")
            self.assertTrue(facts2["minor_defect"])
            self.assertTrue(facts2["functional_fail"], "通电失败 → 影响使用")
            # 什么都不坏：两个都 False
            facts3 = inspection.facts_of(row, all_pass, None, "")
            self.assertFalse(facts3["minor_defect"])
            self.assertFalse(facts3["functional_fail"])

    def test_20_panel_fail_suggests_b_sellable(self):
        """只有「面板 / 显示」不合格：明确判 B（可卖），且规则说得出依据。"""
        item = self._appliance()
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            all_pass = {k: "pass" for k, _l in inspection.full_template(row.category)}
            out = grading.evaluate(
                s, row.category,
                inspection.facts_of(row, {**all_pass, "panel": "fail"}, None, ""))
            self.assertEqual(out["suggest_grade"], "B")
            self.assertFalse(out["blocked"], "不再是「没命中规则」的兜底判 B")
            self.assertIn("不影响使用", out["rule_title"])
            self.assertIn("不影响使用", "；".join(out["reasons"]),
                          "依据里要写明为什么还能卖")

    def test_30_functional_fail_not_minor_b(self):
        """真有功能故障就不走这条 B —— 留给维修 / 报修 / 报废那几条规则。"""
        item = self._appliance()
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            all_pass = {k: "pass" for k, _l in inspection.full_template(row.category)}
            out = grading.evaluate(
                s, row.category,
                inspection.facts_of(row, {**all_pass, "panel": "fail", "power": "fail"},
                                    None, ""))
            self.assertNotIn("不影响使用", out["rule_title"],
                             "有真故障时不该再落到「只有不影响使用的缺陷」这条")

    def test_40_submit_panel_fail_lands_sale_warehouse(self):
        """提交检测：判 B 后照样直接进可售仓「可售」（2026-10-13 的自动入仓口径）。"""
        item = self._appliance()
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            all_pass = {k: "pass" for k, _l in inspection.full_template(row.category)}
            inspection.submit(
                s, row, tester="tester",
                results={**all_pass, "accessory": "fail"},
                fail_descs={"accessory": "缺说明书"}, flaw_desc="无明显瑕疵",
            )
            s.commit()
            row = s.get(WmsItem, item.id)
            self.assertEqual(str(row.grade), "B")
            self.assertEqual(str(row.status), STATUS_SELLABLE, "B 档是可卖的")
            self.assertEqual(str(row.warehouse_code), "SALE", "直接进可售仓")

    def test_50_item_json_marks_minor_items(self):
        """检测台按 minor 标记显示「不影响使用」，检测员看到红叉不会以为要报废。"""
        item = self._appliance()
        data = self.client.get(f"/wms/api/item.json?code={item.barcode}").json()
        minors = [t["key"] for t in data["template"] if t.get("minor")]
        self.assertIn("panel", minors)
        self.assertIn("accessory", minors)
        self.assertNotIn("power", minors, "通电坏了是真故障，不能标「不影响使用」")
        page = self.client.get(f"/wms/inspect?code={item.barcode}").text
        self.assertIn("minor-tag", page, "页面上有「不影响使用」的小标签样式")
        self.assertIn("只缺说明书 / 遥控器这类非关键配件", page,
                      "「缺件严重」那一勾要写清：只缺说明书别勾，勾了会判 D")


class WmsTableNoWrapTest(unittest.TestCase):
    """列表一格一行：表宽了交给 .table-wrap 横向滚动，不在单元格里竖着长。"""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
            wms_permissions.ensure_default_roles(s)
        ensure_default_admin()
        cls.client = TestClient(app)

    def setUp(self) -> None:
        self.client.cookies.set(COOKIE_NAME, make_session(1)[0])

    def test_10_body_cells_never_wrap(self):
        shell = self.client.get("/wms/items").text
        # 2026-10-15：设计系统 CSS 从 base.html 内联搬到了外部静态文件（能被浏览器缓存），
        # 这几条规则的守护改成查那张样式表，再确认页面确实引了它 —— 规则一条都不能丢。
        css = pathlib.Path(__file__).resolve().parents[1].joinpath(
            "app", "static", "wms_design.css").read_text(encoding="utf-8")
        self.assertIn("/static/wms_design.css", shell, "页面要引这张外部样式表")
        self.assertIn("tbody td{white-space:nowrap}", css, "列表一律一格一行")
        self.assertIn("td.wrap2{white-space:normal", css, "长字段才准换行")
        self.assertIn("-webkit-line-clamp:2", css, "长字段最多两行")

    def test_20_ledger_name_cell_is_two_lines(self):
        """台账的「产品名称」是唯一的例外：最多两行，第三行省略。"""
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "recycle", "qty_expected": 1,
                    "supplier_name": "v1.0.29 换行测试"},
                operator="tester",
            )
            intake.register(
                s, batch.id,
                [{"sku_code": "A-1029L", "product_name": "测试" + "超长产品名" * 6,
                  "category": "HA", "purchase_cost": 40, "base_price": 100}],
                operator="tester",
            )
            s.commit()
        page = self.client.get("/wms/items?q=A-1029L").text
        self.assertIn('class="wrap2"', page, "产品名列用两行截断，不是 clip 省略号")
        self.assertIn('class="clamp2"', page)
        self.assertNotIn('<td class="clip"', page, "列表里不再有 clip 的行")
        # 收尾：删掉演练货
        with SessionLocal() as s:
            s.query(WmsItem).delete(synchronize_session=False)
            s.query(WmsBatch).delete(synchronize_session=False)
            s.commit()


if __name__ == "__main__":
    unittest.main()
