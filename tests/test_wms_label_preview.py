"""渔集 v1.0.11：标签可视化预览 + 检测台渔货价 + 商品字段去备注 + 退回入库原始状态。

对应用户 2026-10-08 提的四件事：
  ① 渔集的更新日志从 v1.0.1 起独立编号（断言在 tests/test_updates.py）；
  ② 商品字段直接用字段名，去掉「（来自 某表.xlsx（2610-03））」这串备注（出处留 hover）；
  ③ 检测后的渔货价按「分级规则」的价格系数出默认金额，并从标题到数字都醒目；
  ④ 标签内容的可视化预览（真 Code128）；外加货品详情页的「退回入库原始状态」。
"""
from __future__ import annotations

import re
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
from wms import barcode128, grading, importer, inspection, intake, label_preview, labeling  # noqa: E402
from wms import audit as wms_audit  # noqa: E402
from wms import ledger, permissions as wms_permissions, seed as wms_seed  # noqa: E402
from wms import print_settings  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import (  # noqa: E402
    STATUS_GRADED,
    STATUS_SELLABLE,
    STATUS_HAZARD,
    STATUS_LABELS,
    STATUS_PENDING,
    WH_PENDING,
    WmsAuditLog,
    WmsBatch,
    WmsItem,
    WmsItemField,
    WmsLabelPrint,
    WmsPriceRule,
    WmsStockBalance,
    WmsStockLedger,
    WmsTestRecord,
)

SOURCE_NOTE = "河南仓库第一批清单.xlsx （2610-03）"


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsLabelPrint).delete(synchronize_session=False)
        s.query(WmsTestRecord).delete(synchronize_session=False)
        s.query(WmsStockLedger).delete(synchronize_session=False)
        s.query(WmsStockBalance).delete(synchronize_session=False)
        s.query(WmsAuditLog).delete(synchronize_session=False)
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsBatch).delete(synchronize_session=False)
        s.commit()
        ledger.rebuild(s)


