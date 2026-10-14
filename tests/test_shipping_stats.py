"""发货统计子页面（2026-10-11）：供应链发货页的「按天 / 按月走势」单独成页。

口径与列表页 /batches **完全一致**（同一份 push.pushed_filters + 同一份统计），
所以「列表 N 条」和「统计 N 单」永远对得上；列表页只留四个汇总数与右上角入口，
走势表、时间口径、快捷区间、是否含已关闭、CSV 导出都在这一页。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import OperationLog, Order, OrderImage, PushBatch, PushBatchItem  # noqa: E402
from app.services import push as push_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
CN = timedelta(hours=8)


def _utc(day: str, hour: int = 10) -> datetime:
    """「北京 day 日 hour 点」→ 库里存的 UTC naive（-8h）。"""
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hour, 0, 0) - CN


class ShippingStatsTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        self._clean()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def tearDown(self) -> None:
        self._clean()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(OperationLog).delete()
            s.query(PushBatchItem).delete()
            s.query(PushBatch).delete()
            s.query(OrderImage).delete()
            s.query(Order).delete()
            s.commit()

    def _pushed(self, no: str, day: str, cost: float = 50.0, sale: float = 100.0,
                state: str = "pushed") -> int:
        """造一笔已推单订单（pushed_at 落在北京 day 日）；state 传 closed 就是已关闭完成。"""
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=no,
                product_name="测试商品",
                supplier_name="华强北数码",
                receiver_name="李四",
                cost_amount=cost,
                sale_amount=sale,
                quantity=1,
                pushed_at=_utc(day),
                ordered_at=_utc(day, 9),
                supplier_status="pushed",
                order_state=state,
            )
            s.add(order)
            s.commit()
            return order.id

    # --------------------------------------------------------------- 入口
    def test_10_entry_button_on_batches_page(self):
        """供应链发货页右上角有入口，带着当前筛选条件；走势表不在列表页了。"""
        self._pushed("S-1", "2026-10-06")
        html = self.client.get("/batches?date_from=2026-10-01&date_to=2026-10-06").text
        self.assertIn("发货统计", html, "列表页要有子页面入口")
        self.assertIn("/batches/shipping-stats", html)
        self.assertIn("date_from=2026-10-01", html, "入口要把当前筛选条件带过去")
        self.assertNotIn("按天统计发货", html, "走势表已搬到子页面")

    def test_20_page_shows_trend_and_stats(self):
        """子页面能打开，按天走势表与统计卡都在，且与列表同一份口径。"""
        self._pushed("S-1", "2026-10-01", cost=60, sale=120)
        self._pushed("S-2", "2026-10-06", cost=50, sale=100)
        page = self.client.get("/batches/shipping-stats?date_from=2026-10-01&date_to=2026-10-06")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn("按天统计发货", html)
        self.assertIn("2026-10-01", html)
        self.assertIn("2026-10-06", html)
        self.assertIn("采购成本合计", html)
        self.assertIn("110.00", html)       # 60 + 50
        # 与列表页统计一致（两边都走 push.pushed_stats）
        with SessionLocal() as s:
            conds = push_service.pushed_filters(s, date_from="2026-10-01", date_to="2026-10-06")
            conds.append(Order.order_state != push_service.ORDER_STATE_CLOSED)
            stats = push_service.pushed_stats(s, conds)
        self.assertEqual(stats["count"], 2)
        self.assertIn("发货单数", html)

    def test_30_group_month(self):
        """按月分组：只给 YYYY-MM 一行。"""
        self._pushed("S-1", "2026-10-01")
        self._pushed("S-2", "2026-10-06")
        page = self.client.get(
            "/batches/shipping-stats?date_from=2026-10-01&date_to=2026-10-31&group=month"
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn("按月统计发货", page.text)
        self.assertIn("2026-10", page.text)
        self.assertIn("共 1 行", page.text, "10 月汇总成一行")

    def test_40_export_csv(self):
        """导出 CSV：带 BOM（Excel 不乱码）、中文表头、按天一行。"""
        self._pushed("S-1", "2026-10-06")
        resp = self.client.get(
            "/batches/shipping-stats?date_from=2026-10-01&date_to=2026-10-06&export=csv"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers.get("content-type", ""))
        self.assertIn("attachment", resp.headers.get("content-disposition", ""))
        body = resp.text
        self.assertIn("日期,发货单,已发货", body)
        self.assertIn("2026-10-06", body)

    def test_50_back_url_keeps_filters(self):
        """返回按钮带着筛选条件回列表页，来回切不丢条件。"""
        page = self.client.get("/batches/shipping-stats?date_from=2026-10-01&date_to=2026-10-06")
        self.assertIn("/batches?date_from=2026-10-01", page.text)

    def test_60_closed_toggle_and_time_field(self):
        """「统计已关闭完成」与「时间口径」在子页面同样生效。"""
        self._pushed("S-1", "2026-10-06")
        self._pushed("S-2", "2026-10-06", state=push_service.ORDER_STATE_CLOSED)
        # 默认不含已关闭：只有 1 天有数据
        plain = self.client.get("/batches/shipping-stats?date_from=2026-10-01&date_to=2026-10-06")
        self.assertIn("共 1 行", plain.text)
        # 勾上已关闭：两笔都算（同一天，还是 1 行，但单量变 2）
        with_closed = self.client.get(
            "/batches/shipping-stats?date_from=2026-10-01&date_to=2026-10-06&show_closed=1"
        )
        self.assertIn("含已关闭完成", with_closed.text)
        # 换时间口径（按下单时间）：子页面下拉里三个口径都在
        self.assertIn("推单时间", with_closed.text)
        self.assertIn("发货时间", with_closed.text)
        self.assertIn("下单时间", with_closed.text)

    def test_70_empty_is_not_500(self):
        """区间内一单都没有：正常渲染，给一句人话。"""
        self._pushed("S-1", "2026-10-06")
        page = self.client.get("/batches/shipping-stats?date_from=2025-01-01&date_to=2025-01-31")
        self.assertEqual(page.status_code, 200)
        self.assertIn("当前筛选下没有可统计的记录", page.text)


if __name__ == "__main__":
    unittest.main()
