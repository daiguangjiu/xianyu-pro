"""供应链发货（/batches）：时间维度筛选 + 按时间统计发货 + 成本项统计。

业务口径（2026-10-06 新增）：
  1 列表能按时间筛：**默认推单时间**，也能按发货时间 / 下单时间看，起止日期按**北京时间**算；
  2 同一份筛选条件喂给列表、统计、趋势（push.pushed_filters），所以「列表几条」和
    「统计几单」永远一致（列表有 500 行上限，统计走 SQL 汇总不会被截断）；
  3 成本项四个数：采购成本合计 / 单均成本 / 销售额合计 / 账面毛利（销售额 − 成本 − 运费 − 平台服务费）；
  4 按天（YYYY-MM-DD）或按月（YYYY-MM）分组看走势。

坑：库里存 UTC，「北京 10-06」在库里是 10-05 16:00 起 —— 边界换算见 push.day_bounds，
按天分组必须走 _day_expr（+8），SQLite 与 PostgreSQL 两种方言各自实现。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import OperationLog, Order, OrderImage, PushBatch, PushBatchItem, RawMessage  # noqa: E402
from app.services import push as push_service  # noqa: E402
from app.timeutil import beijing_now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
CN = timedelta(hours=8)


def _utc(day: str, hour: int = 10) -> datetime:
    """「北京 day 日 hour 点」→ 库里存的 UTC naive（-8h）。"""
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hour, 0, 0) - CN


class BatchesTimeFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        self.admin = ensure_default_admin()
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
            s.query(RawMessage).delete()
            s.commit()

    def _pushed_order(self, no: str, day: str, cost: float, sale: float, tracking: str = "",
                      shipped_day: str = "", freight: float = 0.0):
        """造一笔「已推单」订单：pushed_at 落在北京 day 日，单号为空 = 待回单。"""
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=no,
                product_name="测试商品",
                supplier_name="华强北数码",
                receiver_name="李四",
                cost_amount=cost,
                sale_amount=sale,
                shipping_fee=freight,
                quantity=1,
                pushed_at=_utc(day),
                ordered_at=_utc(day, 9),
                supplier_status="pushed",
                tracking_no=tracking,
                shipped_at=_utc(shipped_day) if shipped_day else None,
            )
            s.add(order)
            s.commit()
            return order.id

    # ---------------------------------------------------------------- 边界换算
    def test_10_day_bounds_uses_beijing_day(self):
        """「北京 10-06」在库里是 10-05 16:00 ~ 10-06 16:00（半开区间）。"""
        start, end = push_service.day_bounds("2026-10-06", "2026-10-06")
        self.assertEqual(start, datetime(2026, 10, 5, 16, 0))
        self.assertEqual(end, datetime(2026, 10, 6, 16, 0))

        # 只填起始日：只有下界
        start2, end2 = push_service.day_bounds("2026-10-06", "")
        self.assertEqual(start2, datetime(2026, 10, 5, 16, 0))
        self.assertIsNone(end2)

        # 填歪了（起止颠倒 / 非法格式）不给条件，页面不至于 500
        self.assertEqual(push_service.day_bounds("2026-10-09", "2026-10-06"), (None, None))
        self.assertEqual(push_service.day_bounds("昨天", "明天"), (None, None))

    def test_20_range_presets(self):
        """快捷区间要能翻译成具体日期（本月 = 1 号到今天）。"""
        today = beijing_now().date()
        iso = today.isoformat()
        self.assertEqual(push_service.range_days("today"), (iso, iso))
        self.assertEqual(push_service.range_days("7d"), ((today - timedelta(days=6)).isoformat(), iso))
        self.assertEqual(push_service.range_days("30d"), ((today - timedelta(days=29)).isoformat(), iso))
        self.assertEqual(push_service.range_days("month"), (today.replace(day=1).isoformat(), iso))
        # 上月：2026-10-06 → 2026-09-01 ~ 2026-09-30
        self.assertEqual(push_service.range_days("last_month", datetime(2026, 10, 6).date()), ("2026-09-01", "2026-09-30"))
        self.assertEqual(push_service.range_days(""), ("", ""))

    # ---------------------------------------------------------------- 筛选口径
    def _seed(self):
        """三笔已推单：10-01 已发货（成本 60 / 售价 115）、10-05 待回单（40 / 80）、10-06 待回单（50 / 99）。"""
        self._pushed_order("T-A", "2026-10-01", 60.0, 115.0, tracking="SF123", shipped_day="2026-10-01", freight=5.0)
        self._pushed_order("T-B", "2026-10-05", 40.0, 80.0)
        self._pushed_order("T-C", "2026-10-06", 50.0, 99.0)

    def test_30_filter_by_pushed_day(self):
        """按推单时间筛：只出这一天的单（换时间口径就换一列）。"""
        self._seed()
        with SessionLocal() as s:
            conds = push_service.pushed_filters(
                s, date_from="2026-10-06", date_to="2026-10-06", time_field="pushed"
            )
            rows = push_service.pushed_orders(
                s, date_from="2026-10-06", date_to="2026-10-06", time_field="pushed"
            )
            self.assertEqual([o.order_no for o in rows], ["T-C"])
            self.assertEqual(push_service.pushed_stats(s, conds)["count"], 1)

            # 10-01 ~ 10-05：两笔（按推单时间）
            rows2 = push_service.pushed_orders(
                s, date_from="2026-10-01", date_to="2026-10-05", time_field="pushed"
            )
            self.assertEqual(sorted(o.order_no for o in rows2), ["T-A", "T-B"])

            # 换成发货时间：只有 T-A 有 shipped_at
            rows3 = push_service.pushed_orders(
                s, date_from="2026-10-01", date_to="2026-10-01", time_field="shipped"
            )
            self.assertEqual([o.order_no for o in rows3], ["T-A"])

    def test_40_stats_cost_amounts(self):
        """成本项统计：合计 / 单均 / 销售额 / 毛利，与列表同一批单。"""
        self._seed()
        with SessionLocal() as s:
            conds = push_service.pushed_filters(s)
            stats = push_service.pushed_stats(s, conds)
        self.assertEqual(stats["count"], 3)
        self.assertEqual(stats["shipped"], 1)      # 有单号 = 供应商已发货
        self.assertEqual(stats["pending"], 2)      # 没单号 = 待回单
        self.assertAlmostEqual(stats["cost"], 150.0, places=2)   # 60 + 40 + 50
        self.assertAlmostEqual(stats["sale"], 294.0, places=2)   # 115 + 80 + 99
        self.assertAlmostEqual(stats["avg_cost"], 50.0, places=2)
        self.assertAlmostEqual(stats["freight"], 5.0, places=2)
        # 毛利 = 294 − 150 − 5 − 0（平台服务费按 0 计）
        self.assertAlmostEqual(stats["profit"], 139.0, places=2)
        self.assertAlmostEqual(stats["cost_rate"], 51.0, places=1)

    def test_45_stats_follows_time_filter(self):
        """统计跟着时间筛选走：筛 10-06 就只算这一天的成本。"""
        self._seed()
        with SessionLocal() as s:
            conds = push_service.pushed_filters(s, date_from="2026-10-06", date_to="2026-10-06")
            stats = push_service.pushed_stats(s, conds)
        self.assertEqual(stats["count"], 1)
        self.assertAlmostEqual(stats["cost"], 50.0, places=2)
        self.assertAlmostEqual(stats["avg_cost"], 50.0, places=2)

    # ---------------------------------------------------------------- 按时间统计
    def test_50_trend_by_day(self):
        """按天分组：每天一行的单数与成本，日期是北京那一天。"""
        self._seed()
        with SessionLocal() as s:
            trend = push_service.pushed_trend(s, push_service.pushed_filters(s), group="day")
        by_day = {row["period"]: row for row in trend}
        self.assertEqual(sorted(by_day), ["2026-10-01", "2026-10-05", "2026-10-06"])
        self.assertEqual(by_day["2026-10-01"]["count"], 1)
        self.assertEqual(by_day["2026-10-01"]["shipped"], 1)
        self.assertEqual(by_day["2026-10-01"]["pending"], 0)
        self.assertAlmostEqual(by_day["2026-10-01"]["cost"], 60.0, places=2)
        self.assertAlmostEqual(by_day["2026-10-06"]["cost"], 50.0, places=2)

    def test_55_trend_by_month_and_shipped_field(self):
        """按月分组只给 YYYY-MM；换时间口径后按发货时间分组。"""
        self._seed()
        with SessionLocal() as s:
            months = push_service.pushed_trend(s, push_service.pushed_filters(s), group="month")
            shipped = push_service.pushed_trend(
                s, push_service.pushed_filters(s), time_field="shipped", group="day"
            )
        self.assertEqual([row["period"] for row in months], ["2026-10"])
        self.assertEqual(months[0]["count"], 3)
        # 只有 T-A 有发货时间 → 按发货时间分组只有 10-01 一行
        self.assertEqual([row["period"] for row in shipped], ["2026-10-01"])
        self.assertEqual(shipped[0]["count"], 1)

    # ---------------------------------------------------------------- 页面
    def test_60_page_shows_filter_and_stats(self):
        """列表页要有时间筛选框与成本统计数字；按天走势表搬到「发货统计」子页面了。"""
        self._seed()
        page = self.client.get("/batches?date_from=2026-10-01&date_to=2026-10-06")
        self.assertEqual(page.status_code, 200)
        html = page.text
        # 三个时间控件：口径下拉 + 起止日期
        self.assertIn('name="date_field"', html)
        self.assertIn('name="date_from"', html)
        self.assertIn('name="date_to"', html)
        # 统计卡（成本项是重点）
        self.assertIn("采购成本合计", html)
        self.assertIn("150.00", html)       # 成本合计
        self.assertIn("销售额合计", html)
        self.assertIn("294.00", html)
        # 走势表已单独成页：列表页只留入口（带着当前筛选条件）
        self.assertNotIn("按天统计发货", html, "走势表搬到 /batches/shipping-stats 了")
        self.assertIn("/batches/shipping-stats", html)

        stats_page = self.client.get("/batches/shipping-stats?date_from=2026-10-01&date_to=2026-10-06")
        self.assertEqual(stats_page.status_code, 200)
        # 走势表：按天，三行日期
        self.assertIn("2026-10-01", stats_page.text)
        self.assertIn("2026-10-06", stats_page.text)
        self.assertIn("按天统计发货", stats_page.text)

    def test_65_range_shortcut_fills_dates(self):
        """?range=7d 要把起止日期填回日期框（快捷区间不是另一套隐藏口径）。"""
        self._seed()
        today = beijing_now().date().isoformat()
        page = self.client.get("/batches?range=7d")
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="date_from"', page.text)
        self.assertIn(f'value="{today}"', page.text)   # 结束日 = 今天

        # 「本月」：起始日是该月 1 号
        month_page = self.client.get("/batches?range=month")
        self.assertIn(f'value="{today[:8]}01"', month_page.text)

    def test_70_page_time_field_switch(self):
        """按发货时间筛：页面上只剩那笔已发货的单。"""
        self._seed()
        page = self.client.get(
            "/batches?date_field=shipped&date_from=2026-10-01&date_to=2026-10-01"
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn("T-A", page.text)
        self.assertNotIn("T-B", page.text)
        self.assertNotIn("T-C", page.text)

    def test_75_group_switch_keeps_filters(self):
        """「按天 / 按月」切换链接要带着当前筛选条件（group 换成 month，日期不丢）。"""
        self._seed()
        page = self.client.get(
            "/batches/shipping-stats?date_from=2026-10-01&date_to=2026-10-06&group=day"
        )
        self.assertEqual(page.status_code, 200)
        html = page.text
        idx = html.find("按月")
        self.assertGreater(idx, 0)
        # 找到「按月」按钮前面的 href（模板里按钮紧跟在拆分链接后）
        href = ""
        for chunk in html[max(0, idx - 400):idx].split('href="')[1:]:
            href = chunk.split('"')[0]
        self.assertIn("group=month", href)
        self.assertIn("date_from=2026-10-01", href)
        self.assertIn("date_to=2026-10-06", href)

        month_page = self.client.get(
            "/batches/shipping-stats?date_from=2026-10-01&date_to=2026-10-06&group=month"
        )
        self.assertEqual(month_page.status_code, 200)
        self.assertIn("按月统计发货", month_page.text)
        self.assertIn("2026-10", month_page.text)

    def test_80_no_data_is_not_500(self):
        """时间区间内一单都没有：页面正常渲染，不给报错。"""
        self._seed()
        page = self.client.get("/batches/shipping-stats?date_from=2025-01-01&date_to=2025-01-31")
        self.assertEqual(page.status_code, 200)
        self.assertIn("当前筛选下没有可统计的记录", page.text)

    def test_90_postgres_dialect_compiles(self):
        """按天分组的 SQL 在 PostgreSQL 上也得编译得过（本地跑的是 SQLite 分支）。

        线上吃过一次亏：SQLite 的 date(col,'+8 hours') 在 PG 上是 UndefinedFunction，
        页面直接 500。所以这里按 PG 方言编译一遍，确认走的是 to_char + INTERVAL。
        """
        from unittest import mock

        from sqlalchemy import select as sa_select
        from sqlalchemy.dialects import postgresql

        with mock.patch("app.services.push.dialect_name", lambda: "postgresql"):
            day = push_service._day_expr(Order.pushed_at)
            sql = str(sa_select(day).compile(dialect=postgresql.dialect()))
        self.assertIn("to_char", sql)
        self.assertIn("INTERVAL", sql)
        # SQLite 专属的两参数 date() 不能出现在 PG 的语句里
        self.assertNotIn("date(", sql.lower())

    def test_85_history_view_unaffected(self):
        """历史批次视图照旧（不依赖新加的时间参数）。"""
        page = self.client.get("/batches?view=history")
        self.assertEqual(page.status_code, 200)
        self.assertIn("历史批次", page.text)


if __name__ == "__main__":
    unittest.main()
