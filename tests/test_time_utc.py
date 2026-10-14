"""时间口径护栏（2026-10-03 定稿）：**库里存 UTC，页面按用户时区展示**。

三条一起守：
  1. 写入：库里的时间就是 UTC（比北京时间早 8 小时）；
  2. 展示：页面按 cookie `tz` 渲染，默认北京 → 显示的是北京时间（用户看到的不变）；
  3. 边界：按天统计 / 定时任务按北京的自然日与小时，别拿 UTC 的去算。

任何一条被人改回「存北京时间」或「模板直接 strftime」，这里会先红。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, RawMessage, Settlement  # noqa: E402
from app.services import daily_summary as daily_summary_service  # noqa: E402
from app.timeutil import beijing_now, now, to_iso, to_local  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class TimeStorageTest(unittest.TestCase):
    """写入口径：落库的是 UTC。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        self._clean()

    def tearDown(self) -> None:
        self._clean()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(Settlement).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def test_10_stored_time_is_utc(self):
        """新建订单的归档时间应是 UTC（与北京时间差 8 小时）。"""
        with SessionLocal() as s:
            order = Order(platform="xianyu", order_no="TZ-1", sale_amount=100.0)
            s.add(order)
            s.commit()
            stamped = order.created_at
            self.assertIsNotNone(stamped)
            gap = (stamped - beijing_now()).total_seconds()
            self.assertLess(gap, 0, "库里应存 UTC（比北京时间早）")
            self.assertAlmostEqual(gap, -8 * 3600, delta=120, msg="差 8 小时")

    def test_20_model_default_is_utc(self):
        """模型 default 走 timeutil.now()（UTC），不是 datetime.now()（服务器本地）。"""
        with SessionLocal() as s:
            order = Order(platform="xianyu", order_no="TZ-2", sale_amount=1.0)
            s.add(order)
            s.flush()
            self.assertAlmostEqual(
                (order.created_at - now()).total_seconds(), 0, delta=60,
                msg="created_at 应该就是 now()（UTC）那一刻",
            )
            s.rollback()


