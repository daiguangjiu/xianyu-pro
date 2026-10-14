"""首页指标与订单列表调整（2026-09-25 版本）。

业务口径：
  1 首页第 4 个数字由「待解析消息」换成「待供应商回单」：
    已推给供应商、但还没回填物流单号的订单数（点击进入「已推单」列表，跟进催单）；
  2 订单列表把「详情 / 推单」两个按钮从最后一列挪到最前面一列（订单信息之前）；
  3 首页顶部新增「已发货」指标：订单状态走到「已发货」的单数，点击进入已发货列表。
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
from app.models import (  # noqa: E402
    ORDER_STATE_CANCELED,
    ORDER_STATE_NEW,
    ORDER_STATE_REFUNDED,
    ORDER_STATE_SHIPPED,
    Order,
    RawMessage,
)
from app.services import stats as stats_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class DashboardTrackingTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def _order(self, order_no: str, **kw) -> int:
        with SessionLocal() as s:
            order = Order(platform="xianyu", order_no=order_no, msg_time=now(), **kw)
            s.add(order)
            s.commit()
            return order.id

    # ---------------------------------------------------------------- 待供应商回单
    def test_pending_tracking_counts_pushed_without_tracking_no(self):
        """已推单且没有物流单号 -> 待供应商回单 +1；回填单号后归零。"""
        order_id = self._order(
            "DASH-1",
            supplier_status="pushed",
            pushed_at=now(),
            tracking_no="",
        )
        with SessionLocal() as s:
            self.assertEqual(stats_service.summary(s)["pending_tracking"], 1)

        with SessionLocal() as s:
            s.get(Order, order_id).tracking_no = "SF123456"
            s.commit()
        with SessionLocal() as s:
            self.assertEqual(stats_service.summary(s)["pending_tracking"], 0, "回单后不再等待")

    def test_pending_tracking_ignores_unpushed_refund_and_recalled(self):
        """还没推单 / 退单 / 撤回的订单都不算「待供应商回单」。"""
        self._order("DASH-2", supplier_status="pending", tracking_no="")  # 还没推给供应商
        self._order(
            "DASH-3",
            supplier_status="pushed",
            pushed_at=now(),
            tracking_no="",
            refund_status="此单退",
            order_status="refund",
        )
        self._order(
            "DASH-4",
            supplier_status="pushed",
            pushed_at=now(),
            tracking_no="",
            is_recalled=True,
            recalled_at=now(),
        )
        with SessionLocal() as s:
            self.assertEqual(stats_service.summary(s)["pending_tracking"], 0)

    def test_dashboard_shows_pending_supplier_tracking(self):
        """首页第 4 个数字：显示「待供应商回单」，不再显示「待解析消息」。"""
        self._order("DASH-5", supplier_status="pushed", pushed_at=now(), tracking_no="")
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("待供应商回单", page.text)
        self.assertIn("/orders?supplier_status=pushed", page.text, "点进去看已推单列表")
        self.assertNotIn("待解析消息", page.text, "首页不再展示「待解析消息」指标")

        hero = page.text.split('class="hero-stats"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(hero.count("hero-stat"), 5, "首页顶部现在是 5 个数字")

    # ---------------------------------------------------------------- 已发货
    def test_shipped_counts_shipped_orders(self):
        """首页「已发货」：只数订单状态 = 已发货的单。"""
        self._order("DASH-6", order_state=ORDER_STATE_SHIPPED)
        self._order("DASH-7", order_state=ORDER_STATE_NEW)
        with SessionLocal() as s:
            self.assertEqual(stats_service.summary(s)["shipped"], 1)

    def test_shipped_ignores_cancelled_refunded_and_recalled(self):
        """撤单 / 退单 / 已撤回的单不算「已发货」。"""
        self._order("DASH-8", order_state=ORDER_STATE_CANCELED)
        self._order("DASH-9", order_state=ORDER_STATE_REFUNDED)
        self._order(
            "DASH-10",
            order_state=ORDER_STATE_SHIPPED,
            is_recalled=True,
            recalled_at=now(),
        )
        with SessionLocal() as s:
            self.assertEqual(stats_service.summary(s)["shipped"], 0)

    def test_dashboard_shows_shipped_metric(self):
        """首页顶部有「已发货」入口，点进去就是已发货列表。"""
        self._order("DASH-11", order_state=ORDER_STATE_SHIPPED)
        page = self.client.get("/")
        self.assertIn("/orders?state=shipped", page.text, "点进去看已发货列表")
        hero = page.text.split('class="hero-stats"', 1)[1].split("</section>", 1)[0]
        self.assertIn("已发货", hero)
        self.assertEqual(hero.count("hero-stat"), 5)

        listing = self.client.get("/orders?state=shipped")
        self.assertEqual(listing.status_code, 200)
        self.assertIn("DASH-11", listing.text)

    # ---------------------------------------------------------------- 今日口径
    def test_today_metrics_are_today_only(self):
        """首页卡片：累计是累计、今日只算今天归档的单。"""
        from datetime import timedelta

        yesterday = now() - timedelta(days=1)
        self._order("DASH-T1", sale_amount=100.0, cost_amount=60.0, shipping_fee=5.0)
        self._order(
            "DASH-T2",
            sale_amount=900.0,
            cost_amount=500.0,
            shipping_fee=0.0,
            created_at=yesterday,
        )
        with SessionLocal() as s:
            data = stats_service.summary(s)
        self.assertEqual(data["total_orders"], 2, "累计口径不变")
        self.assertEqual(data["total_sale"], 1000.0)
        self.assertEqual(data["today_orders"], 1)
        self.assertEqual(data["today_sale"], 100.0)
        self.assertEqual(data["today_cost"], 60.0)
        self.assertEqual(data["today_fee"], 5.0)
        self.assertEqual(data["today_profit"], 35.0, "今日毛利 = 100 − 60 − 5")

    def test_dashboard_shows_today_values(self):
        """首页 4 张卡片都带「今日」数字（累计 + 今日两套口径并排看）。"""
        self._order("DASH-T3", sale_amount=100.0, cost_amount=60.0, shipping_fee=5.0)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("今日新增 1 单", page.text)
        self.assertIn("今日 ¥100.00", page.text, "今日销售额")
        self.assertIn("今日 ¥60.00", page.text, "今日采购成本")
        self.assertIn("今日 ¥35.00", page.text, "今日毛利")
        self.assertIn("hi-today", page.text)

    # ------------------------------------------- 双口径指标（累计/今日新增、已发货/待发货）
    def _hero(self, text: str) -> str:
        return text.split('class="hero-stats"', 1)[1].split("</section>", 1)[0]

    def test_hero_shows_cumulative_and_today(self):
        """「累计 / 今日新增」并排：累计是全部单量，今日只算今天归档的。"""
        from datetime import timedelta

        now_ts = now()
        self._order("DASH-C1")  # 今天：累计 3 / 今日 2
        self._order("DASH-C2")
        self._order("DASH-C3", created_at=now_ts - timedelta(days=1))

        page = self.client.get("/")
        hero = self._hero(page.text)
        self.assertIn("累计 / 今日新增", hero)
        self.assertIn('data-count="3"', hero, "累计口径")
        self.assertIn('data-count="2"', hero, "今日口径")
        self.assertIn('class="n n-sub"', hero, "副值小一号显示")
        self.assertIn("/orders?created=today", page.text, "点进去看今日新增列表")

    def test_pending_ship_counts_unshipped(self):
        """待发货 = 已推单（已推给供应商、等发货）；新建（还没推单）/ 已发货 / 撤单 / 退单 / 已撤回都不算。"""
        self._order("DASH-P1", order_state=ORDER_STATE_NEW)
        self._order("DASH-P2", order_state="pushed", supplier_status="pushed", pushed_at=now())
        self._order("DASH-P3", order_state=ORDER_STATE_SHIPPED, tracking_no="SF1")
        self._order("DASH-P4", order_state=ORDER_STATE_CANCELED)
        self._order("DASH-P5", order_state=ORDER_STATE_REFUNDED)
        self._order(
            "DASH-P6",
            order_state=ORDER_STATE_NEW,
            is_recalled=True,
            recalled_at=now(),
        )
        with SessionLocal() as s:
            data = stats_service.summary(s)
        self.assertEqual(data["pending_ship"], 1, "只有已推单算待发货：还没推单的单子谈不上发货")
        self.assertEqual(data["shipped"], 1)

    def test_hero_shows_shipped_and_pending_ship(self):
        """「已发货 / 待发货」并排，且待发货能一键筛出来。"""
        self._order("DASH-S1", order_state=ORDER_STATE_SHIPPED, tracking_no="SF2")
        self._order("DASH-S2", order_state=ORDER_STATE_NEW)
        self._order("DASH-S3", order_state="pushed", supplier_status="pushed", pushed_at=now())

        page = self.client.get("/")
        hero = self._hero(page.text)
        self.assertIn("已发货 / 待发货", hero)
        self.assertIn('data-count="1"', hero, "已发货 1 单、待发货 1 单（新建未推单不计入）")
        with SessionLocal() as s:
            self.assertEqual(stats_service.summary(s)["pending_ship"], 1, "待发货只算已推单")

        listing = self.client.get("/orders?state=new,pushed")
        self.assertEqual(listing.status_code, 200, "订单状态支持逗号分隔的多值筛选")
        self.assertIn("DASH-S2", listing.text)
        self.assertIn("DASH-S3", listing.text)
        self.assertNotIn("DASH-S1", listing.text, "已发货的不在待发货列表里")

    def test_orders_filter_has_pending_ship_option(self):
        """订单页筛选里有「待发货（新建 + 已推单）」选项，选中态能正确回显。"""
        self._order("DASH-F1", order_state=ORDER_STATE_NEW)
        page = self.client.get("/orders?state=new,pushed")
        self.assertIn('value="new,pushed"', page.text)
        self.assertIn("待发货（新建 + 已推单）", page.text)
        self.assertIn("待发货（新建 + 已推单）（1）", page.text, "选项里带数量")
        self.assertIn('value="new,pushed" selected', page.text, "筛选回显")


if __name__ == "__main__":
    unittest.main()
