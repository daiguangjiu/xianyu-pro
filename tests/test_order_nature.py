"""订单「性质」（自营订单 / 学员订单，2026-10-11 用户拍板）。

口径：订单性质不看订单本身，看**出货闲鱼号对应的店铺性质**（店铺管理里维护：
自营店铺 / 学员店铺）—— 账号在店铺档案里是「学员店铺」就是学员订单，其余
（含没有档案的账号、空账号）都算自营订单。学员店铺还会把「带教」带出来。

覆盖：
  1 列表：每行打「自营订单 / 学员订单」标，筛选 self / student 只出对应的单，筛选项带条数；
  2 详情：订单信息里有「订单性质」，学员订单带出带教；
  3 统计：首页「订单性质」区块与 service 层 nature_counts 口径一致；
  4 没建店铺档案的账号按自营算（默认口径），不会被算成学员订单。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    SHOP_NATURE_SELF_RUN,
    SHOP_NATURE_STUDENT,
    Order,
    RawMessage,
    Settlement,
    Shop,
    User,
)
from app.services import stats as stats_service  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402


class OrderNatureTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        self._clean()
        self.client = TestClient(app)
        with SessionLocal() as s:
            user = s.query(User).order_by(User.id).first()
            self.uid = user.id
        self.client.cookies.set(COOKIE_NAME, make_session(self.uid)[0])

    def tearDown(self) -> None:
        self._clean()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(Settlement).delete()
            s.query(Order).delete()
            s.query(Shop).delete()
            s.query(RawMessage).delete()
            s.commit()

    def _shop(self, name: str, code: str, nature: str, coach: str = "") -> int:
        with SessionLocal() as s:
            row = Shop(name=name, code=code, shop_nature=nature, coach=coach)
            s.add(row)
            s.commit()
            return row.id

    def _order(self, order_no: str, account: str) -> int:
        with SessionLocal() as s:
            row = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="测试商品",
                receiver_name="测试",
                xianyu_account=account,
                sale_amount=100.0,
                ordered_at=datetime(2026, 10, 10, 12, 0),
            )
            s.add(row)
            s.commit()
            return row.id

    # ------------------------------------------------------------------ 1 列表
    def test_01_list_marks_and_filters_by_nature(self):
        """列表每行有「订单性质」标；按自营 / 学员筛出来的单互不串台，筛选项带条数。"""
        self._shop("自营的号", "N001", SHOP_NATURE_SELF_RUN)
        self._shop("大卫不可以", "N002", SHOP_NATURE_STUDENT, coach="范志强")
        self._order("N-SELF-1", "自营的号")
        self._order("N-STU-1", "大卫不可以")

        page = self.client.get("/orders").text
        self.assertIn("订单性质", page)
        self.assertIn("自营订单", page)
        self.assertIn("学员订单", page)

        stu = self.client.get("/orders?nature=student").text
        self.assertIn("N-STU-1", stu)
        self.assertNotIn("N-SELF-1", stu)
        self.assertIn("学员订单（1）", stu, "筛选项要带条数")

        own = self.client.get("/orders?nature=self").text
        self.assertIn("N-SELF-1", own)
        self.assertNotIn("N-STU-1", own)

    # ------------------------------------------------------------------ 2 详情
    def test_02_detail_shows_nature_and_coach(self):
        """订单信息里有「订单性质」；学员订单把所属带教带出来。"""
        self._shop("大卫不可以", "N003", SHOP_NATURE_STUDENT, coach="范志强")
        oid = self._order("N-STU-2", "大卫不可以")
        page = self.client.get(f"/orders/{oid}").text
        self.assertIn("订单性质", page)
        self.assertIn("学员订单", page)
        self.assertIn("范志强", page, "学员订单要带出带教")

    # ------------------------------------------------------------------ 3 统计
    def test_03_counts_match_service_and_dashboard(self):
        """首页「订单性质」区块与 service 层 nature_counts 同一份口径。"""
        self._shop("自营的号", "N004", SHOP_NATURE_SELF_RUN)
        self._shop("大卫不可以", "N005", SHOP_NATURE_STUDENT)
        self._order("N-1", "自营的号")
        self._order("N-2", "大卫不可以")
        self._order("N-3", "还没建档的号")
        with SessionLocal() as s:
            counts = stats_service.nature_counts(s)
        self.assertEqual(counts["student"], 1)
        self.assertEqual(counts["self"], 2, "没建店铺档案的账号算自营")
        self.assertEqual(counts["total"], 3)

        dash = self.client.get("/").text
        self.assertIn("订单性质", dash)
        self.assertIn("/orders?nature=student", dash, "统计块点进去就是筛好的列表")

    # ------------------------------------------------------- 4 没档案按自营算
    def test_04_unknown_account_counts_as_self(self):
        """账号没建店铺档案（老数据 / 手填）按自营算，不会跑到学员订单里去。"""
        self._shop("大卫不可以", "N006", SHOP_NATURE_STUDENT)
        self._order("N-9", "江湖号")
        self.assertIn("N-9", self.client.get("/orders?nature=self").text)
        self.assertNotIn("N-9", self.client.get("/orders?nature=student").text)


if __name__ == "__main__":
    unittest.main()