class BarcodeAndPreviewTest(unittest.TestCase):
    """预览的两块地基：Code128 位串对不对、XML 能不能画出来（都不碰库）。"""

    def test_10_code128_checksum(self):
        """校验位 = (起始符 + Σ 值 × 位置) % 103，停止符固定 13 模块。"""
        text = "QCHA26100003"
        values = [104] + [ord(c) - 32 for c in text]
        checksum = (values[0] + sum((i + 1) * v for i, v in enumerate(values[1:]))) % 103
        got = barcode128.values_of(text)
        self.assertEqual(got[: len(values)], values, "起始符与每个字符的值")
        self.assertEqual(got[-2], checksum)
        self.assertEqual(got[-1], barcode128.STOP)
        bits = barcode128.encode(text)
        self.assertTrue(bits.startswith(barcode128.PATTERNS[104]))
        self.assertTrue(bits.endswith("1100011101011"))
        self.assertEqual(len(bits), 11 * (len(text) + 2) + 13)
        self.assertEqual(barcode128.modules(text), len(bits))

    def test_11_svg_bars(self):
        """条码画成 SVG：只有条被画出来（连续条合成一条 path）。"""
        svg = label_preview.bars_svg(barcode128.encode("AB12"), 2.0, 20)
        self.assertIn("<path", svg)
        self.assertIn('fill="#111"', svg)
        width = float(svg.split('width="')[1].split('"')[0])
        self.assertAlmostEqual(width, barcode128.modules("AB12") * 2.0, places=2)

    def test_12_render_default_template(self):
        """默认模板能画出预览：纸面尺寸、文字、条码、页脚信息都在。"""
        xml = print_settings.render(
            print_settings.DEFAULT_TEMPLATE,
            {"barcode": "QCHA26100003", "display_code": "QC-HA-2610-0003", "grade": "S",
             "grade_label": "S 级准新机", "action": "直接上架", "category": "HA",
             "title": "电视支架", "date": "2026-10-08"},
            width=60, height=40, gap=2, copies=1,
        )
        result = label_preview.render(xml)
        self.assertTrue(result["ok"])
        self.assertIn("lbl-paper", result["html"])
        self.assertIn("QC-HA-2610-0003", result["html"])
        self.assertIn("<path", result["html"], "条码要画成真条纹")
        self.assertEqual(result["counts"].get("BARC"), 1)
        self.assertIn("60 × 40 mm", result["footer"])
        self.assertIn("203dpi", result["footer"])
        self.assertIn("一维条码 1", result["count_text"])

    def test_12b_text_scales_with_xw_yh(self):
        """xw / yh 放大倍数要进预览 —— 不放大就会出现「预览正好、打出来大字压行」。"""
        base = ('<PAGE w="60" l="40" ld="2" dpi="203"><LABEL id="1">'
                '<TEXT x="2" y="2" f="6" a="center">{t}</TEXT></LABEL></PAGE>')
        plain = label_preview.render(base.replace("{t}", "A"))["html"]
        big = label_preview.render(
            base.replace('<TEXT x="2" y="2" f="6"', '<TEXT x="2" y="2" xw="3" yh="2" f="6"')
                .replace("{t}", "A"))["html"]
        font = re.search(r"font-size:([\d.]+)px", plain)
        font_big = re.search(r"font-size:([\d.]+)px", big)
        self.assertIsNotNone(font)
        self.assertAlmostEqual(float(font_big.group(1)), float(font.group(1)) * 2, places=1,
                               msg="yh=2 字高要翻倍")
        self.assertIn("scaleX(1.500)", big, "xw=3 / yh=2 → 横向再放大 1.5 倍")
        # 顺序必须是 scaleX 在前，百分比位移才跟着一起放大（不然居中会跑偏）
        self.assertRegex(big, r"transform:scaleX\(1\.500\) translateX\(-50%\)")
        self.assertIn("transform-origin:left top", big)

    def test_13_render_kinds_and_bad_template(self):
        """二维码 / 直线 / 方框都认；写坏的模板不抛异常，只回 ok=False。"""
        xml = (
            '<PAGE w="50" l="30" ld="0" dpi="203"><LABEL id="1" c="2">'
            '<TEXT x="25" y="2" f="6" a="center">QC-HA-0001</TEXT>'
            '<BARC x="25" y="8" h="50" s="2" a="center" ct="code128">QCHA0001</BARC>'
            '<QR x="40" y="20" h="30">https://x.co/a</QR>'
            '<LINE x="2" y="28" x2="48" y2="28" w="2"/>'
            '<RECT x="1" y="1" w="48" h="28" lw="2"/>'
            "</LABEL></PAGE>"
        )
        result = label_preview.render(xml)
        kinds = [e["kind"] for e in label_preview.elements_of(xml)]
        self.assertEqual(kinds, ["TEXT", "BARC", "QR", "LINE", "RECT"])
        self.assertIn("第 1 张 × 2", result["html"], "份数要标出来")
        for broken in ("<PAGE w=\"60\" oops", "", "<PAGE><LABEL id=\"1\"></LABEL></PAGE>"):
            self.assertFalse(label_preview.render(broken)["ok"], broken)


