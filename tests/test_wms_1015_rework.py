"""2026-10-15 第二轮五条改动的回归：台账拆分 / 预警 / 库存台账列 / 出库列表 / 报修工单。

① 多条码拆分：一个渔货号只对应一个条码商品 —— 多条的把多余的拆出去各发新渔货号
   （最早建档那台留原号），YS202610120001 用户点名暂不拆；
② 货品台账：去类目列 + 可售 0 预警徽标；
③ 库存台账：去类目 / 残值 / 锁定列，价格三列改渔货价 / 建议销售价 / 市场价；
④ 出库列表：出库商品显示商品名 + 渔货号（条码小字跟上）；
⑤ 报修工单：货品模糊搜索（渔货号 / 条码 / 商品名）+ SN 必填 + 通道固定三选
   + 维修费 / 快递费 + 送出 → 寄回 → 确认再入库。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from wms import intake, repairs  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import WmsBatch, WmsItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsBatch).delete(synchronize_session=False)
        s.commit()


class Rework1015Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text[:200]

    def setUp(self):
        _wipe()

    def _batch(self, qty: int = 5) -> WmsBatch:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "purchase", "qty_expected": qty,
                    "supplier_name": "1015 改版测试"}, operator="tester")
            return s.get(WmsBatch, batch.id)

    def _register(self, batch: WmsBatch, rows: list[dict]) -> list[int]:
        with SessionLocal() as s:
            return [it.id for it in intake.register(s, batch.id, rows, operator="tester")]

    # ------------------------------------------------------ ① 多条码拆分
    def test_10_split_fish_keeps_first_and_issues_new_codes(self):
        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "SP-1", "product_name": "要拆的一款", "fish_code": "YS202610159901", "qty": 1},
            {"sku_code": "SP-2", "product_name": "要拆的一款", "fish_code": "YS202610159901", "qty": 1},
            {"sku_code": "SP-3", "product_name": "要拆的一款", "fish_code": "YS202610159901", "qty": 1},
        ])
        with SessionLocal() as s:
            new_codes = intake.split_fish(s, "YS202610159901", operator="tester")
        self.assertEqual(len(new_codes), 2, "3 条码 → 1 留 + 2 拆")
        self.assertTrue(all(c.startswith("YS") and c != "YS202610159901" for c in new_codes))
        with SessionLocal() as s:
            kept = s.get(WmsItem, ids[0])
            self.assertEqual(kept.fish_code, "YS202610159901", "最早那台留原号（渔枢商家编码不动）")
            moved = [s.get(WmsItem, i).fish_code for i in ids[1:]]
            self.assertEqual(moved, new_codes)
            # 拆完每号一个条码
            self.assertEqual(len(intake.find_items_by_fish(s, "YS202610159901")), 1)

    def test_15_split_single_and_excluded(self):
        batch = self._batch()
        self._register(batch, [
            {"sku_code": "SP-4", "product_name": "单条码", "fish_code": "YS202610159902", "qty": 1},
        ])
        with SessionLocal() as s:
            with self.assertRaises(ValueError) as ctx:
                intake.split_fish(s, "YS202610159902", operator="tester")
            self.assertIn("不用拆", str(ctx.exception))

    def test_20_split_route_and_excluded_fish(self):
        batch = self._batch()
        self._register(batch, [
            {"sku_code": "SP-5", "product_name": "不拆款", "fish_code": "YS202610120001", "qty": 1},
            {"sku_code": "SP-6", "product_name": "不拆款", "fish_code": "YS202610120001", "qty": 1},
        ])
        page = self.client.get("/wms/goods")
        self.assertEqual(page.status_code, 200)
        self.assertIn("YS202610120001", page.text)
        # 点名款：出子行明细但不给拆分按钮
        self.assertIn("sub-row", page.text)
        self.assertNotIn("/wms/goods/YS202610120001/split", page.text)
        # 单条码款不给拆分按钮
        self._register(batch, [
            {"sku_code": "SP-7", "product_name": "另一款", "fish_code": "YS202610159903", "qty": 1},
        ])
        page2 = self.client.get("/wms/goods").text
        self.assertNotIn("/wms/goods/YS202610159903/split", page2)

    def test_25_split_route_end_to_end(self):
        batch = self._batch()
        self._register(batch, [
            {"sku_code": "SP-8", "product_name": "路由拆分款", "fish_code": "YS202610159904", "qty": 1},
            {"sku_code": "SP-9", "product_name": "路由拆分款", "fish_code": "YS202610159904", "qty": 1},
        ])
        resp = self.client.post("/wms/goods/YS202610159904/split", follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:200])
        with SessionLocal() as s:
            self.assertEqual(
                len(intake.find_items_by_fish(s, "YS202610159904")), 1, "拆完原号只剩一台")
        # 点名款走路由也被拦
        self._register(batch, [
            {"sku_code": "SP-10", "product_name": "不拆款", "fish_code": "YS202610120001", "qty": 1},
            {"sku_code": "SP-11", "product_name": "不拆款", "fish_code": "YS202610120001", "qty": 1},
        ])
        resp2 = self.client.post("/wms/goods/YS202610120001/split", follow_redirects=False)
        self.assertEqual(resp2.status_code, 303)

    # ------------------------------------------------------ ②③ 台账列与预警
    def test_30_goods_page_columns_and_alert(self):
        batch = self._batch()
        self._register(batch, [
            {"sku_code": "GA-1", "product_name": "列测试款", "fish_code": "YS202610159905", "qty": 2},
        ])
        page = self.client.get("/wms/goods")
        self.assertEqual(page.status_code, 200)
        html = page.text
        # 类目列下线
        self.assertNotIn("<th>品类</th>", html)
        # 可售 0 → 预警徽标（新登记还没检测，可售必然 0）
        self.assertIn("可售预警", html)
        self.assertIn("title=\"可售台数为 0", html)

    def test_35_items_page_columns(self):
        batch = self._batch()
        self._register(batch, [
            {"sku_code": "IB-1", "product_name": "库存台账列款", "base_price": 100.0,
             "selling_price": 60.0, "suggest_price": 0, "qty": 1},
        ])
        page = self.client.get("/wms/items")
        self.assertEqual(page.status_code, 200)
        html = page.text
        # 新三列在，老三列不在
        self.assertIn("渔货价</th>", html)
        self.assertIn("建议销售价</th>", html)
        self.assertIn("市场价</th>", html)
        self.assertNotIn("<th>成本</th>", html)
        self.assertNotIn("<th>售价</th>", html)
        self.assertNotIn("<th>残值</th>", html)
        self.assertNotIn("<th>锁定</th>", html)
        self.assertNotIn("<th>品类</th>", html)
        self.assertIn("未设", html, "建议售价没设显示「未设」不装样子")

    # ------------------------------------------------------ ④ 出库列表
    def test_40_outbound_list_line_display(self):
        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "OB-1", "product_name": "出库展示款", "fish_code": "YS202610159906", "qty": 1},
        ])
        from wms import outbound
        from wms.models import WmsOutboundItem, WmsOutboundOrder
        with SessionLocal() as s:
            item = s.get(WmsItem, ids[0])
            row = outbound.create(s, {"type": "SALE", "warehouse_code": "SALE",
                                      "receiver_name": "丁玲",
                                      "receiver_phone": "13800000000",
                                      "receiver_addr": "测试地址",
                                      "ref_no": "TEST-REF"}, operator="tester")
            # 挂货要走锁货 + 拣货校验（流水 / 库位），这里只验列表展示 —— 直接落数据
            s.add(WmsOutboundItem(outbound_id=row.id, item_id=item.id,
                                  barcode=item.barcode, title=item.title))
            s.commit()
        page = self.client.get("/wms/outbound")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn("出库商品（商品名 · 渔货号 / 条码）", html)
        self.assertIn("YS202610159906", html, "渔货号要显示在列表里")
        self.assertIn("出库展示款", html, "商品名要显示在列表里")

    # ------------------------------------------------------ ⑤ 报修工单
    def test_50_item_search_api(self):
        batch = self._batch()
        self._register(batch, [
            {"sku_code": "RS-1", "product_name": "模糊搜索款", "fish_code": "YS202610159907", "qty": 1},
        ])
        for kw in ("YS202610159907", "模糊搜索", "RS-1"):
            resp = self.client.get("/wms/api/item/search", params={"q": kw})
            self.assertEqual(resp.status_code, 200)
            items = resp.json()["items"]
            self.assertTrue(items, f"关键词 {kw} 应命中")
            self.assertEqual(items[0]["fish_code"], "YS202610159907")
        self.assertEqual(self.client.get(
            "/wms/api/item/search", params={"q": "绝对不存在的词"}).json()["items"], [])

    def test_55_repair_form_fields(self):
        batch = self._batch()
        self._register(batch, [
            {"sku_code": "RP-0", "product_name": "表单字段款", "qty": 1},
        ])
        with SessionLocal() as s:
            item = s.get(WmsItem, self._register(batch, [
                {"sku_code": "RP-0b", "product_name": "表单字段款", "qty": 1}])[0])
            item.grade = "C"
            s.commit()
        page = self.client.get("/wms/repairs/new")
        self.assertEqual(page.status_code, 200)
        html = page.text
        # 模糊搜索带商品
        self.assertIn("r-item-q", html)
        self.assertIn("/wms/api/item/search", html)
        # SN 序列号必填
        self.assertIn("SN 序列号 / IMEI", html)
        self.assertIn('name="serial_no" value="" required', html)
        # 通道固定三选（老 C1/C2 不再给新单选）
        self.assertIn("品牌官方寄回维修", html)
        self.assertIn("品牌到家维修", html)
        self.assertIn("自行维修", html)
        self.assertNotIn("C1 线上（京东 / 400）</option>", html)
        # 维修费 + 快递费
        self.assertIn('name="repair_fee"', html)
        self.assertIn('name="express_fee"', html)

    def test_60_repair_create_with_fees_and_status_flow(self):
        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "RP-1", "product_name": "报修流转款", "qty": 1},
        ])
        with SessionLocal() as s:
            item = s.get(WmsItem, ids[0])
            item.serial_no = "SN-REPAIR-1"
            item.grade = "C"
            s.commit()
        resp = self.client.post("/wms/repairs", data={
            "item_id": str(ids[0]), "channel": "BH", "vendor": "官方到家",
            "serial_no": "SN-REPAIR-1", "fault_desc": "不开机",
            "contact_name": "张师傅", "contact_phone": "13800000000",
            "promise_days": "10", "repair_fee": "300", "express_fee": "50",
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:200])
        from wms.models import WmsRepairOrder
        with SessionLocal() as s:
            row = s.query(WmsRepairOrder).order_by(WmsRepairOrder.id.desc()).first()
            self.assertEqual(row.channel, "BH")
            self.assertEqual(row.paid_amount, 350.0, "垫付 = 维修费 + 快递费")
            self.assertEqual(row.repair_fee, 300.0)
            self.assertEqual(row.express_fee, 50.0)
            rid = row.id
        # 状态流：送出 → 寄回 → 确认再入库
        self.client.post(f"/wms/repairs/{rid}/actions", data={"action": "send",
                                                              "waybill_no": "SF-OUT"},
                         follow_redirects=False)
        self.client.post(f"/wms/repairs/{rid}/actions", data={"action": "return",
                                                              "waybill_no": "SF-BACK"},
                         follow_redirects=False)
        page = self.client.get(f"/wms/repairs/{rid}")
        self.assertIn("确认再入库", page.text, "寄回后详情页给「确认再入库」按钮")
        with SessionLocal() as s:
            from wms import state
            item = s.get(WmsItem, ids[0])
            self.assertEqual(item.status, state.STATUS_OUT_REPAIR, "寄回在途不搬账")
        resp = self.client.post(f"/wms/repairs/{rid}/actions", data={"action": "restock"},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            item = s.get(WmsItem, ids[0])
            self.assertEqual(item.status, "REWORK", "确认再入库搬回待检 + 强制重检")
            self.assertTrue(item.need_recheck)

    def test_65_repair_detail_fees_and_channel_label(self):
        batch = self._batch()
        ids = self._register(batch, [
            {"sku_code": "RP-2", "product_name": "报修展示款", "qty": 1},
        ])
        with SessionLocal() as s:
            item = s.get(WmsItem, ids[0])
            item.serial_no = "SN-2"
            item.grade = "C"
            s.commit()
            row = repairs.create(s, item, {"channel": "SF", "serial_no": "SN-2",
                                           "fault_desc": "屏碎",
                                           "repair_fee": 120, "express_fee": 30},
                                 operator="tester")
            rid = row.id
        page = self.client.get(f"/wms/repairs/{rid}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("自行维修", page.text)
        self.assertIn("¥120.00 / ¥30.00", page.text)


if __name__ == "__main__":
    unittest.main()