class TimeRenderTest(unittest.TestCase):
    """展示口径：页面按用户时区渲染。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(Order).filter(Order.order_no.like("TZ-R%")).delete(
                synchronize_session=False
            )
            # 存一个「UTC 2026-01-02 16:30」= 北京 2026-01-03 00:30
            s.add(
                Order(
                    platform="xianyu",
                    order_no="TZ-R1",
                    product_name="时间口径测试",
                    sale_amount=10.0,
                    ordered_at=datetime(2026, 1, 2, 16, 30),
                    msg_time=datetime(2026, 1, 2, 16, 30),
                )
            )
            s.commit()

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(Order).filter(Order.order_no.like("TZ-R%")).delete(
                synchronize_session=False
            )
            s.commit()

    def test_30_page_renders_beijing_by_default(self):
        """默认（没有 tz cookie）按北京渲染：UTC 16:30 → 页面 01-03 00:30。"""
        client = TestClient(app)
        client.post("/login", data=ADMIN, follow_redirects=False)
        row = (
            client.get("/orders?keyword=TZ-R1").text
            + client.get("/orders").text
        )
        self.assertIn("01-03 00:30", row, "默认应按北京时间显示")

    def test_40_page_ignores_tz_cookie(self):
        """2026-10-13 起：**页面不跟浏览器时区走** —— 带纽约的 tz cookie 也按北京时间出。

        以前同一个时间两个人看到两个点，这就是「商品时间戳不是北京时间」的根因。
        要按各自时区看，得把 TZ_FOLLOW_BROWSER 设成 1。
        """
        client = TestClient(app)
        client.post("/login", data=ADMIN, follow_redirects=False)
        client.cookies.set("tz", "America/New_York")
        html = client.get("/orders").text
        self.assertIn("01-03 00:30", html, "统一按北京时间出，cookie 里的时区不算数")
        self.assertNotIn("01-02 11:30", html, "不该再按纽约时区渲染")

    def test_50_filters_are_registered(self):
        """两个模板环境都要有 dt / ts 过滤器（少了模板会直接报错）。"""
        from app.web.pages import templates as hub_templates
        from src.web.router import templates as self_templates

        for env in (hub_templates.env, self_templates.env):
            self.assertIn("dt", env.filters)
            self.assertIn("ts", env.filters)


class TimeBoundaryTest(unittest.TestCase):
    """边界口径：按天统计与定时任务按北京算。"""

    def test_60_biz_day_uses_beijing(self):
        """业务日：UTC 16:30 的单算北京第二天。"""
        from app.services.finance import biz_day_of

        order = Order(
            platform="xianyu", order_no="TZ-B1", sale_amount=1.0,
            ordered_at=datetime(2026, 1, 2, 16, 30),
        )
        self.assertEqual(biz_day_of(order), "2026-01-03")

    def test_70_daily_summary_hour_is_beijing(self):
        """日报定时看北京小时：北京 07:00（UTC 前一天 23:00）时还没到 8 点。"""
        from unittest import mock

        from app.config import settings

        old_enabled, old_hour = settings.daily_summary_enabled, settings.daily_summary_hour
        try:
            settings.daily_summary_enabled = True
            settings.daily_summary_hour = 8
            local = datetime(2026, 1, 3, 7, 0)
            with mock.patch.object(daily_summary_service, "beijing_now", lambda: local), \
                 mock.patch.object(
                     daily_summary_service, "now", lambda: local - timedelta(hours=8)
                 ):
                self.assertIsNone(
                    daily_summary_service.run_if_due(),
                    "北京 7 点还没到 8 点的推送时间，不该推",
                )
        finally:
            settings.daily_summary_enabled = old_enabled
            settings.daily_summary_hour = old_hour

    def test_80_iso_transport(self):
        """传输口径：ISO8601 —— **默认按服务端统一时区（北京 +08:00）出**，
        跟页面显示的是同一个点（不再出 UTC 的 Z）。"""
        self.assertEqual(to_iso(datetime(2026, 10, 3, 12, 0)), "2026-10-03T20:00:00+08:00")
        self.assertEqual(
            to_iso(datetime(2026, 10, 3, 12, 0), "Asia/Shanghai"), "2026-10-03T20:00:00+08:00"
        )
        self.assertEqual(to_local(datetime(2026, 10, 3, 12, 0), "Asia/Shanghai"),
                         datetime(2026, 10, 3, 20, 0))


class TimeStaticTest(unittest.TestCase):
    """静态护栏：业务代码里不许再出现裸的 datetime.now() / utcnow()。"""

    def test_90_no_raw_datetime_now_in_business_code(self):
        offenders: list[str] = []
        for root in (ROOT / "app", ROOT / "src"):
            for path in sorted(root.rglob("*.py")):
                if path.name == "timeutil.py":  # 唯一允许自己造时间的地方
                    continue
                for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    stripped = line.lstrip()
                    if stripped.startswith("#"):
                        continue
                    if "datetime.now(" in line or "datetime.utcnow(" in line:
                        offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {stripped.strip()}")
        self.assertEqual(
            [], offenders,
            "业务代码取时间一律走 app.timeutil（now / utc_now / beijing_now），否则库里存的不是 UTC",
        )


class HumanTimeTest(unittest.TestCase):
    """`human_time()`：拿不到 request，也跟着当前请求的时区走。"""

    def test_100_unified_display_tz(self):
        """展示时区全站统一：默认北京，**请求里带的时区不生效**（2026-10-13 口径）。"""
        from app.config import settings
        from app.timeutil import reset_current_tz, set_current_tz
        from app.utils import human_time

        value = datetime(2026, 1, 2, 16, 30)  # UTC = 北京 01-03 00:30 = 纽约 01-02 11:30
        token = set_current_tz("America/New_York")
        try:
            self.assertEqual(human_time(value), "2026-01-03 00:30", "统一按北京出，纽约不算数")
        finally:
            reset_current_tz(token)
        self.assertEqual(human_time(value), "2026-01-03 00:30", "没有请求上下文也按北京出")
        self.assertEqual(human_time(None), "-")
        self.assertEqual(human_time("2026-01-02 16:30"), "2026-01-02 16:30", "非 datetime 原样出")

        # 开了 TZ_FOLLOW_BROWSER 才跟请求时区走（给真有跨时区团队的场景留的门）
        saved = settings.tz_follow_browser
        settings.tz_follow_browser = True
        try:
            token = set_current_tz("America/New_York")
            try:
                self.assertEqual(human_time(value), "2026-01-02 11:30", "开了开关才按纽约出")
            finally:
                reset_current_tz(token)
        finally:
            settings.tz_follow_browser = saved


class TimeInputTest(unittest.TestCase):
    """输入口径：表单填的是用户时区的时间，存库要转回 UTC。"""

    def test_110_form_value_goes_back_to_utc(self):
        from app.services.product import _parse_datetime
        from app.timeutil import from_local, reset_current_tz, set_current_tz

        token = set_current_tz("Asia/Shanghai")
        try:
            parsed = _parse_datetime("2026-10-03T20:15")
        finally:
            reset_current_tz(token)
        self.assertEqual(parsed, datetime(2026, 10, 3, 12, 15), "北京 20:15 → 库里存 UTC 12:15")
        self.assertEqual(
            from_local(datetime(2026, 10, 3, 20, 15), "Asia/Shanghai"),
            datetime(2026, 10, 3, 12, 15),
        )
        # 渲染回去还是同一个点：`| dt` 按用户时区出，用户改完再存不会漂
        self.assertEqual(to_local(parsed, "Asia/Shanghai"), datetime(2026, 10, 3, 20, 15))


class TimeSqlDialectTest(unittest.TestCase):
    """按天统计的 SQL 必须按数据库方言写（2026-10-03 线上事故护栏）。

    曾经把 SQLite 独有的 ``date(col, '+8 hours')`` 直接跑在 PostgreSQL 上，线上
    首页直接 500：``function date(timestamp without time zone, character varying)
    does not exist``。测试库是 SQLite，跑一遍页面也发现不了，所以这里强行用
    PostgreSQL 方言把 SQL 编译出来检查。
    """

    def _compiled(self, expr, dialect_name: str) -> str:
        from sqlalchemy import select
        from sqlalchemy.dialects import postgresql, sqlite

        dialect = sqlite.dialect() if dialect_name == "sqlite" else postgresql.dialect()
        return str(select(expr).compile(dialect=dialect))

    def test_120_day_expr_sqlite_uses_date_modifier(self):
        from app.models import Order
        from app.services.stats import _day_expr

        sql = self._compiled(_day_expr(Order.created_at), "sqlite")
        self.assertIn("date(orders.created_at,", sql, "SQLite 走 date(col, '+8 hours')")

    def test_130_day_expr_postgres_has_no_sqlite_date(self):
        from unittest import mock

        from app.models import Order
        from app.services import daily_summary as ds
        from app.services.stats import _day_expr

        # 测试库是 SQLite，直接编译出来的永远是 SQLite 分支，
        # 所以这里把「当前方言」假装成 PostgreSQL，才能看到线上真正执行的 SQL
        with mock.patch("app.services.stats.dialect_name", lambda: "postgresql"):
            sql = self._compiled(_day_expr(Order.created_at), "postgresql")
        self.assertIn("to_char", sql, "PostgreSQL 走 to_char(col + 8h)")
        self.assertNotIn(
            "date(orders.created_at,",
            sql,
            "PostgreSQL 上没有两参数的 date()，写了就是线上 500",
        )
        # 日报那一处同样要守（以前是复制过去的同一段写法）
        with mock.patch("app.services.daily_summary.dialect_name", lambda: "postgresql"):
            sql2 = self._compiled(ds._day_expr(Order.created_at), "postgresql")
        self.assertIn("to_char", sql2)
        self.assertNotIn("date(orders.created_at,", sql2)


if __name__ == "__main__":
    unittest.main()