class WmsV1011Test(unittest.TestCase):
    """要碰库 / 页面的部分。"""

    @classmethod
    def setUpClass(cls):
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
            wms_permissions.ensure_default_roles(s)
        ensure_default_admin()
        cls.client = TestClient(app)

    def setUp(self):
        _wipe()
        self.client.cookies.set(COOKIE_NAME, make_session(1)[0])

    # ---------------------------------------------------------- 建货
    def _item(self, *, base_price: float = 100.0, cost: float = 40.0) -> WmsItem:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "recycle", "qty_expected": 1,
                    "supplier_name": "v1.0.11 测试"},
                operator="tester",
            )
            item = intake.register(
                s, batch.id,
                [{"brand": "小米", "model": "MB-FB30", "base_price": base_price,
                  "purchase_cost": cost, "sku_code": "8617774630925303"}],
                operator="tester",
            )[0]
            return s.get(WmsItem, item.id)

    def _grade(self, item: WmsItem, grade: str, **kw) -> WmsTestRecord:
        # A 档（微瑕）必须填「外观瑕疵描述」（2026-10-12 硬校验）：这里给一句默认的，
        # 要测「不填被拒」自己显式传 flaw_desc=""（段描述会带进商品上架的供应商友情提示）
        if grade == "A":
            kw.setdefault("flaw_desc", "外壳轻微划痕（测试）")
        # 复检（第 2 轮起）必填覆盖理由（2026-10-15）：helper 统一带上，专门测
        # 「不填被拦」的用例自己显式传 recheck_reason=""
        kw.setdefault("recheck_reason", "测试：复检覆盖")
        with SessionLocal() as s:
            row = inspection.submit(
                s, s.get(WmsItem, item.id), tester="tester", results={},
                final_grade=grade, override_reason=kw.pop("override_reason", "测试定档"), **kw
            )
            return s.get(WmsTestRecord, row.id)

    # ---------------------------------------------------------- ② 商品字段只留字段名
    def test_20_field_label_without_source_note(self):
        with SessionLocal() as s:
            importer.ensure_field(s, "promise_at", "下单时间", "text", source=SOURCE_NOTE)
            s.commit()
        item = self._item()
        page = self.client.get(f"/wms/items/{item.id}/edit")
        self.assertEqual(page.status_code, 200)
        self.assertIn("下单时间", page.text)
        self.assertNotIn("（来自", page.text, "字段名后面不再挂「（来自 …）」")
        self.assertNotIn(SOURCE_NOTE, page.text, "建档 / 编辑页彻底不出现出处备注")
        fields_page = self.client.get("/wms/fields")
        self.assertNotIn("来源：", fields_page.text)
        self.assertIn('title="出处：', fields_page.text)

    # ---------------------------------------------------------- ③ 检测台渔货价
    def test_30_inspect_page_shows_price_card(self):
        page = self.client.get("/wms/inspect")
        self.assertEqual(page.status_code, 200)
        for token in ("price-card", "渔货价", "manualPrice", "系统默认",
                      "价格系数", "manual_price"):
            self.assertIn(token, page.text, token)

    def test_31_submit_uses_factor_then_manual_price(self):
        """默认价 = 基准价 × 该档系数；检测台填了就按人填的，并写审计。"""
        item = self._item(base_price=100.0)
        with SessionLocal() as s:
            factor = float(grading.price_rule_of(s, "A").factor)
        record = self._grade(item, "A")
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            self.assertAlmostEqual(row.selling_price, round(100.0 * factor, 2))
            self.assertEqual(str(row.grade), "A")
            self.assertEqual(str(row.status), STATUS_SELLABLE)

        # 硬规矩（2026-10-10）：**渔货价不能高于建议售价** ——
        # A 档建议售价 = 基准价 100 × 建议售价系数 1.0 = 100，填 888.5 会被压回 100
        self._grade(item, "A", manual_price=888.5)
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            self.assertAlmostEqual(row.selling_price, 100.0, "高于建议售价 → 压到建议售价")

        # 把该档的建议售价系数抬上去，人工价才落得下去（并留下一笔 price 审计）
        with SessionLocal() as s:
            grading.price_rule_of(s, "A").suggest_factor = 10.0
            s.commit()
        self._grade(item, "A", manual_price=888.5)
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            # 人工改价也按整数口径向下取整
            self.assertAlmostEqual(row.selling_price, 888.0)
            log = s.scalar(
                select(WmsAuditLog).where(
                    WmsAuditLog.entity == "item", WmsAuditLog.action == "price"
                ).order_by(WmsAuditLog.id.desc())
            )
            self.assertIsNotNone(log, "人工改价要留审计")
            self.assertIn("人工改价", log.note)
            # 人工价 888.5 也按整数口径向下取整落库并写进审计
            self.assertIn("888.00", log.after)
        self.assertTrue(record.id)

    # ---------------------------------------------------------- 退回入库原始状态
    def test_40_reset_to_inbound(self):
        item = self._item(base_price=100.0)
        self._grade(item, "S")
        with SessionLocal() as s:
            before_tests = len(s.scalars(
                select(WmsTestRecord).where(WmsTestRecord.item_id == item.id)).all())
            row = s.get(WmsItem, item.id)
            self.assertEqual(str(row.status), STATUS_SELLABLE)
            self.assertNotEqual(str(row.grade), "W")
            self.assertGreater(row.selling_price, 0)
            inspection.reset_to_inbound(s, row, operator="tester", reason="误检，退回重来")
            row = s.get(WmsItem, item.id)
            self.assertEqual(str(row.status), STATUS_PENDING)
            self.assertEqual(str(row.warehouse_code), WH_PENDING, "回待检仓")
            self.assertEqual(str(row.grade), "W", "等级回未定级")
            self.assertEqual(row.selling_price, 0.0)
            self.assertEqual(row.residual_value, 0.0)
            self.assertIsNone(row.graded_at)
            self.assertFalse(row.need_recheck)
            self.assertEqual(row.sku_code, "8617774630925303", "货号不动")
            after_tests = len(s.scalars(
                select(WmsTestRecord).where(WmsTestRecord.item_id == item.id)).all())
            self.assertEqual(after_tests, before_tests, "检测记录一条都不能删")
            log = s.scalar(
                select(WmsAuditLog).where(
                    WmsAuditLog.entity == "item", WmsAuditLog.action == "reset"
                ).order_by(WmsAuditLog.id.desc())
            )
            self.assertIsNotNone(log, "退回要留审计")
            self.assertIn("误检", log.note)
            # 余额按新等级归桶：待检仓里有一台 W
            bucket = s.scalar(
                select(WmsStockBalance).where(
                    WmsStockBalance.warehouse_code == WH_PENDING,
                    WmsStockBalance.status == STATUS_PENDING,
                    WmsStockBalance.grade == "W",
                )
            )
            self.assertIsNotNone(bucket, "退回后余额要落在（待检仓 / 待检 / W）")
            self.assertGreaterEqual(int(bucket.qty), 1)
        page = self.client.get(f"/wms/items/{item.id}")
        self.assertIn("退回入库原始状态", page.text)

    def test_41_reset_refuses_terminal_and_locked(self):
        item = self._item()
        self._grade(item, "D")
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            row.status = STATUS_HAZARD           # 终态之一（另一个是已售）
            s.commit()
            with self.assertRaises(ValueError) as ctx:
                inspection.reset_to_inbound(s, row, operator="tester")
            self.assertIn(STATUS_LABELS.get(STATUS_HAZARD, "危废"), str(ctx.exception))
            row = s.get(WmsItem, item.id)
            row.status = STATUS_GRADED
            row.locked_by = "订单 123"
            s.commit()
            with self.assertRaises(ValueError) as ctx2:
                inspection.reset_to_inbound(s, row, operator="tester")
            self.assertIn("锁着", str(ctx2.exception))

    # ---------------------------------------------------------- ④ 标签可视化预览
    def test_50_preview_page_from_queue(self):
        item = self._item()
        with SessionLocal() as s:
            row_id = labeling.enqueue(
                s, [s.get(WmsItem, item.id)], operator="tester",
                transport=labeling.TRANSPORT_CLOUD,
            )[0].id
        page = self.client.get(f"/wms/labels/{row_id}/preview")
        self.assertEqual(page.status_code, 200)
        self.assertIn("标签预览", page.text)
        self.assertIn("lbl-paper", page.text, "要有标签预览图")
        self.assertIn("<path", page.text, "条码按真 Code128 画")
        self.assertIn(str(item.display_code), page.text)
        self.assertIn("&lt;PAGE", page.text, "原始 XML 也一并给出来")
        # 队列页要有入口
        self.assertIn(f"/wms/labels/{row_id}/preview",
                      self.client.get("/wms/labels").text)
        # 打印设置页的模板卡也要有预览图
        settings_page = self.client.get("/wms/print-settings")
        self.assertIn("标签预览图", settings_page.text)
        self.assertIn("lbl-paper", settings_page.text)


