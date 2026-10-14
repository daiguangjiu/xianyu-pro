"""报修工单：C 档一键开单 + 结案转拆机件（2026-10-14）。

以前这两步都堵着：
  · C 档定完级，系统只在提示里写一句「下一步开报修单」，人要自己去 /wms/repairs
    再从一长串下拉里认货（序列号还得回头再去货品页抄）；
  · 官方判不可修要结案，跑去货品页点「转拆机件」却被一句「只有 D 档能转」顶回来，
    人が不知道下一步干嘛。
现在：检测台提交 C 档直接给按钮（货带过去），详情页也有入口；报修详情页一次做完
「判 D 档 + 转残值池 + 关单」。本文件就盯这两条路走不走得通。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from wms import intake, inspection, repairs  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import (  # noqa: E402
    GRADE_C,
    GRADE_D,
    STATUS_PARTS,
    TEST_FAIL,
    TEST_PASS,
    WmsItem,
    WmsPartsItem,
    WmsRepairOrder,
    WmsStockLedger,
    WmsStocktake,
    WmsStocktakeLine,
    WmsTestItem,
    WmsTestRecord,
)

ADMIN = {"username": "admin", "password": "xianyu2026"}

# 统一检测项全过（安全红线 3 项另判）；判 C 用 override 直给，不依赖规则引擎的种子数据
_PHONE_RESULTS = {
    "appear": TEST_PASS, "inner": TEST_PASS, "power": TEST_PASS, "panel": TEST_PASS,
    "touch": TEST_PASS, "charge": TEST_PASS, "camera": TEST_PASS, "wifi": TEST_PASS,
    "function": TEST_PASS, "accessory": TEST_PASS,
}


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsPartsItem).delete(synchronize_session=False)
        s.query(WmsStocktakeLine).delete(synchronize_session=False)
        s.query(WmsStocktake).delete(synchronize_session=False)
        s.query(WmsRepairOrder).delete(synchronize_session=False)
        s.query(WmsTestItem).delete(synchronize_session=False)
        s.query(WmsTestRecord).delete(synchronize_session=False)
        s.query(WmsStockLedger).delete(synchronize_session=False)
        s.query(WmsItem).delete(synchronize_session=False)
        s.commit()


def _c_item():
    """造一台已经判成 C 档的货（有序列号，报修要录它）。"""
    with SessionLocal() as s:
        batch = intake.create_batch(s, {"category": "PH", "qty_expected": 1},
                                    operator="tester")
        intake.register(s, batch.id,
                        [{"brand": "Apple", "model": "iPhone 13", "spec": "128G",
                          "serial_no": "SN-C-001", "purchase_cost": 2000.0,
                          "base_price": 3000.0}],
                        operator="tester")
        item = intake.list_items(s, batch_id=batch.id)[0]
        results = dict(_PHONE_RESULTS)
        results["charge"] = TEST_FAIL
        inspection.submit(s, item, tester="tester", results=results,
                          fail_descs={"charge": "不充电"}, battery_health=80,
                          final_grade=GRADE_C, override_reason="官方才会修",
                          flaw_desc="充电口腐蚀")
        return int(item.id)


class RepairEntryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        wms_init_tables()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text[:200]

    def setUp(self):
        _wipe()

    # ------------------------------------------------------ C 档一键入口
    def test_10_item_page_has_repair_entry(self):
        """C 档货品详情页给「开报修单」按钮，货已经带进开单页（不用再认货）。"""
        item_id = _c_item()
        page = self.client.get(f"/wms/items/{item_id}")
        self.assertEqual(page.status_code, 200, page.text[:200])
        self.assertIn("开报修单", page.text)
        self.assertIn(f"/wms/repairs/new?item_id={item_id}", page.text)

    def test_15_new_page_preselects_item(self):
        """开单页：条码 / 序列号从货品档案带出来，序列号当场就能核。"""
        item_id = _c_item()
        page = self.client.get(f"/wms/repairs/new?item_id={item_id}")
        self.assertEqual(page.status_code, 200, page.text[:200])
        with SessionLocal() as s:
            item = s.get(WmsItem, item_id)
            barcode = item.barcode
        self.assertIn(barcode, page.text)
        self.assertIn('name="serial_no"', page.text)
        self.assertIn('value="SN-C-001"', page.text, "序列号要预填，别让人回去翻")
        self.assertIn(f'value="{item_id}"', page.text)
        self.assertIn("return_to", page.text, "校验不过要能退回这一页")

    def test_20_c1_needs_ticket_no(self):
        """C1 线上没凭证号不让开单，并且把人退回原页面（填的内容还在）。"""
        item_id = _c_item()
        resp = self.client.post(
            "/wms/repairs",
            data={"item_id": item_id, "channel": "C1", "vendor": "京东服务",
                  "ticket_no": "", "serial_no": "SN-C-001", "contact_phone": "",
                  "promise_days": "15", "paid_amount": "0",
                  "fault_desc": "不充电",
                  "return_to": f"/wms/repairs/new?item_id={item_id}"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn(f"/wms/repairs/new?item_id={item_id}", resp.headers["location"])
        with SessionLocal() as s:
            self.assertEqual(s.query(WmsRepairOrder).count(), 0,
                             "凭证号没有，一张单都不该落库")

    def test_25_create_then_item_page_shows_progress(self):
        """开过单之后详情页改成「查看报修单」——同一台不许开第二张。"""
        item_id = _c_item()
        resp = self.client.post(
            "/wms/repairs",
            data={"item_id": item_id, "channel": "C1", "vendor": "京东服务",
                  "ticket_no": "JD-777", "serial_no": "SN-C-001",
                  "promise_days": "15", "paid_amount": "0", "fault_desc": "不充电"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            row = s.query(WmsRepairOrder).one()
            self.assertEqual(row.status, "open")
            self.assertEqual(row.item_id, item_id)
            url = f"/wms/repairs/{row.id}"
        self.assertIn(url, resp.headers["location"], "开完直接落到单据详情")
        detail = self.client.get(url)
        self.assertEqual(detail.status_code, 200, detail.text[:200])
        self.assertIn("结案并转拆机件残值", detail.text, "详情页要有新出口")

        item_page = self.client.get(f"/wms/items/{item_id}")
        self.assertIn("查看报修单", item_page.text)
        self.assertIn(url, item_page.text)
        self.assertNotIn(f"/wms/repairs/new?item_id={item_id}", item_page.text,
                         "有单在走就不该再冒出一个开单入口")

    def test_30_inspect_submit_hint(self):
        """检测台提交 C 档：返回体给落点，不只写一句话让人自己找。"""
        item_id = _c_item()
        res = self.client.post(
            "/wms/api/inspect/submit",
            json={"item_id": item_id, "tester": "tester", "battery_health": 80,
                  "results": dict(_PHONE_RESULTS),
                  "fail_descs": {"charge": "不充电"}, "flaw_desc": "充电口腐蚀",
                  "final_grade": GRADE_C, "override_reason": "复检判 C"},
        )
        self.assertEqual(res.status_code, 200, res.text[:200])
        data = res.json()
        self.assertTrue(data.get("ok"), data)
        self.assertEqual(data.get("final"), GRADE_C)
        hint = data.get("repair")
        self.assertIsNotNone(hint, "C 档必须给下一步落点")
        self.assertFalse(hint["exists"])
        self.assertEqual(hint["url"], f"/wms/repairs/new?item_id={item_id}")

        # 已经在报修中：这里就改成指向那张单，不再让开第二张
        with SessionLocal() as s:
            item = s.get(WmsItem, item_id)
            repairs.create(s, item, {"channel": "C1", "vendor": "京东服务",
                                     "ticket_no": "JD-888", "serial_no": item.serial_no,
                                     "fault_desc": "不充电"}, operator="tester")
            order_id = s.query(WmsRepairOrder).one().id
        res = self.client.post(
            "/wms/api/inspect/submit",
            json={"item_id": item_id, "tester": "tester", "battery_health": 80,
                  "results": dict(_PHONE_RESULTS),
                  "fail_descs": {"charge": "不充电"}, "flaw_desc": "充电口腐蚀",
                  "final_grade": GRADE_C, "override_reason": "复检判 C"},
        )
        hint = res.json().get("repair")
        self.assertTrue(hint["exists"])
        self.assertEqual(hint["url"], f"/wms/repairs/{order_id}")

    def test_35_non_c_item_has_no_entry(self):
        """不是 C 档的货不给报修入口（免得把在售的好货寄出去）。"""
        with SessionLocal() as s:
            batch = intake.create_batch(s, {"category": "PH", "qty_expected": 1},
                                        operator="tester")
            intake.register(s, batch.id, [{"sku_code": "OK-1", "serial_no": "SN-OK"}],
                            operator="tester")
            item = intake.list_items(s, batch_id=batch.id)[0]
            inspection.submit(s, item, tester="tester",
                              results=dict(_PHONE_RESULTS), battery_health=95,
                              final_grade="S", override_reason="完好")
            self.assertEqual(item.grade, "S")
            item_id = int(item.id)
        page = self.client.get(f"/wms/items/{item_id}")
        self.assertNotIn("开报修单", page.text)

    # ------------------------------------------------------ 结案转拆机件
    def test_40_to_parts_closes_order(self):
        """报修收尾另一条出路：一次做完「判 D + 进拆机件仓 + 关单」。"""
        item_id = _c_item()
        with SessionLocal() as s:
            item = s.get(WmsItem, item_id)
            repairs.create(s, item, {"channel": "C1", "vendor": "京东服务",
                                     "ticket_no": "JD-999", "serial_no": item.serial_no,
                                     "fault_desc": "主板进水"}, operator="tester")
            order_id = s.query(WmsRepairOrder).one().id
        # 走一遍真流程：登记送修 → 修回（回仓待复检）
        self.client.post(f"/wms/repairs/{order_id}/actions",
                         data={"action": "send", "waybill_no": "SF123"})
        self.client.post(f"/wms/repairs/{order_id}/actions", data={"action": "return"})

        resp = self.client.post(f"/wms/repairs/{order_id}/actions",
                                data={"action": "parts",
                                      "note": "官方判主板进水不可修"},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            row = s.get(WmsRepairOrder, order_id)
            self.assertEqual(row.status, "closed")
            self.assertIn("转拆机件", str(row.result_note or ""))
            item = s.get(WmsItem, item_id)
            self.assertEqual(item.grade, GRADE_D, "不可修的必须判 D，不能挂着 C 档")
            self.assertEqual(item.status, STATUS_PARTS)
            self.assertTrue(any(str(r.biz_type or "") == "SCRAP"
                                for r in s.query(WmsStockLedger)
                                .filter(WmsStockLedger.item_id == item_id).all()),
                            "要写拆机件流水（否则库存账对不上）")

    def test_45_to_parts_blocked_while_out(self):
        """货还在外面不给拆 —— 手上有货才谈得上拆件。"""
        item_id = _c_item()
        with SessionLocal() as s:
            item = s.get(WmsItem, item_id)
            repairs.create(s, item, {"channel": "C2", "vendor": "城南授权网点",
                                     "contact_phone": "13800000000",
                                     "serial_no": item.serial_no,
                                     "fault_desc": "不开机"}, operator="tester")
            order_id = s.query(WmsRepairOrder).one().id
        with SessionLocal() as s:
            repairs.send(s, order_id, waybill_no="SF456", operator="tester")
            status = s.get(WmsItem, item_id).status
        self.assertEqual(status, "OUT_REPAIR")

        page = self.client.post(f"/wms/repairs/{order_id}/actions",
                                data={"action": "parts", "note": "不可修"})
        self.assertEqual(page.status_code, 200, "被拦时留在详情页并给出原因")
        self.assertIn("货还在外面", page.text)
        with SessionLocal() as s:
            self.assertEqual(s.get(WmsRepairOrder, order_id).status, "sent")
            self.assertNotEqual(s.get(WmsItem, item_id).status, STATUS_PARTS)


if __name__ == "__main__":
    unittest.main()
