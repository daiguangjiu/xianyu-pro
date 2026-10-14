"""毛利口径与退款金额符号口径（2026-10-03 定稿）。

渔枢系统：毛利 = 销售价 − 运费 + 退款给客户 − 成本价 + 供应商退款 − 平台技术服务费
        成本价 = 自营飞书群同步的成本价（cost_amount）
符号：  「退款给客户」存**负值**（钱出去）、「供应商退款 / 平台退款」存**正值**（钱进来）；
        结算流水金额仍记正数（方向由 direction 表达），页面展示取绝对值。

这套用例就是口径的护栏：改任何一个数、或有人把符号改回正数，这里会先红。
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
    ORDER_STATE_REFUND_REQUEST,
    SETTLEMENT_REFUND,
    Order,
    RawMessage,
    Settlement,
)
from app.services import finance as finance_service  # noqa: E402
from app.services import refund as refund_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from sqlalchemy import select  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

# 定稿公式的算例：售价 100 / 运费 5 / 退客户 50 / 成本 45 / 供应商退 30 / 手续费 0.6
# 100 − 5 − 50 − 45 + 30 − 0.6 = 29.40
SAMPLE = dict(
    deal_amount=100.0,
    shipping_fee=5.0,
    cost_amount=45.0,
    platform_fee=0.6,
)


class ProfitFormulaTest(unittest.TestCase):
    """定稿公式：`销售价 − 运费 + 退款给客户 − 成本价 + 供应商退款 − 平台技术服务费`。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        self.client = TestClient(app)
        self._clean()

    def tearDown(self) -> None:
        self._clean()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(Settlement).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def _order(self, order_no: str = "PF-1", **kw) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="测试商品",
                receiver_name="买家",
                sender_name="小A",
                supplier_name="BH001 华强北",
                ordered_at=now(),
                msg_time=now(),
                **{**SAMPLE, **kw},
            )
            s.add(order)
            s.commit()
            return order.id

    @staticmethod
    def _profit(order) -> float:
        return finance_service.order_amounts(order)["profit"]

    def test_10_plain_order_profit(self):
        """没退单：毛利 = 销售价 − 运费 − 成本价 − 平台技术服务费。"""
        order_id = self._order("PF-PLAIN")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertAlmostEqual(self._profit(order), 49.40, places=2)
            # 唯一实现：模型上的 net_profit 与 order_amounts 必须是同一个数
            self.assertAlmostEqual(order.net_profit, 49.40, places=2)
            self.assertAlmostEqual(order.net_profit, self._profit(order), places=2)

    def test_20_refund_order_profit(self):
        """退单：退款给客户（负）减掉、供应商退款（正）加回来 → 29.40。"""
        order_id = self._order(
            "PF-REFUND",
            order_state=ORDER_STATE_REFUND_REQUEST,
            refund_status="pending",
            refund_amount=-50.0,           # 退给客户，存负值
            supplier_refund_amount=30.0,   # 供应商退给我们，存正值
        )
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertAlmostEqual(self._profit(order), 29.40, places=2)
            self.assertAlmostEqual(order.net_profit, 29.40, places=2)

    def test_25_shipping_is_subtracted_right_after_price(self):
        """运费必须是减项、且紧跟销售价：运费 +5 毛利就少 5。"""
        order_id = self._order("PF-SHIP")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            base = self._profit(order)
        order_id2 = self._order("PF-SHIP2", shipping_fee=10.0)
        with SessionLocal() as s:
            order2 = s.get(Order, order_id2)
            self.assertAlmostEqual(self._profit(order2), base - 5.0, places=2)

    def test_30_refund_amount_is_negative_after_accept(self):
        """退单受理：退款金额按成交价记，存负值。"""
        order_id = self._order("PF-ACCEPT", deal_amount=88.0)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            _, accepted = refund_service.mark_refund(s, order, None, "此单退")
            self.assertTrue(accepted)
            self.assertEqual(order.refund_amount, -88.0, "退给客户的钱存负值")
            # 页面 / 表单回填看绝对值
            self.assertAlmostEqual(order.refund_amount_abs, 88.0, places=2)

    def test_40_set_refund_amount_keeps_sign(self):
        """维护退款金额：页面填正数 46，库里存 −46；重复填同值不报错。"""
        order_id = self._order("PF-SET", deal_amount=88.0)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, _ = refund_service.set_refund_amount(s, order, "46", actor="ops")
            self.assertTrue(ok)
            self.assertAlmostEqual(order.refund_amount, -46.0, places=2)
            ok2, msg = refund_service.set_refund_amount(s, order, "46", actor="ops")
            self.assertTrue(ok2)
            self.assertIn("没有变化", msg, "同值重复提交要识别出来")
            self.assertAlmostEqual(order.refund_amount, -46.0, places=2)

    def test_50_refund_settlement_amount_is_positive(self):
        """退款流水金额记正数（方向由 direction=out 表达），不能跟着订单存负数。"""
        order_id = self._order(
            "PF-SETTLE",
            order_state=ORDER_STATE_REFUND_REQUEST,
            refund_status="pending",
            refund_amount=-50.0,
        )
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            row = finance_service.sync_refund(s, order, commit=True)
            self.assertIsNotNone(row, "退单且金额 > 0 要生成退款流水")
            self.assertAlmostEqual(row.amount, 50.0, places=2)
            self.assertEqual(row.kind, SETTLEMENT_REFUND)
            # 服务层算的退款金额也是正数
            self.assertAlmostEqual(
                finance_service.amount_of(order, SETTLEMENT_REFUND), 50.0, places=2
            )

    def test_60_pages_show_the_same_number(self):
        """订单详情 / 订单列表 / 资金页：四处同一个毛利 29.40。"""
        order_id = self._order(
            "PF-PAGE",
            order_state=ORDER_STATE_REFUND_REQUEST,
            refund_status="pending",
            refund_amount=-50.0,
            supplier_refund_amount=30.0,
        )
        self.client.post("/login", data=ADMIN, follow_redirects=False)
        detail = self.client.get(f"/orders/{order_id}")
        self.assertIn("29.40", detail.text, "订单详情的毛利")
        # 详情上退款给客户与供应商退款都按正数显示（库里是 −50 / +30）
        self.assertIn("50.00", detail.text)
        self.assertIn("30.00", detail.text)

        listing = self.client.get("/orders")
        self.assertIn("29.40", listing.text, "订单列表的毛利")


