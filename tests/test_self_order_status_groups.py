"""2026-10-16 用户拍板：渔货栈订单状态**收敛成七个** ——
待确认 / 待发货 / 已发货 / 已完成 / 已退款 / 已关闭 / 异常单。

内部九个状态不变（推单、退款中这些动作还要流转），只是「给人看、给人筛」的口径收敛：
· 已推单 → 并入 待发货（本质上还是等着发货）
· 退款中 → 并入 已退款（过程态）
· 已取消 → 改叫 已关闭
"""
from __future__ import annotations

import re
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
from app.models import User  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402
from src.models import DownstreamOrder  # noqa: E402
from src.order import state as order_state  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

SEVEN = ("待确认", "待发货", "已发货", "已完成", "已退款", "已关闭", "异常单")


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(DownstreamOrder).delete(synchronize_session=False)
        s.query(User).filter(User.username.in_(("stu_grp_a", "self_run_b"))).delete(
            synchronize_session=False
        )
        s.commit()


def _dso(no: str, status: str) -> None:
    with SessionLocal() as s:
        s.add(DownstreamOrder(
            downstream_order_id=no, channel="xianyu", channel_sku_id="SKU-1",
            buyer_name="李四", qty=1, sale_price=Decimal("10.00"), status=status,
        ))
        s.commit()


class OrderStatusGroupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        assert cls.client.post("/login", data=ADMIN, follow_redirects=False).status_code == 303

    def setUp(self):
        _wipe()
        for no, st in (
            ("SG-PEND", "pending"),
            ("SG-CONF", "confirmed"),
            ("SG-PUSH", "pushed"),
            ("SG-SHIP", "shipped"),
            ("SG-DONE", "completed"),
            ("SG-ING", "refunding"),
            ("SG-REF", "refunded"),
            ("SG-CANCEL", "cancelled"),
            ("SG-EXC", "exception"),
        ):
            _dso(no, st)

    def tearDown(self):
        _wipe()

    # ------------------------------------------------ ① 七个状态
    def test_10_seven_status_chips_only(self):
        html = self.client.get("/self/orders").text
        keys = re.findall(r"/self/orders\?status=([a-z_]+)", html)
        self.assertEqual(
            sorted(set(keys)), sorted(order_state.GROUP_ORDER),
            "筛选 chips 只能是七个对外状态",
        )
        # chips 区块（状态按钮那一行）里不再出现旧状态名
        i = html.find('class="d-flex gap-2 flex-wrap mb-4 align-items-center"')
        self.assertGreater(i, -1, "状态筛选行在")
        chips = html[i : i + 2500]
        for label in SEVEN:
            self.assertIn(label, chips, f"筛选 chips 要有「{label}」")
        for gone in ("已推单", "退款中", "已取消"):
            self.assertNotIn(gone, chips, f"chips 里不该再有「{gone}」")

    # ------------------------------------------------ ② 合并后的筛选
    def test_20_group_filter_merges_inner_statuses(self):
        to_ship = self.client.get("/self/orders?status=to_ship").text
        self.assertIn("SG-PUSH", to_ship, "已推单并入待发货")
        self.assertIn("SG-CONF", to_ship)
        self.assertNotIn("SG-PEND", to_ship)

        refunded = self.client.get("/self/orders?status=refunded").text
        self.assertIn("SG-ING", refunded, "退款中并入已退款")
        self.assertIn("SG-REF", refunded)

        closed = self.client.get("/self/orders?status=closed").text
        self.assertIn("SG-CANCEL", closed, "已取消 = 已关闭")

        shipped = self.client.get("/self/orders?status=shipped").text
        self.assertIn("SG-SHIP", shipped)
        self.assertNotIn("SG-DONE", shipped)

    # ------------------------------------------------ ③ 徽标显示七态
    def test_30_badge_shows_the_group_label(self):
        html = self.client.get("/self/orders").text
        # 已推单那一行的状态徽标写「待发货」；退款中那一行写「已退款」
        for no, want in (("SG-PUSH", "待发货"), ("SG-ING", "已退款"), ("SG-CANCEL", "已关闭")):
            i = html.find(no)
            self.assertGreater(i, -1, f"{no} 在列表里")
            row = html[i - 200 : i + 2000]
            self.assertIn(want, row, f"{no} 的状态徽标要写「{want}」（走七态口径）")

    # ------------------------------------------------ ④ 计数按七态汇总
    def test_40_counts_sum_by_group(self):
        html = self.client.get("/self/orders").text
        # 待发货 = 待发货 1 + 已推单 1 = 2；已退款 = 退款中 1 + 已退款 1 = 2
        self.assertEqual(order_state.group_counts({"confirmed": 1, "pushed": 1})["to_ship"], 2)
        self.assertEqual(order_state.group_counts({"refunding": 1, "refunded": 1})["refunded"], 2)
        self.assertIn("共 9 笔", html)


if __name__ == "__main__":
    unittest.main()