class WmsPriceRuleEnabledTest(unittest.TestCase):
    """价格系数「启用」语义（2026-10-09 修 bug）。

    线上 S / A / B / C 四条系数在 10-08 中午被保存过一次就全变「停用」，
    检测台于是取不到系数、按 1.0 兜底 —— B 档显示「系数 1 × 基准价 ¥87.00 · 毛利 100.0%」。
    根因是保存路由把「表单没给 enabled」当成「停用」。
    """

    @classmethod
    def setUpClass(cls):
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
            wms_permissions.ensure_default_roles(s)
        ensure_default_admin()
        cls.client = TestClient(app)

    def setUp(self):
        _wipe()
        self.client.cookies.set(COOKIE_NAME, make_session(1)[0])
        # 这些用例会改价格系数的启用状态与数值，跑完还原，免得影响别的用例
        with SessionLocal() as s:
            self.saved = {
                r.id: (bool(r.enabled), float(r.factor))
                for r in s.scalars(select(WmsPriceRule)).all()
            }

    def tearDown(self):
        with SessionLocal() as s:
            for rid, (enabled, factor) in self.saved.items():
                row = s.get(WmsPriceRule, rid)
                if row:
                    row.enabled, row.factor = enabled, factor
            s.commit()

    def _item(self, *, base_price: float = 100.0, cost: float = 40.0) -> WmsItem:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "recycle", "qty_expected": 1,
                    "supplier_name": "价格系数测试"},
                operator="tester",
            )
            item = intake.register(
                s, batch.id,
                [{"brand": "小米", "model": "MB-FB30", "base_price": base_price,
                  "purchase_cost": cost, "sku_code": "8617774630925303"}],
                operator="tester",
            )[0]
            return s.get(WmsItem, item.id)

    def test_60_save_price_rule_does_not_disable_it(self):
        with SessionLocal() as s:
            rule = grading.price_rule_of(s, "B")
            rid = rule.id
        # 页面上那张「保存价格系数」表单里没有 enabled 控件 → 必须保持原状态
        res = self.client.post(
            "/wms/rules/price",
            data={"grade": "B", "factor": "0.40", "min_margin": "0.05",
                  "need_approval_below": "0.30"},
            follow_redirects=False,
        )
        self.assertEqual(res.status_code, 303)
        with SessionLocal() as s:
            row = s.get(WmsPriceRule, rid)
            self.assertTrue(row.enabled, "保存系数不该把这条规则停用（就是它坑了线上）")
            self.assertAlmostEqual(float(row.factor), 0.40)

        # 表单明确给了 enabled=0 才停用
        self.client.post("/wms/rules/price",
                         data={"grade": "B", "factor": "0.40", "enabled": "0"},
                         follow_redirects=False)
        with SessionLocal() as s:
            self.assertFalse(s.get(WmsPriceRule, rid).enabled)

        # 列表上的启用 / 停用开关
        res = self.client.post(f"/wms/rules/price/{rid}/toggle", follow_redirects=False)
        self.assertEqual(res.status_code, 303)
        with SessionLocal() as s:
            self.assertTrue(s.get(WmsPriceRule, rid).enabled)

    def test_61_disabled_coefficient_falls_back_to_one(self):
        with SessionLocal() as s:
            grading.price_rule_of(s, "B").enabled = False
            s.commit()
        with SessionLocal() as s:
            out = grading.price_of(s, "B", 87.0, 0.0)
            self.assertIsNone(out["rule_id"], "停用的档位取不到规则")
            self.assertEqual(out["factor"], 1.0)
            self.assertEqual(out["price"], 87.0)
            self.assertEqual(out["margin"], 1.0, "兜底价把毛利算成 100%")
            # 页面靠这两个标记说清「为什么是 1.0」「这价会不会被写进去」
            item = self._item(base_price=87.0)
            prev = inspection.preview(s, s.get(WmsItem, item.id), {"results": {}})
            self.assertTrue(prev["price"]["rule_missing"])
            self.assertEqual(prev["price"]["priced"],
                             prev["suggest_grade"] in ("N", "S", "A", "B"))

    def test_62_enabled_coefficient_drives_the_price(self):
        with SessionLocal() as s:
            grading.price_rule_of(s, "A").factor = 0.5
            grading.price_rule_of(s, "A").enabled = True
            s.commit()
        item = self._item(base_price=100.0)
        with SessionLocal() as s:
            inspection.submit(s, s.get(WmsItem, item.id), tester="tester", results={},
                              final_grade="A", override_reason="测试定档",
                              flaw_desc="外壳轻微划痕（测试）")
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            self.assertAlmostEqual(float(row.selling_price), 50.0, "100 × 系数 0.5")

    def test_63_grade_b_price_only_when_typed(self):
        """B 档（2026-10-13 起照常写价）：默认价 = 基准价 ×0.30，人工改价要落库并留审计。

        填了 = 人工改价，照样过「底价审批线 + 建议售价上限」两道闸（与 N / S / A 同一把尺子）。
        """
        # ① 不填：按系统默认落价（基准价 100 ×0.30 = 30）
        plain = self._item(base_price=100.0)
        with SessionLocal() as s:
            inspection.submit(s, s.get(WmsItem, plain.id), tester="tester", results={},
                              final_grade="B", override_reason="测试定档")
        with SessionLocal() as s:
            row = s.get(WmsItem, plain.id)
            self.assertAlmostEqual(float(row.selling_price or 0), 30.0, places=2)
            # 2026-10-13：定级即按等级路由入仓 —— B 档直进可售仓「可售」
            self.assertEqual(str(row.status), STATUS_SELLABLE)
            self.assertEqual(str(row.warehouse_code), "SALE")
        # ② 填了合法价：照人填的写
        with SessionLocal() as s:
            calc = grading.price_of(s, "B", 100.0, 0.0)
        floor = float(calc.get("floor") or 0)
        ceil = float(calc.get("suggest_price") or 0)
        want = round(ceil * 0.95, 2) if ceil else 80.0
        if floor and want < floor:
            want = round(floor, 2)
        item = self._item(base_price=100.0)
        with SessionLocal() as s:
            inspection.submit(s, s.get(WmsItem, item.id), tester="tester", results={},
                              final_grade="B", override_reason="测试定档", manual_price=want)
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            self.assertAlmostEqual(float(row.selling_price or 0), float(want), places=2,
                                   msg="人工定价要按人填的写进去")
            self.assertTrue(any("人工改价" in (l.note or "")
                                for l in wms_audit.of_entity(s, "item", item.id)))
        # ③ 低于审批线：拦下（不填价不受影响，填了就得守规矩）
        if floor > 0:
            low = self._item(base_price=100.0)
            with SessionLocal() as s:
                with self.assertRaises(ValueError):
                    inspection.submit(s, s.get(WmsItem, low.id), tester="tester", results={},
                                      final_grade="B", override_reason="测试定档",
                                      manual_price=round(floor - 1, 2))
        page = self.client.get("/wms/inspect")
        self.assertIn("该档不写价", page.text)

    def test_64_rules_page_has_state_switch(self):
        page = self.client.get("/wms/rules")
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="enabled"', page.text, "保存表单要能直接选启用")
        self.assertIn("/wms/rules/price/", page.text, "列表每行要能启停")
        # 按钮写的是动作：启用中的档位给「点此停用」，反过来给「点此启用」
        self.assertIn("正在用 · 点此停用", page.text)
        with SessionLocal() as s:
            rid = grading.price_rule_of(s, "C").id
        self.assertIn(f"/wms/rules/price/{rid}/toggle", page.text)
        # 停用后：徽标写明后果，按钮变成「点此启用」，别让人以为再也开不回来
        with SessionLocal() as s:
            s.get(WmsPriceRule, rid).enabled = False
            s.commit()
        page = self.client.get("/wms/rules").text
        self.assertIn("停用（按 1.0 兜底）", page)
        self.assertIn("已停用 · 点此启用", page)


if __name__ == "__main__":
    unittest.main()
