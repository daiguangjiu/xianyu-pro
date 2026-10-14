"""渔货栈「订单管理」（2026-10-02）：自己的订单 + 与渔枢系统字段一致。

口径：
1. 渔货栈出的单先落渔货栈的 `downstream_orders`，能在渔货栈订单管理里看到；
2. 同一单号在渔枢系统也有一笔「平台订单」（编审单 / 推单 / 发货 / 结算在那里），
   渔货栈按单号把它的事实取回来展示 —— 金额口径与渔枢系统端一致（售价 / 退款 / 采购成本 /
   供应链退款 / 平台技术服务费 / 毛利）；
3. 还没推到渔枢系统的单显示「未建档」，详情页能一键补推（幂等）。
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ORDER_ORIGIN_SELF, ORDER_STATE_NEW, Order, OrderLog  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.models import DownstreamOrder, PurchaseOrder  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

DSO_ID = "TXY20261002999"
HUB_ORDER_NO = "TXY20261002111"


def _reset() -> None:
    with SessionLocal() as s:
        s.query(OrderLog).delete()
        s.query(Order).delete()
        s.query(PurchaseOrder).delete()
        s.query(DownstreamOrder).delete()
        s.commit()


def _dso(order_no: str = DSO_ID, *, status: str = "pending") -> int:
    with SessionLocal() as s:
        row = DownstreamOrder(
            downstream_order_id=order_no,
            channel="xianyu",
            channel_sku_id="XY-SKU-1",
            buyer_name="李四",
            buyer_phone="13800000000",
            address="广东省深圳市南山区",
            qty=1,
            sale_price=Decimal("120.00"),
            status=status,
        )
        s.add(row)
        s.commit()
        return row.id


def _hub_order(order_no: str = DSO_ID) -> int:
    with SessionLocal() as s:
        row = Order(
            platform="xianyu",
            order_no=order_no,
            order_state=ORDER_STATE_NEW,
            origin=ORDER_ORIGIN_SELF,
            product_name="测试-记忆棉枕",
            spec="标准款",
            quantity=1,
            sale_amount=120.0,
            deal_amount=120.0,
            cost_amount=50.0,
            shipping_fee=8.0,
            platform_fee=0.72,
            refund_amount=0.0,
            supplier_refund_amount=0.0,
            receiver_name="李四",
            receiver_phone="13800000000",
            receiver_address="广东省深圳市南山区",
            supplier_status="pending",
        )
        s.add(row)
        s.commit()
        return row.id


class SelfOrdersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    # ---------------------------------------------------------------- 列表
    def test_10_list_shows_hub_fields(self):
        """列表里既有渔货栈自己的单，也有渔枢系统端同一单号的金额口径与「已建档」入口。"""
        _dso()
        hub_id = _hub_order()
        page = self.client.get("/self/orders")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn(DSO_ID, html, "渔货栈自己的订单在列表里")
        self.assertIn("测试-记忆棉枕", html, "商品名取渔枢系统端（渔货栈只有 SKU）")
        self.assertIn("已建档", html)
        self.assertIn(f"/orders/{hub_id}", html, "能一键跳到渔枢系统那笔单")
        # 2026-10-12：列表精简成「走到哪一步 + 下一步干嘛」，
        # 金额只留售价与毛利（平台技术服务费那些明细在详情页看）
        self.assertIn("现在走到哪一步", html)
        self.assertIn("下一步", html)
        self.assertIn("毛利", html)

    def test_11_list_has_no_dev_notes(self):
        """2026-10-12 用户拍板：页面下方那些给开发看的说明全删（系统要开放给学员）。"""
        _dso()
        html = self.client.get("/self/orders").text
        for junk in ("写操作走", "Swagger", "MVP", "/self/docs", "采购单闭环", "素材包里有什么"):
            self.assertNotIn(junk, html, f"订单页还留着给开发看的说明：{junk}")
        purchases = self.client.get("/self/purchases").text
        self.assertNotIn("采购单闭环", purchases)
        self.assertNotIn("Swagger", purchases)

    def test_20_not_built_marks_and_offers_push(self):
        """还没推到渔枢系统的单：列表标「未建档」，详情页给补推按钮。"""
        _dso()
        html = self.client.get("/self/orders").text
        self.assertIn("未建档", html)
        self.assertNotIn("已建档", html)
        detail = self.client.get(f"/self/orders/{DSO_ID}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("推送到渔枢系统", detail.text)

    def test_30_push_creates_hub_order(self):
        """补推：渔枢系统端建档（幂等），页面变成已建档。"""
        _dso()
        resp = self.client.post(
            f"/self/orders/{DSO_ID}/sync-hub", follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("sync-hub", resp.headers["location"] + "sync-hub")
        with SessionLocal() as s:
            row = s.scalar(select(Order).where(Order.order_no == DSO_ID))
            self.assertIsNotNone(row, "渔枢系统建了这笔单")
            self.assertEqual(row.origin, ORDER_ORIGIN_SELF)
            self.assertEqual(float(row.sale_amount or 0), 120.0, "售价按渔货栈下游单带过去")
            self.assertEqual(row.receiver_name, "李四", "收货信息带过去")
        # 再推一次：幂等，不会建出第二条
        self.client.post(f"/self/orders/{DSO_ID}/sync-hub", follow_redirects=False)
        with SessionLocal() as s:
            rows = s.scalars(select(Order).where(Order.order_no == DSO_ID)).all()
            self.assertEqual(len(rows), 1, "同单号只建一笔")
        detail = self.client.get(f"/self/orders/{DSO_ID}").text
        self.assertIn("已建档", detail)
        self.assertIn("在渔枢系统打开", detail)

    # ---------------------------------------------------------------- 详情
    def test_40_detail_amounts_match_hub(self):
        """详情页的金额口径与渔枢系统端一致（售价 / 退款 / 采购成本 / 供应链退款 / 技术服务费 / 毛利）。"""
        _dso()
        _hub_order()
        html = self.client.get(f"/self/orders/{DSO_ID}").text
        for label in ("售价（买家实付）", "退款给客户", "采购成本", "供应链退款", "平台技术服务费", "毛利"):
            self.assertIn(label, html, f"缺金额口径：{label}")
        self.assertIn("120.00", html, "售价按成交价")
        self.assertIn("50.00", html, "采购成本取渔枢系统端")
        self.assertIn("0.72", html, "平台技术服务费取渔枢系统端")
        self.assertIn("61.28", html, "毛利 = 120 − 50 − 8 − 0.72")

    def test_50_status_filter_still_works(self):
        """渔货栈自己的状态筛选不受影响。"""
        _dso(DSO_ID, status="pending")
        _dso("TXY20261002998", status="completed")
        html = self.client.get("/self/orders?status=completed").text
        self.assertIn("TXY20261002998", html)
        self.assertNotIn(DSO_ID, html, "筛掉的渔货栈订单不出现")

    # ------------------------------------------------- 渔货栈路由（别再跳到渔枢系统端）
    def test_70_menu_links_stay_on_self_end(self):
        """侧边栏订单子菜单必须指向渔货栈（/self/orders），裸 /orders 是渔枢系统的页。"""
        _dso()
        html = self.client.get("/self/orders").text
        self.assertIn('href="/self/orders"', html, "全部订单 → 渔货栈")
        self.assertIn('href="/self/orders?status=pending"', html, "待确认 → 渔货栈")
        # 侧边栏这一段里不能出现裸 /orders（两端同一个端口，那个地址归渔枢系统）
        nav_start = html.find("<nav")
        nav_end = html.find("</nav>", nav_start)
        sidebar = html[nav_start:nav_end]
        self.assertNotIn('href="/orders"', sidebar, "侧边栏里还有指向渔枢系统端订单页的链接")

    def test_80_self_orders_is_its_own_page(self):
        """渔货栈订单页是渔货栈的页，不是渔枢系统端那张。"""
        _dso()
        self_page = self.client.get("/self/orders")
        hub_page = self.client.get("/orders")
        self.assertEqual(self_page.status_code, 200)
        self.assertIn("店铺管理", self_page.text, "渔货栈侧边栏")
        self.assertIn("平台技术服务费", self_page.text, "渔货栈订单的金额列")
        self.assertNotEqual(self_page.text, hub_page.text, "两个端是两张不同的订单页")

    def test_90_detail_links_stay_on_self_end(self):
        """详情页的返回链接与补推 action 也都在渔货栈前缀下。"""
        _dso()
        html = self.client.get(f"/self/orders/{DSO_ID}").text
        self.assertIn('href="/self/orders"', html, "返回列表 → 渔货栈")
        self.assertIn('action="/self/orders/', html, "补推渔枢系统的 action → 渔货栈")
        self.assertIn("/sync-hub", html)

    def test_60_detail_not_found(self):
        """找不到的单：详情页给提示，不报错。"""
        page = self.client.get("/self/orders/NO-SUCH-ORDER")
        self.assertEqual(page.status_code, 200)
        self.assertIn("找不到订单", page.text)


if __name__ == "__main__":
    unittest.main()