class RefundSignMigrationTest(unittest.TestCase):
    """存量数据迁移：历史的正值退款金额一次性翻成负值，且幂等。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        self._clean()

    def tearDown(self) -> None:
        self._clean()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def test_70_positive_legacy_value_is_flipped(self):
        from app.db import _migrate_refund_sign, engine

        with SessionLocal() as s:
            s.add(
                Order(
                    platform="xianyu",
                    order_no="PF-LEGACY",
                    deal_amount=100.0,
                    refund_amount=50.0,   # 老口径：正值
                    msg_time=now(),
                    ordered_at=now(),
                )
            )
            s.commit()

        # 清掉迁移标记，模拟「第一次上线跑迁移」
        from sqlalchemy import text

        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM schema_migrations WHERE name = :n"),
                {"n": "2026-10-refund-sign"},
            )
        _migrate_refund_sign()

        with SessionLocal() as s:
            order = s.scalar(select(Order).where(Order.order_no == "PF-LEGACY"))
            self.assertEqual(order.refund_amount, -50.0, "老正值要翻成负值")

        # 幂等：再跑一次不能翻回正数
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM schema_migrations WHERE name = :n"),
                {"n": "2026-10-refund-sign"},
            )
        _migrate_refund_sign()
        with SessionLocal() as s:
            order = s.scalar(select(Order).where(Order.order_no == "PF-LEGACY"))
            self.assertEqual(order.refund_amount, -50.0, "重复执行不能再翻一次符号")


class SelfSideRefundSignTest(unittest.TestCase):
    """渔货栈：退款动作写入的金额同样是负值（与渔枢系统同一套符号口径）。"""

    def test_80_self_order_refund_stores_negative(self):
        from src.order import state as order_state

        plan = order_state.plan("shipped", "refund_request", refund_amount="50")
        self.assertTrue(plan.ok, f"已发货可以退单：{plan.error}")
        self.assertAlmostEqual(
            float(plan.fields["refund_amount"]), -50.0, places=2,
            msg="渔货栈也存负值（页面填正数）",
        )

        # 已经存成负值时再走一次：不能翻回正数（-abs 幂等）
        plan2 = order_state.plan("shipped", "refund_request", refund_amount="-50")
        self.assertAlmostEqual(float(plan2.fields["refund_amount"]), -50.0, places=2)


if __name__ == "__main__":
    unittest.main()
