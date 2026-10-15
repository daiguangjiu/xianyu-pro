"""出库管理改版 + 检测台人工选档（2026-10-15 用户五条）。

1. 出库单详情页：订单信息置顶、收货人可复制、关键信息分区高亮，原「类型/状态/
   件数/发货仓」四张统计卡撤掉；类型 / 状态一律中文。
2. 出库核销把渔货号库存减到 0 → 渔枢商品自动下架，商品列表打「售罄」标。
3. 新建出库单支持渔货号挂单（与详情页订单商品面板同一口径）；列表类型 / 状态中文。
4. 检测台「最终档位」人工选档 → 评级 / 价格 / 标签等级立即按所选档重算。
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
from app.models import Product  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402
from wms import intake, outbound, seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import (  # noqa: E402
    WmsBatch, WmsItem, WmsOutboundItem, WmsOutboundOrder,
)


def _wipe() -> None:
    from wms.models import WmsLabelPrint, WmsStockBalance, WmsStockLedger

    with SessionLocal() as s:
        s.query(WmsLabelPrint).delete(synchronize_session=False)
        s.query(WmsStockLedger).delete(synchronize_session=False)
        s.query(WmsStockBalance).delete(synchronize_session=False)
        s.query(WmsOutboundItem).delete(synchronize_session=False)
        s.query(WmsOutboundOrder).delete(synchronize_session=False)
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsBatch).delete(synchronize_session=False)
        # 先删 YS 商品的规格行（SQLite 复用 product id，残留 SKU 会串到新商品上）
        from app.models import ProductSku

        ys_ids = [int(x[0]) for x in s.query(Product.id).filter(
            Product.code.like("YS%")).all()]
        if ys_ids:
            s.query(ProductSku).filter(ProductSku.product_id.in_(ys_ids)).delete(
                synchronize_session=False)
        s.query(Product).filter(Product.code.like("YS%")).delete(
            synchronize_session=False)
        s.commit()


class OutboundReworkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
        ensure_default_admin()
        cls.client = TestClient(app)

    def setUp(self) -> None:
        _wipe()
        self.client.cookies.set(COOKIE_NAME, make_session(1)[0])

    def tearDown(self) -> None:
        _wipe()

    def _wms_item(self, qty: int = 2, grade: str = "S") -> tuple[int, str, str]:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "recycle", "qty_expected": qty,
                    "supplier_name": "出库改版测试"}, operator="tester")
            item = intake.register(
                s, batch.id,
                [{"brand": "美的", "product_name": "改版测试破壁机", "qty": qty,
                  "purchase_cost": 100, "base_price": 300, "sku_code": "T-OB"}],
                operator="tester")[0]
            row = s.get(WmsItem, item.id)
            row.status = "SELLABLE"
            row.grade = grade
            row.warehouse_code = "SALE"
            s.commit()
            return int(row.id), str(row.barcode), str(row.fish_code or "")

    def _product(self, code: str, stock: int = 2, status: str = "on_sale") -> int:
        with SessionLocal() as s:
            p = Product(name="改版测试破壁机", code=code, stock=stock,
                        status=status, stock_enabled=True,
                        source_type="self_operated",
                        category="家用电器 / 厨房电器 / 家用破壁机")
            s.add(p)
            s.commit()
            return int(p.id)

    # ------------------------------------------------ ① 详情页重排（中文 + 置顶）
    def test_10_detail_no_stat_cards_chinese_labels(self):
        item_id, barcode, fish = self._wms_item()
        with SessionLocal() as s:
            order = outbound.create(
                s, {"type": "SALE", "warehouse_code": "SALE",
                    "target_no": "XY-TEST"}, operator="t")
            oid = int(order.id)
        page = self.client.get(f"/wms/outbound/{oid}")
        self.assertEqual(page.status_code, 200, page.text[:300])
        html = page.text
        # 四张统计卡撤掉了
        self.assertNotIn('<div class="stat">', html)
        # 顶部信息卡 + 类型 / 状态中文
        self.assertIn("出库单信息", html)
        self.assertIn("销售出库", html)
        self.assertIn("草稿", html)
        self.assertIn("发货仓", html)
        # 关键信息分区高亮的壳在
        self.assertIn("od-wrap", html)
        self.assertIn("od-sec hl", html)

    def test_15_detail_template_has_copy_support(self):
        """收货人复制：模板里要有一键复制按钮与兜底脚本（渔货栈订单面板）。"""
        from pathlib import Path

        tpl = (Path(__file__).resolve().parents[1]
               / "wms/templates/wms_outbound_detail.html").read_text("utf-8")
        self.assertIn('id="copyRecv"', tpl)
        self.assertIn("data-text", tpl)
        self.assertIn("execCommand", tpl, "非安全上下文兜底复制")

    # ------------------------------------------------ ② 建单挂渔货号
    def test_20_create_with_fish_code(self):
        item_id, barcode, fish = self._wms_item(qty=1)
        with SessionLocal() as s:
            batch = s.query(WmsBatch).order_by(WmsBatch.id.desc()).first()
            second = intake.register(
                s, batch.id,
                [{"brand": "美的", "product_name": "改版测试破壁机", "qty": 1,
                  "purchase_cost": 100, "base_price": 300, "sku_code": "T-OB2",
                  "fish_code": fish}],
                operator="tester")[0]
            s.get(WmsItem, second.id).status = "SELLABLE"
            s.get(WmsItem, second.id).grade = "S"
            s.get(WmsItem, second.id).warehouse_code = "SALE"
            s.commit()
        resp = self.client.post(
            "/wms/outbound",
            data={"type": "SALE", "warehouse_code": "SALE", "target_no": "XY-F",
                  "fish_code": fish, "qty": "2"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:300])
        with SessionLocal() as s:
            order = s.query(WmsOutboundOrder).order_by(
                WmsOutboundOrder.id.desc()).first()
            lines = list(s.scalars(select(WmsOutboundItem).where(
                WmsOutboundItem.outbound_id == order.id)))
            self.assertEqual(len(lines), 2, "渔货号名下 2 台都挂上了")
            self.assertEqual(int(order.item_count), 2)
            item = s.get(WmsItem, item_id)
            self.assertEqual(str(item.status or ""), "LOCKED", "可售自动锁货")

    def test_25_create_with_unknown_fish_code_still_creates(self):
        self._wms_item()
        resp = self.client.post(
            "/wms/outbound",
            data={"type": "SALE", "warehouse_code": "SALE",
                  "fish_code": "YS209912310009"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(
                s.query(WmsOutboundOrder).count(), 1, "挂不上不拦建单")

    # ------------------------------------------------ ③ 列表页中文
    def test_30_list_chinese_type_status(self):
        self._wms_item()
        resp = self.client.post(
            "/wms/outbound",
            data={"type": "SALE", "warehouse_code": "SALE", "target_no": "XY-L"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        page = self.client.get("/wms/outbound")
        self.assertEqual(page.status_code, 200, page.text[:300])
        html = page.text
        self.assertIn("销售出库", html)
        self.assertIn("草稿", html)
        self.assertNotIn("<td>draft</td>", html)
        self.assertNotIn("<td>SALE</td>", html)

    # ------------------------------------------------ ④ 售罄联动
    def test_40_sellout_auto_unlist_and_badge(self):
        item_id, barcode, fish = self._wms_item(qty=2)
        product_id = self._product(fish, stock=2, status="on_sale")
        with SessionLocal() as s:
            item = s.get(WmsItem, item_id)
            order = outbound.create(s, {"type": "SALE", "warehouse_code": "SALE",
                                        "target_no": "XY-SO"}, operator="t")
            outbound.lock(s, order, item, operator="t")
            outbound.pick(s, order, item.barcode, operator="t")
            order = outbound.ship(s, order.id, operator="t", waybill_no="SF001")
        self.assertIn("售罄", "".join(order.hub_sync.get("notes") or []),
                      order.hub_sync)
        with SessionLocal() as s:
            self.assertEqual(str(s.get(Product, product_id).status), "off_sale",
                             "库存归零自动下架")
        page = self.client.get("/products")
        self.assertEqual(page.status_code, 200)
        self.assertIn("售罄", page.text, "商品列表要打售罄标")

    def test_45_no_sellout_when_stock_left(self):
        item_id, barcode, fish = self._wms_item(qty=2)
        product_id = self._product(fish, stock=2, status="on_sale")
        with SessionLocal() as s:
            item = s.get(WmsItem, item_id)
            order = outbound.create(s, {"type": "SALE", "warehouse_code": "SALE",
                                        "target_no": "XY-SO2"}, operator="t")
            outbound.lock(s, order, item, operator="t")
            outbound.pick(s, order, item.barcode, operator="t")
            # 一台带 2 台全走了，但渔枢库存口径 = 台数 —— 这里直接改 qty 让它剩 1：
            # 简化成手工把渔枢库存放到 3，出 2 台还剩 1，不该下架
            with SessionLocal() as s2:
                p = s2.get(Product, product_id)
                p.stock = 3
                s2.commit()
            order = outbound.ship(s, order.id, operator="t", waybill_no="SF002")
        self.assertNotIn("售罄", "".join(order.hub_sync.get("notes") or []),
                         order.hub_sync)
        with SessionLocal() as s:
            self.assertEqual(str(s.get(Product, product_id).status), "on_sale")

    # ------------------------------------------------ ⑤ 检测台人工选档
    def test_50_preview_manual_grade(self):
        item_id, barcode, fish = self._wms_item(qty=1)
        with SessionLocal() as s:
            item = s.get(WmsItem, item_id)
            item_id_num, base_price = int(item.id), float(item.base_price or 0)
        # 先拿引擎建议
        r1 = self.client.post("/wms/api/inspect/preview", json={
            "item_id": item_id_num,
            "results": {"power_on": "pass", "screen": "pass"},
        }).json()
        self.assertTrue(r1.get("ok"), r1)
        self.assertFalse(r1.get("manual_grade"))
        self.assertEqual(r1["effective_grade"], r1["suggest_grade"])
        # 人工指定一个与建议不同的档：生效档 / 价格 / 中文档名全跟过去
        target = "D" if r1["suggest_grade"] != "D" else "S"
        r2 = self.client.post("/wms/api/inspect/preview", json={
            "item_id": item_id_num,
            "results": {"power_on": "pass", "screen": "pass"},
            "grade": target,
        }).json()
        self.assertTrue(r2.get("ok"), r2)
        self.assertTrue(r2.get("manual_grade"), r2)
        self.assertEqual(r2["effective_grade"], target)
        self.assertEqual(r2["price"]["base_price"], base_price)
        self.assertTrue(r2.get("grade_label_text"))
        # 乱档位回落引擎建议
        r3 = self.client.post("/wms/api/inspect/preview", json={
            "item_id": item_id_num, "results": {}, "grade": "XX"}).json()
        self.assertTrue(r3.get("ok"), r3)
        self.assertFalse(r3.get("manual_grade"))

    def test_55_inspect_page_has_manual_badge(self):
        page = self.client.get("/wms/inspect")
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn('id="gradeSrc"', page.text)
        self.assertIn("人工指定", page.text)
        self.assertIn("grade:", page.text, "refresh payload 要带人工档")

    # ------------------------------------------------ ⑥ 复检必填覆盖理由
    def test_60_recheck_needs_reason(self):
        """第 2 轮起 = 复检：不填覆盖理由，后端直接拦。"""
        item_id, barcode, fish = self._wms_item(qty=1)
        ok1 = self.client.post("/wms/api/inspect/submit", json={
            "item_id": item_id, "results": {"power_on": "pass"},
            "final_grade": "B", "override_reason": "首轮先按 B 收",
        }).json()
        self.assertTrue(ok1.get("ok"), ok1)
        bad = self.client.post("/wms/api/inspect/submit", json={
            "item_id": item_id, "results": {"power_on": "pass"},
            "final_grade": "B",
        }).json()
        self.assertFalse(bad.get("ok"), bad)
        self.assertIn("覆盖理由", bad.get("message") or "")

    def test_65_recheck_with_reason_ok(self):
        item_id, barcode, fish = self._wms_item(qty=1)
        self.client.post("/wms/api/inspect/submit", json={
            "item_id": item_id, "results": {"power_on": "pass"},
            "final_grade": "B",
        })
        ok2 = self.client.post("/wms/api/inspect/submit", json={
            "item_id": item_id, "results": {"power_on": "pass"},
            "final_grade": "B", "recheck_reason": "客户退货复检，外观复查",
        }).json()
        self.assertTrue(ok2.get("ok"), ok2)
        with SessionLocal() as s:
            from wms.models import WmsTestRecord

            rec = s.query(WmsTestRecord).filter(
                WmsTestRecord.item_id == item_id).order_by(
                WmsTestRecord.id.desc()).first()
            self.assertTrue(rec.is_recheck)
            self.assertIn("复检覆盖理由", str(rec.note or ""))
            self.assertIn("客户退货复检", str(rec.note or ""))

    def test_68_recheck_reason_serves_as_override(self):
        """复检 + 人工改档只填复检理由：同一个理由两头顶（2026-10-15 收口）。"""
        item_id, barcode, fish = self._wms_item(qty=1)
        self.client.post("/wms/api/inspect/submit", json={
            "item_id": item_id, "results": {"power_on": "pass"},
            "final_grade": "B", "override_reason": "首轮先按 B 收",
        })
        ok2 = self.client.post("/wms/api/inspect/submit", json={
            "item_id": item_id, "results": {"power_on": "pass"},
            "final_grade": "A", "recheck_reason": "复检复查外观远好于首轮",
        }).json()
        self.assertTrue(ok2.get("ok"), ok2)
        with SessionLocal() as s:
            from wms.models import WmsTestRecord

            rec = s.query(WmsTestRecord).filter(
                WmsTestRecord.item_id == item_id).order_by(
                WmsTestRecord.id.desc()).first()
            self.assertEqual(rec.final_grade, "A")
            self.assertEqual(rec.override_reason, "复检复查外观远好于首轮")
            self.assertIn("复检覆盖理由", str(rec.note or ""))

    def test_70_page_has_reason_input(self):
        page = self.client.get("/wms/inspect")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="rcReason"', page.text)
        self.assertIn("覆盖理由", page.text)
        self.assertIn("recheck_reason", page.text)
        # 2026-10-15 收口：理由框只有一个（rcReasonBox，放在拍照留档上面），
        # 右列旧的改档理由框（overrideBox/#override）下线 —— 复检与人工改档共用一个框
        self.assertIn('id="rcReasonBox"', page.text)
        self.assertNotIn('id="overrideBox"', page.text)
        self.assertIn("ENGINE_GRADE", page.text)
        # 理由框必须在「拍照留档」标题前面（用户指定的位置；注意 CSS/注释里也有这四个字，
        # 要用带后缀的标题全文匹配）
        self.assertLess(page.text.find('id="rcReasonBox"'), page.text.find("拍照留档（选填"))


if __name__ == "__main__":
    unittest.main()
