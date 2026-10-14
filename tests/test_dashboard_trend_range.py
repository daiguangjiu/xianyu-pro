"""首页「销售额趋势」按时间段 / 按月查看（2026-10-10）。

口径：
① 区间可选：``?start=&end=``（按**北京自然日**），也能一键切本月 / 上月 / 近 30 天；
   认不出的日期回落近 30 天，起止写反自动对调，跨度超过 366 天从末尾掐；
② 卡片头部给**区间累计**与**日均**（日均 = 区间累计 ÷ 区间天数，含没有订单的那天）；
③ 每根柱子的悬停提示多一项**当天件单价**（当天销售额 ÷ 当天件数，件数按订单 quantity 累加）。
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order  # noqa: E402
from app.services import stats as stats_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
KEY = "趋势区间用例"  # 唯一前缀：把用例数据与库里其它订单隔开
DAY_A = "2026-01-05"
DAY_B = "2026-01-06"


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(Order).filter(Order.product_name.like(f"{KEY}%")).delete(synchronize_session=False)
        s.commit()


def _order(day: str, sale: float, qty: int, seq: int, hour: int = 3) -> None:
    """按**北京**某日 hour 点下单（库里存 UTC —— hour=3 时 UTC 还停在前一天，
    这样才验证得了「按北京自然日切」，而不是按 UTC 天切）。"""
    utc = datetime.strptime(f"{day} {hour:02d}:00", "%Y-%m-%d %H:%M") - timedelta(hours=8)
    with SessionLocal() as s:
        s.add(
            Order(
                platform="xianyu",
                order_no=f"{KEY}-{day}-{seq}",
                temp_order_no=f"{KEY}-{day}-{seq}",
                product_name=f"{KEY} 商品{seq}",
                sale_amount=sale,
                cost_amount=sale / 2,
                quantity=qty,
                ordered_at=utc,
                msg_time=utc,
                needs_review=False,
            )
        )
        s.commit()


class TrendRangeLogicTest(unittest.TestCase):
    """区间解析与按天汇总（纯函数，不开页面）。"""

    def test_01_default_is_last_30_days(self):
        today = stats_service.beijing_now().date()
        start, end = stats_service.resolve_range()
        self.assertEqual(end, today)
        self.assertEqual(start, today - timedelta(days=29))

    def test_02_explicit_dates_win(self):
        start, end = stats_service.resolve_range(DAY_A, DAY_B)
        self.assertEqual((start.isoformat(), end.isoformat()), (DAY_A, DAY_B))

    def test_03_bad_or_reversed_dates_fall_back(self):
        today = stats_service.beijing_now().date()
        # 脏参数：回落近 30 天（end 也脏就用今天）
        start, end = stats_service.resolve_range("乱填", "")
        self.assertEqual((start, end), (today - timedelta(days=29), today))
        # 写反了自动对调
        start, end = stats_service.resolve_range(DAY_B, DAY_A)
        self.assertEqual((start.isoformat(), end.isoformat()), (DAY_A, DAY_B))

    def test_04_range_is_capped(self):
        """跨度上限 366 天：选一整年也只从末尾往前掐 366 天。"""
        start, end = stats_service.resolve_range("2000-01-01", DAY_B)
        self.assertEqual(end.isoformat(), DAY_B)
        self.assertEqual((end - start).days, stats_service.MAX_TREND_DAYS - 1)
        self.assertEqual(start, date(2026, 1, 6) - timedelta(days=stats_service.MAX_TREND_DAYS - 1))

    def test_05_daily_rows_carry_qty_and_unit_price(self):
        init_db()  # 纯函数用例也要先建表（单独跑这个类时库里可能啥都没有）
        _wipe()
        self.addCleanup(_wipe)
        _order(DAY_A, 100.0, 2, 1)
        _order(DAY_A, 50.0, 1, 2)
        _order(DAY_B, 100.0, 1, 3)
        with SessionLocal() as s:
            # 多取一天（1-04）验证：没有订单的那天也给一行 0，柱子才连得起来
            rows = stats_service.daily_trend(s, start="2026-01-04", end=DAY_B)
        self.assertEqual([r["date"] for r in rows], ["2026-01-04", DAY_A, DAY_B])
        empty, a, b = rows
        self.assertEqual((empty["count"], empty["qty"], empty["sale"], empty["unit_price"]), (0, 0, 0.0, 0.0))
        self.assertEqual((a["count"], a["qty"], a["sale"]), (2, 3, 150.0))
        self.assertAlmostEqual(a["unit_price"], 50.0, places=2)
        self.assertEqual((b["count"], b["qty"], b["sale"]), (1, 1, 100.0))
        self.assertAlmostEqual(b["unit_price"], 100.0, places=2)


class TrendRangePageTest(unittest.TestCase):
    """首页趋势图：区间筛选 + 累计 / 日均 + 柱子悬停的件单价。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        _wipe()
        _order(DAY_A, 100.0, 2, 1)
        _order(DAY_A, 50.0, 1, 2)
        _order(DAY_B, 100.0, 1, 3)
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def tearDown(self) -> None:
        _wipe()

    def test_10_range_shows_total_and_daily_average(self):
        page = self.client.get(f"/?start={DAY_A}&end={DAY_B}")
        self.assertEqual(page.status_code, 200)
        text = page.text
        self.assertIn("销售额趋势", text)
        # 区间累计 100+50+100=250，两天 → 日均 125
        self.assertIn("¥250.00", text, "区间累计")
        self.assertIn("¥125.00", text, "日均 = 累计 ÷ 区间天数")
        self.assertIn(">区间累计<", text)
        self.assertIn(">日均<", text)
        # 起止日期回填到筛选框，方便直接改
        self.assertIn(f'name="start" value="{DAY_A}"', text)
        self.assertIn(f'name="end" value="{DAY_B}"', text)
        # 快捷区间（本月 / 上月 / 近 30 天）都在
        for preset in ("this_month", "last_month", "d30"):
            self.assertIn(f'data-trend-preset="{preset}"', text)
        self.assertIn("getElementById('trendRange')", text, "快捷区间由页面脚本填日期后提交")

    def test_20_bar_tooltip_has_unit_price(self):
        page = self.client.get(f"/?start={DAY_A}&end={DAY_B}")
        # 当天：2 单 · 3 件 · ¥150.00 · 件单价 ¥50.00
        self.assertIn(f'title="{DAY_A} · 2 单 · 3 件 · ¥150.00 · 件单价 ¥50.00"', page.text)
        self.assertIn(f'title="{DAY_B} · 1 单 · 1 件 · ¥100.00 · 件单价 ¥100.00"', page.text)
        # 图例：区间天数、件数与区间件单价
        self.assertIn(f"{DAY_A} → {DAY_B}（2 天）", page.text)
        self.assertIn("共 4 件 · 件单价 ¥62.50", page.text)

    def test_30_bad_params_fall_back_to_30_days(self):
        page = self.client.get("/?start=乱填&end=")
        self.assertEqual(page.status_code, 200)
        today = stats_service.beijing_now().date()
        self.assertIn(f'name="start" value="{(today - timedelta(days=29)).isoformat()}"', page.text)
        self.assertIn(f'name="end" value="{today.isoformat()}"', page.text)


if __name__ == "__main__":
    unittest.main()
