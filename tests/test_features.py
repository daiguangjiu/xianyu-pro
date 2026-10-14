"""时区口径 / 供应商编码与序号 / 成本为 0 警示 / 数据总览成本与毛利率。

业务口径（2026-09 新增）：
  1 全站时间统一东八区（北京时间），系统时间字段不再存 UTC；
  2 供应商档案支持「供应商编码」维护，列表带系统序号，系统内统一显示
    「供应商名称（供应商编码）」；
  3 订单里的「所属供应链」填编码（如 GYS001）或名称，都会关联到同一家供应商档案；
  4 成本价为 0 的订单在列表/详情红色警示，提醒推单人员维护；
  5 数据总览各维度表补上成本与毛利率。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.db import _BEIJING_SHIFT_COLUMNS  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, OrderImage, PushBatchItem, Supplier  # noqa: E402
from app.services import push as push_service  # noqa: E402
from app.services import stats as stats_service  # noqa: E402
from app.services import supplier as supplier_service  # noqa: E402
from app.services.notify import local_now  # noqa: E402
from app.timeutil import (  # noqa: E402
    beijing_now,
    fmt,
    from_epoch_ms,
    now,
    to_iso,
    to_local,
    utc_now,
)
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class TimezoneTest(unittest.TestCase):
    """时间口径（2026-10-03 定稿）：**库里存 UTC**，展示按用户时区。"""

    def test_now_is_utc(self):
        """now() 存的是 UTC；北京时间 = 它 + 8 小时。"""
        diff = (now() - beijing_now()).total_seconds()
        self.assertAlmostEqual(diff, -8 * 3600, delta=5, msg="now() 应为 UTC（比北京早 8 小时）")
        self.assertAlmostEqual(
            (now() - utc_now()).total_seconds(), 0, delta=1, msg="utc_now 与 now 同义"
        )

    def test_from_epoch_ms_is_utc(self):
        self.assertIsNone(from_epoch_ms(None), "缺时间戳时返回 None")
        self.assertIsNone(from_epoch_ms(0), "0 视为没有时间戳")
        self.assertEqual(from_epoch_ms(1), datetime(1970, 1, 1, 0, 0, 0, 1000))
        self.assertEqual(
            from_epoch_ms(1758729600000),
            datetime(2025, 9, 24, 16, 0),
            "毫秒时间戳按 UTC 存（同一时刻的北京是 09-25 00:00）",
        )

    def test_to_local_uses_user_timezone(self):
        """展示口径：UTC naive -> 服务端统一时区（默认北京；模板 | dt 过滤器走这两个函数）。

        2026-10-13 起**不跟浏览器时区**：不传时区就是北京，传了也按统一时区算
        （要按别的时区出，得把 TZ_FOLLOW_BROWSER 打开）。
        """
        self.assertEqual(
            to_local(datetime(2026, 9, 24, 16, 0), "Asia/Shanghai"), datetime(2026, 9, 25, 0, 0)
        )
        self.assertEqual(
            fmt(datetime(2026, 9, 24, 16, 0), "%Y-%m-%d %H:%M", "Asia/Shanghai"),
            "2026-09-25 00:00",
        )
        # 统一口径：就算外面带了个纽约时区进来，也照样按北京出
        self.assertEqual(
            fmt(datetime(2026, 9, 24, 16, 0), "%Y-%m-%d %H:%M", "America/New_York"),
            "2026-09-25 00:00",
        )
        # 脏时区名回落到北京，不让页面崩
        self.assertEqual(
            fmt(datetime(2026, 9, 24, 16, 0), "%Y-%m-%d %H:%M", "Not/AZone"),
            "2026-09-25 00:00",
        )
        # 空值给空串（模板直接渲染不会冒 None）
        self.assertEqual(to_local(None), None)
        self.assertEqual(fmt(None), "")

    def test_to_iso_is_iso8601(self):
        """传输口径：ISO8601 —— 默认按服务端统一时区（北京 +08:00）出，跟页面同一个点。"""
        self.assertEqual(to_iso(datetime(2026, 10, 3, 12, 0)), "2026-10-03T20:00:00+08:00")
        self.assertEqual(
            to_iso(datetime(2026, 10, 3, 12, 0), "Asia/Shanghai"), "2026-10-03T20:00:00+08:00"
        )
        self.assertEqual(to_iso(None), None)

    def test_start_of_local_day(self):
        """按天统计的边界按用户时区：北京 00:00 = 前一天 16:00 UTC。"""
        from app.timeutil import start_of_local_day

        self.assertEqual(
            start_of_local_day("Asia/Shanghai", datetime(2026, 10, 3, 12, 0)),
            datetime(2026, 10, 2, 16, 0),
        )

    def test_cn_day_start_is_beijing_natural_day(self):
        """凌晨 0:30 仍算当天，不能因为时区换算被算到前一天。"""
        start, end = push_service.cn_day_start(datetime(2026, 9, 25, 0, 30))
        self.assertEqual(start, datetime(2026, 9, 25, 0, 0))
        self.assertEqual(end, datetime(2026, 9, 26, 0, 0))

    def test_migration_columns_cover_system_time_only(self):
        """系统时间字段要平移，飞书消息时间字段绝不能动（本来就是北京时间）。"""
        cols = set(_BEIJING_SHIFT_COLUMNS)
        for item in (
            ("orders", "created_at"),
            ("orders", "updated_at"),
            ("orders", "pushed_at"),
            ("operation_logs", "created_at"),
            ("raw_messages", "notified_at"),
        ):
            self.assertIn(item, cols)
        for item in (
            ("raw_messages", "msg_time"),
            ("orders", "msg_time"),
            ("orders", "ordered_at"),
            ("sync_states", "last_message_time"),
        ):
            self.assertNotIn(item, cols)

    def test_notify_local_now_follows_configured_offset(self):
        with mock.patch.object(settings, "notify_utc_offset", 0):
            self.assertAlmostEqual((local_now() - utc_now()).total_seconds(), 0, delta=5)
        with mock.patch.object(settings, "notify_utc_offset", 8):
            self.assertAlmostEqual((local_now() - utc_now()).total_seconds(), 8 * 3600, delta=5)

    def test_page_shows_beijing_time(self):
        client = _client()
        client.post(
            "/suppliers",
            data={"name": "时区校验供应商", "code": "TZ001"},
            follow_redirects=False,
        )
        try:
            with SessionLocal() as s:
                row = s.scalar(select(Supplier).where(Supplier.name == "时区校验供应商"))
                self.assertAlmostEqual(
                    (row.created_at - now()).total_seconds(), 0, delta=10, msg="入库时间应为北京时间"
                )
                # 页面上直接展示北京时间的年月日时分
                self.assertIn(now().strftime("%Y-%m-%d"), row.created_at.strftime("%Y-%m-%d"))
        finally:
            _cleanup_suppliers("时区校验供应商")


class CostZeroWarningTest(unittest.TestCase):
    """成本为 0：列表与详情都要红色警示，提醒推单人员维护。"""

    def setUp(self):
        init_db()
        self.client = _client()
        self.ids: list[int] = []

    def tearDown(self):
        _cleanup_orders(self.ids)

    def _make_order(self, order_no: str, sale: float, cost: float) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="测试商品",
                sale_amount=sale,
                cost_amount=cost,
                supplier_name="测试供应商",
                msg_time=now(),
            )
            s.add(order)
            s.commit()
            self.ids.append(order.id)
            return order.id

    def test_list_warns_and_marks_zero_cost(self):
        zero_id = self._make_order("TZCOST-0", 100.0, 0.0)
        ok_id = self._make_order("TZCOST-1", 100.0, 60.0)

        page = self.client.get("/orders?q=TZCOST")
        self.assertEqual(page.status_code, 200)
        self.assertIn("成本价未维护", page.text)
        self.assertIn("cost-zero", page.text)

        # 详情页：顶部提醒 + 输入框标红
        detail = self.client.get(f"/orders/{zero_id}")
        self.assertIn("成本价未维护", detail.text)
        self.assertIn("input-warn", detail.text)

        # 成本已维护的订单不标红
        detail_ok = self.client.get(f"/orders/{ok_id}")
        self.assertNotIn("input-warn", detail_ok.text)
        self.assertNotIn("成本价未维护", detail_ok.text)

    def test_warning_disappears_after_cost_filled(self):
        order_id = self._make_order("TZCOST-2", 100.0, 0.0)
        resp = self.client.post(
            f"/orders/{order_id}/edit",
            data={"order_no": "TZCOST-2", "cost_amount": "55", "quantity": "1"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        detail = self.client.get(f"/orders/{order_id}")
        self.assertNotIn("input-warn", detail.text)
        self.assertIn("55.00", detail.text)

    def test_dashboard_alerts_zero_cost(self):
        self._make_order("TZCOST-3", 100.0, 0.0)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        # 首页顶部提醒：有成本为 0 的订单
        self.assertIn("成本价为 ¥0", page.text)
        # 各维度表的成本列标红
        self.assertIn("cost-zero", page.text)


class SupplierCodeTest(unittest.TestCase):
    """供应商编码：新增/编辑可维护，列表带系统序号，编码可关联订单。"""

    def setUp(self):
        init_db()
        self.client = _client()
        self.order_ids: list[int] = []

    def tearDown(self):
        _cleanup_orders(self.order_ids)
        _cleanup_suppliers(
            "编码测试供应商A", "编码测试供应商B", "编码关联供应商", "编码关联供应商B",
            "编码唯一A", "编码唯一B",
        )

    def test_code_crud_and_list_numbers(self):
        for name, code in (("编码测试供应商A", "GYS001"), ("编码测试供应商B", "GYS002")):
            resp = self.client.post(
                "/suppliers", data={"name": name, "code": code, "city": "深圳"},
                follow_redirects=False,
            )
            self.assertEqual(resp.status_code, 303)

        with SessionLocal() as s:
            rows = s.scalars(
                select(Supplier).where(Supplier.name.like("编码测试供应商%")).order_by(Supplier.code)
            ).all()
            self.assertEqual([r.code for r in rows], ["GYS001", "GYS002"])
            supplier_id = rows[0].id

        page = self.client.get("/suppliers")
        self.assertIn("序号", page.text)
        self.assertIn("已维护编码", page.text)
        # 供应商统一显示为「供应商名称（供应商编码）」
        self.assertIn("编码测试供应商A", page.text)
        self.assertIn("（GYS001）", page.text)

        # 按编码搜索
        page = self.client.get("/suppliers?q=GYS002")
        self.assertIn("编码测试供应商B", page.text)
        self.assertNotIn("编码测试供应商A", page.text)

        # 编辑编码
        resp = self.client.post(
            f"/suppliers/{supplier_id}/edit",
            data={"name": "编码测试供应商A", "code": "GYS-A-9"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(Supplier, supplier_id).code, "GYS-A-9")

        detail = self.client.get(f"/suppliers/{supplier_id}")
        self.assertIn("GYS-A-9", detail.text)
        self.assertIn("编码测试供应商A（GYS-A-9）", detail.text)

    def test_duplicate_code_rejected(self):
        """编码是关联订单的关键字，重复编码必须拦住。"""
        self.client.post(
            "/suppliers", data={"name": "编码唯一A", "code": "GYS910"},
            follow_redirects=False,
        )
        page = self.client.post(
            "/suppliers", data={"name": "编码唯一B", "code": "gys910"},
            follow_redirects=True,
        )
        self.assertIn("已被", page.text)
        with SessionLocal() as s:
            self.assertIsNone(s.scalar(select(Supplier).where(Supplier.name == "编码唯一B")))

    def test_order_supplier_code_links_to_profile(self):
        """订单里的「所属供应链」填编码：自动关联档案、统一显示「名称（编码）」。"""
        self.client.post(
            "/suppliers", data={"name": "编码关联供应商", "code": "GYS900"},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            order = Order(
                platform="xianyu", order_no="SUPCODE-1", product_name="测试商品",
                sale_amount=100.0, cost_amount=50.0, supplier_name="GYS900", msg_time=now(),
            )
            s.add(order)
            s.commit()
            order_id = order.id
            self.order_ids.append(order_id)
            supplier = s.scalar(select(Supplier).where(Supplier.code == "GYS900"))
            supplier_id = supplier.id
            # 归一化：编码 -> 名称（编码）
            self.assertEqual(
                supplier_service.normalize_order_supplier(s, "GYS900"), "编码关联供应商（GYS900）"
            )
            # 关联订单 / 统计：只写了编码也能算到这家
            self.assertEqual(
                [o.id for o in supplier_service.supplier_orders(s, supplier, 10)], [order_id]
            )
            self.assertEqual(supplier_service.supplier_stat(s, supplier)["orders"], 1)
            stats = stats_service.by_supplier(s, 20)
        target = [r for r in stats if r["supplier"] == "编码关联供应商（GYS900）"]
        self.assertTrue(target, f"总览应把编码写法合并到档案名下：{stats}")

        # 详情页与订单列表筛选都按编码能命中
        self.assertIn("编码关联供应商（GYS900）", self.client.get(f"/suppliers/{supplier_id}").text)
        self.assertIn("SUPCODE-1", self.client.get("/orders?supplier=GYS900").text)
        self.assertIn("SUPCODE-1", self.client.get("/orders?supplier=编码关联供应商").text)

        # 档案改名后，关联订单里的写法自动跟随
        self.client.post(
            f"/suppliers/{supplier_id}/edit",
            data={"name": "编码关联供应商B", "code": "GYS900"},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            self.assertEqual(
                s.get(Order, order_id).supplier_name, "编码关联供应商B（GYS900）"
            )


class DashboardCostMarginTest(unittest.TestCase):
    """数据总览：各维度表要有成本与毛利率，且算法正确。"""

    def setUp(self):
        init_db()
        self.client = _client()
        self.ids: list[int] = []
        with SessionLocal() as s:
            for i, (sale, cost) in enumerate(((100.0, 60.0), (200.0, 100.0))):
                order = Order(
                    platform="xianyu",
                    order_no=f"TZMARGIN-{i}",
                    product_name="毛利率测试商品",
                    sale_amount=sale,
                    cost_amount=cost,
                    supplier_name="毛利率测试供应商",
                    xianyu_account="毛利率测试闲鱼号",
                    payee_name="毛利率测试归属人",
                    msg_time=now(),
                )
                s.add(order)
                s.commit()
                self.ids.append(order.id)

    def tearDown(self):
        _cleanup_orders(self.ids)

    def test_margin_math(self):
        self.assertEqual(stats_service.margin(40.0, 100.0), 40.0)
        self.assertEqual(stats_service.margin(10.0, 0.0), 0.0)

    def test_dashboard_shows_cost_and_margin(self):
        with SessionLocal() as s:
            supplier = next(
                r for r in stats_service.by_supplier(s) if r["supplier"] == "毛利率测试供应商"
            )
            self.assertEqual(supplier["cost"], 160.0)
            self.assertEqual(supplier["profit"], 140.0)
            self.assertEqual(supplier["margin"], round(140 / 300 * 100, 1))
            account = next(
                r for r in stats_service.by_account(s) if r["account"] == "毛利率测试闲鱼号"
            )
            self.assertEqual(account["cost"], 160.0)
            self.assertEqual(account["margin"], 46.7)
            payee = next(
                r for r in stats_service.by_payee(s) if r["payee"] == "毛利率测试归属人"
            )
            self.assertEqual(payee["cost"], 160.0)
            top = next(
                r for r in stats_service.top_products(s) if r["product"] == "毛利率测试商品"
            )
            self.assertEqual(top["cost"], 160.0)
            self.assertEqual(top["margin"], 46.7)

        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("毛利率", page.text)
        self.assertIn("46.7%", page.text)


def _client() -> TestClient:
    init_db()
    ensure_default_admin()
    client = TestClient(app)
    resp = client.post("/login", data=ADMIN, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return client


def _cleanup_orders(order_ids: list[int]) -> None:
    with SessionLocal() as s:
        for order_id in order_ids:
            s.query(PushBatchItem).filter(PushBatchItem.order_id == order_id).delete()
            s.query(OrderImage).filter(OrderImage.order_id == order_id).delete()
            s.query(Order).filter(Order.id == order_id).delete()
        s.commit()


def _cleanup_suppliers(*names: str) -> None:
    with SessionLocal() as s:
        for name in names:
            s.query(Supplier).filter(Supplier.name == name).delete()
        s.commit()


if __name__ == "__main__":
    unittest.main()
