"""2026-10-16 三条页面改动的回归：发货管理 / 出库管理（渔集）。

① 发货管理：统计卡（待拣货 / 已发货 / 全部）与筛选区之间留间距（.grid + .card），
   列表接全站统一分页组件 _pager.html，统计数字按筛选全量算（不跟着翻页跳）；
② 出库管理：筛选控件横排一行（filters 类直接挂到 form 上，不再逐行堆叠），
   「新建出库单」拆成独立子页面 /wms/outbound/new，列表页只留入口按钮；
③ 库存台账 / 货品台账的分页统一换成 _pager.html 组件。
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
from wms import intake  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import WmsBatch, WmsDeliveryOrder, WmsItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _wipe_wms() -> None:
    with SessionLocal() as s:
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsBatch).delete(synchronize_session=False)
        s.query(WmsDeliveryOrder).delete(synchronize_session=False)
        s.commit()


def _one_delivery() -> None:
    """造一条最小发货单：让「共 N 条」的分页信息条渲染出来。"""
    with SessionLocal() as s:
        s.add(WmsDeliveryOrder(delivery_no="DV1016TEST01", status="pending"))
        s.commit()


def _one_item() -> None:
    """造一台货：库存台账 / 货品台账有数据，「共 N 条」信息条才渲染。"""
    with SessionLocal() as s:
        batch = intake.create_batch(
            s, {"category": "HA", "source_type": "purchase", "qty_expected": 1,
                "supplier_name": "1016 分页测试"}, operator="tester")
        intake.register(s, batch.id, [
            {"sku_code": "PG-1", "product_name": "分页测试货", "qty": 1},
        ], operator="tester")


class DeliveriesOutboundPagesTest(unittest.TestCase):
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
        _wipe_wms()

    # ------------------------------------------------------ ① 发货管理
    def test_10_deliveries_stats_spacing_and_pager(self):
        _one_delivery()
        page = self.client.get("/wms/deliveries")
        self.assertEqual(page.status_code, 200)
        html = page.text
        # 统计卡三块照旧，数字来自后端（不靠模板对 rows 现算）
        for label in ("待拣货", "已发货", "全部发货单"):
            self.assertIn(label, html)
        self.assertIn("发货单（", html)
        self.assertIn("共 1 条", html, "统一分页组件的信息条（一页也显示）")
        self.assertIn("pager-wrap", html, "发货管理要用统一分页组件")
        # 有筛选值时翻页链接把条件带着走：信息条之上，页码区由组件渲染
        page2 = self.client.get("/wms/deliveries", params={"q": "DV1016", "page": "1"}).text
        self.assertIn("DV1016", page2)
        # 统计卡与筛选卡之间的间距由全局 CSS 给
        css = (Path(__file__).resolve().parent.parent / "app/static/wms_design.css").read_text("utf-8")
        self.assertIn(".grid + .card", css)

    # ------------------------------------------------------ ② 出库管理
    def test_20_outbound_list_filter_inline_and_new_entry(self):
        page = self.client.get("/wms/outbound")
        self.assertEqual(page.status_code, 200)
        html = page.text
        # 筛选横排：filters 类直接挂在 form 上（以前包在 form 外面导致逐行堆叠）
        self.assertIn('<form class="filters padded"', html)
        # 列表页只留「新建出库单」入口，整张建单表单拆到子页面
        self.assertIn('/wms/outbound/new', html)
        self.assertNotIn("name=\"fish_code\"", html, "建单表单不再内嵌在列表页")

    def test_21_wms_items_and_goods_use_unified_pager(self):
        _one_item()
        items = self.client.get("/wms/items").text
        self.assertIn("pager-wrap", items, "库存台账换统一分页组件")
        self.assertIn("共 1 条", items)
        goods = self.client.get("/wms/goods").text
        self.assertIn("pager-wrap", goods, "货品台账换统一分页组件")
        # 手写的窗口页码宏（plink）已从两个页面撤掉
        self.assertNotIn("plink", items)
        self.assertNotIn("plink", goods)

    def test_30_outbound_new_page_has_full_form(self):
        page = self.client.get("/wms/outbound/new")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn("<h2>新建出库单</h2>", html)
        for field in ("fish_code", "barcode", "sku_code", "target_no",
                      "warehouse_code", "express_company", "waybill_no"):
            self.assertIn(f'name="{field}"', html, f"建单表单要有 {field}")
        self.assertIn("创建出库单", html)
        # ① 单据 ② 挂货 ③ 物流与收货 三段分区跟着搬过来
        for sec in ("① 单据", "② 挂货", "③ 物流与收货"):
            self.assertIn(sec, html)


if __name__ == "__main__":
    unittest.main()
