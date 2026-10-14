"""渔货栈订单状态机（2026-10-02 定的口径）。

正向：待确认 → 待推单 → **已推单（推给渔枢系统采购）** → 已发货 → 已完成
逆向：退款中 → 已退款；挂起/终止：异常单、已取消

要点：
1. 推单推的是 **渔枢系统**，不是 1688 —— 推完渔枢系统那边有笔单，渔货栈记住 hub_order_id；
2. 渔枢系统采购发货后把物流单号回写给渔货栈，渔货栈自动流转到「已发货」；
3. 不合法的流转一律拒绝（不会出现「已发货但没有物流单号」这种脏数据）。
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
from src.models import DownstreamOrder, OrderEvent  # noqa: E402
from src.order import flow, hub_sync, state  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
DSO_ID = "TXY20261002988"


def _reset() -> None:
    with SessionLocal() as s:
        s.query(OrderEvent).delete()
        s.query(OrderLog).delete()
        s.query(Order).delete()
        s.query(DownstreamOrder).delete()
        s.commit()


def _dso(order_no: str = DSO_ID, *, status: str = "pending") -> int:
    with SessionLocal() as s:
        row = DownstreamOrder(
            downstream_order_id=order_no,
            channel="xianyu",
            channel_sku_id="XY-SKU-1",
            buyer_name="李四",
            qty=1,
            sale_price=Decimal("120.00"),
            status=status,
        )
        s.add(row)
        s.commit()
        return row.id


def _hub_order(order_no: str = DSO_ID, **kw) -> int:
    with SessionLocal() as s:
        row = Order(
            platform="xianyu",
            order_no=order_no,
            order_state=kw.get("order_state", ORDER_STATE_NEW),
            origin=ORDER_ORIGIN_SELF,
        )
        s.add(row)
        s.commit()
        return row.id


def _status(order_no: str = DSO_ID) -> str:
    with SessionLocal() as s:
        row = s.scalars(
            select(DownstreamOrder).where(DownstreamOrder.downstream_order_id == order_no)
        ).first()
        return row.status


class StateMachineRules(unittest.TestCase):
    """状态机本身的规则（不碰库）。"""

    def test_10_forward_flow(self):
        self.assertTrue(state.can("pending", "confirm"))
        self.assertTrue(state.can("confirmed", "push_hub"))
        self.assertTrue(state.can("pushed", "ship"))
        self.assertTrue(state.can("shipped", "complete"))
        self.assertEqual(state.target_status("pending", "confirm"), "confirmed")
        self.assertEqual(state.target_status("shipped", "complete"), "completed")

    def test_20_illegal_transitions_rejected(self):
        """跳步不行：待确认不能直接发货，已完成的不能再推单。"""
        self.assertFalse(state.can("pending", "ship"))
        self.assertFalse(state.can("confirmed", "complete"))
        self.assertFalse(state.can("completed", "push_hub"))
        self.assertFalse(state.can("refunded", "ship"))
        self.assertFalse(state.can("cancelled", "confirm"))

    def test_30_ship_needs_tracking_no(self):
        """标记发货必须给物流单号。"""
        bad = state.plan("pushed", "ship")
        self.assertFalse(bad.ok)
        self.assertIn("物流单号", bad.error)
        good = state.plan("pushed", "ship", shipping_no="SF1234567890")
        self.assertTrue(good.ok)
        self.assertEqual(good.to_status, "shipped")
        self.assertEqual(good.fields["shipping_no"], "SF1234567890")

    def test_40_refund_and_exception_paths(self):
        self.assertTrue(state.can("shipped", "refund_request"))
        self.assertTrue(state.can("completed", "refund_request"))
        self.assertTrue(state.can("refunding", "refund_finish"))
        self.assertTrue(state.can("pushed", "mark_exception"))
        self.assertEqual(state.target_status("exception", "resolve_exception"), "confirmed")

    def test_50_terminal_has_no_actions(self):
        self.assertEqual(state.available_actions("refunded"), [])
        self.assertEqual(state.available_actions("cancelled"), [])
        self.assertIn("completed", state.TERMINAL_STATUSES)


class ApplyAction(unittest.TestCase):
    """落库：一次动作改状态 + 写履约字段 + 记流水。"""

    def setUp(self):
        init_db()
        ensure_default_admin()
        _reset()
        _dso()

    def tearDown(self):
        _reset()

    def test_10_confirm_then_reject_illegal(self):
        with SessionLocal() as s:
            ok, msg = flow.apply_action(s, DSO_ID, "confirm", actor="tester")
            self.assertTrue(ok, msg)
        self.assertEqual(_status(), "confirmed")
        with SessionLocal() as s:
            ok, msg = flow.apply_action(s, DSO_ID, "complete", actor="tester")
            self.assertFalse(ok)
            self.assertIn("不能做", msg)
        self.assertEqual(_status(), "confirmed", "被拒的流转不能改状态")

    def test_20_ship_writes_tracking_and_logs(self):
        with SessionLocal() as s:
            flow.apply_action(s, DSO_ID, "confirm")
            flow.apply_action(s, DSO_ID, "push_hub")
            ok, msg = flow.apply_action(
                s, DSO_ID, "ship", actor="渔枢系统", shipping_no="SF9988776655",
                shipping_company="顺丰",
            )
            self.assertTrue(ok, msg)
        with SessionLocal() as s:
            row = s.scalars(
                select(DownstreamOrder).where(DownstreamOrder.downstream_order_id == DSO_ID)
            ).first()
            self.assertEqual(row.status, "shipped")
            self.assertEqual(row.shipping_no, "SF9988776655")
            self.assertEqual(row.shipping_company, "顺丰")
            self.assertIsNotNone(row.shipped_at)
            self.assertIsNotNone(row.pushed_at)
            # 渔枢系统那边建档了，渔货栈记住了订单 id
            self.assertIsNotNone(row.hub_order_id)
            events = s.scalars(
                select(OrderEvent).where(OrderEvent.downstream_order_id == DSO_ID)
            ).all()
            self.assertEqual([e.action for e in events], ["confirm", "push_hub", "ship"])
            self.assertEqual(events[-1].actor, "渔枢系统")

    def test_30_push_hub_creates_hub_order(self):
        """推单推的是渔枢系统：推完渔枢系统库里得有这笔单。"""
        _hub_order()  # 已存在同单号时走「补空字段」分支
        with SessionLocal() as s:
            flow.apply_action(s, DSO_ID, "confirm")
            ok, msg = flow.apply_action(s, DSO_ID, "push_hub", actor="tester")
            self.assertTrue(ok, msg)
        with SessionLocal() as s:
            row = s.scalars(
                select(DownstreamOrder).where(DownstreamOrder.downstream_order_id == DSO_ID)
            ).first()
            self.assertEqual(row.status, "pushed")
            self.assertIsNotNone(row.hub_order_id)
            hub = s.get(Order, row.hub_order_id)
            self.assertEqual(hub.order_no, DSO_ID)

    def test_40_refund_uses_sale_price_by_default(self):
        with SessionLocal() as s:
            flow.apply_action(s, DSO_ID, "confirm")
            flow.apply_action(s, DSO_ID, "push_hub")
            flow.apply_action(s, DSO_ID, "ship", shipping_no="SF1")
            flow.apply_action(s, DSO_ID, "refund_request", actor="tester")
            flow.apply_action(s, DSO_ID, "refund_finish", actor="tester")
        with SessionLocal() as s:
            row = s.scalars(
                select(DownstreamOrder).where(DownstreamOrder.downstream_order_id == DSO_ID)
            ).first()
            self.assertEqual(row.status, "refunded")
            # 没填退款金额 → 按售价 120 记（与渔枢系统「退款金额按成交价」同一口径）
            # 符号口径（2026-10-03）：退给客户的钱存负值，与渔枢系统的 orders.refund_amount 一致
            self.assertEqual(float(row.refund_amount), -120.0)
            self.assertIsNotNone(row.refunded_at)


class HubFulfillmentWriteback(unittest.TestCase):
    """渔枢系统履约回写：发货 / 退款 → 渔货栈自动往下流转。"""

    def setUp(self):
        init_db()
        ensure_default_admin()
        _reset()
        _dso()
        _hub_order()

    def tearDown(self):
        _reset()

    def test_10_tracking_no_back_from_hub(self):
        """渔枢系统回传物流单号 → 渔货栈从「已推单」流到「已发货」。"""
        with SessionLocal() as s:
            flow.apply_action(s, DSO_ID, "confirm")
            flow.apply_action(s, DSO_ID, "push_hub")
        with SessionLocal() as s:
            done = hub_sync.sync_from_hub(
                s, DSO_ID, {"tracking_no": "YT77889900", "order_state": "shipped"}
            )
        self.assertIn("ship", done)
        self.assertEqual(_status(), "shipped")
        with SessionLocal() as s:
            row = s.scalars(
                select(DownstreamOrder).where(DownstreamOrder.downstream_order_id == DSO_ID)
            ).first()
            self.assertEqual(row.shipping_no, "YT77889900")
        # 幂等：再同步一次不会重复流转
        with SessionLocal() as s:
            done = hub_sync.sync_from_hub(
                s, DSO_ID, {"tracking_no": "YT77889900", "order_state": "shipped"}
            )
        self.assertEqual(done, [])

    def test_20_refund_back_from_hub(self):
        """渔枢系统退款完成 → 渔货栈走完退款收尾。"""
        with SessionLocal() as s:
            flow.apply_action(s, DSO_ID, "confirm")
            flow.apply_action(s, DSO_ID, "push_hub")
        with SessionLocal() as s:
            done = hub_sync.sync_from_hub(
                s, DSO_ID, {"order_state": "refunded", "refund_amount": 120, "tracking_no": ""}
            )
        self.assertIn("refund_request", done)
        self.assertIn("refund_finish", done)
        self.assertEqual(_status(), "refunded")


class HubSideWriteback(unittest.TestCase):
    """渔枢系统侧的回写钩子：渔枢系统发货后主动推给渔货栈。"""

    def setUp(self):
        init_db()
        ensure_default_admin()
        _reset()
        _dso()
        _hub_order()

    def tearDown(self):
        _reset()

    def test_10_notify_after_hub_shipped(self):
        """渔枢系统回填物流单号并 commit 之后，调回写 → 渔货栈跟着到「已发货」。"""
        from app.services import self_sync

        with SessionLocal() as s:
            flow.apply_action(s, DSO_ID, "confirm")
            flow.apply_action(s, DSO_ID, "push_hub")
        # 模拟运营在渔枢系统填了物流单号并保存
        with SessionLocal() as s:
            order = s.scalars(select(Order).where(Order.order_no == DSO_ID)).first()
            order.tracking_no = "SF5555666677"
            order.order_state = "shipped"
            s.commit()
        done = self_sync.notify_self_fulfillment(DSO_ID)
        self.assertIn("ship", done)
        self.assertEqual(_status(), "shipped")
        with SessionLocal() as s:
            row = s.scalars(
                select(DownstreamOrder).where(DownstreamOrder.downstream_order_id == DSO_ID)
            ).first()
            self.assertEqual(row.shipping_no, "SF5555666677")

    def test_20_ignore_non_self_order(self):
        """群消息来的单（origin 不是 self）不回写。"""
        from app.services import self_sync

        with SessionLocal() as s:
            order = s.scalars(select(Order).where(Order.order_no == DSO_ID)).first()
            order.origin = ""
            order.tracking_no = "SF000"
            s.commit()
        self.assertEqual(self_sync.notify_self_fulfillment(DSO_ID), [])
        self.assertEqual(_status(), "pending")

    def test_30_fulfillment_api(self):
        """分端部署时走的 HTTP 回写接口（要登录，渔枢系统自己调同进程函数不走它）。"""
        client = TestClient(app)
        client.post("/login", data=ADMIN, follow_redirects=False)
        r = client.post(
            f"/api/v1/orders/{DSO_ID}/fulfillment",
            json={"order_state": "shipped", "tracking_no": "YT123"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 这单还在「待确认」，没有发货这一步 → 什么都不做（状态机挡住了）
        self.assertEqual(_status(), "pending")


class WebActionRoute(unittest.TestCase):
    """页面上的动作按钮：走 /self/orders/{id}/action，非法动作被拒且不改状态。"""

    def setUp(self):
        init_db()
        ensure_default_admin()
        _reset()
        _dso()
        _hub_order()
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)

    def tearDown(self):
        _reset()

    def test_10_action_confirm_via_web(self):
        r = self.client.post(
            f"/self/orders/{DSO_ID}/action", data={"action": "confirm"}, follow_redirects=False
        )
        self.assertEqual(r.status_code, 303)
        self.assertEqual(_status(), "confirmed")

    def test_20_illegal_action_rejected(self):
        r = self.client.post(
            f"/self/orders/{DSO_ID}/action", data={"action": "ship"}, follow_redirects=False
        )
        self.assertEqual(r.status_code, 303)
        self.assertIn("err", r.headers["location"])
        self.assertEqual(_status(), "pending", "非法动作不能改状态")

    def test_30_detail_shows_flow_and_actions(self):
        """详情页有状态机：进度、动作按钮、流转流水。"""
        self.client.post(f"/self/orders/{DSO_ID}/action", data={"action": "confirm"})
        html = self.client.get(f"/self/orders/{DSO_ID}").text
        self.assertIn("订单流转", html)
        self.assertIn("待推单", html)
        self.assertIn("推给渔枢系统采购", html, "当前该做的动作是推给渔枢系统")
        self.assertIn("确认接单", html)  # 流水里记了这次动作

    def test_40_status_labels_on_list(self):
        html = self.client.get("/self/orders").text
        self.assertIn("已发货", html, "筛选 chips 里有新增的已发货")
        self.assertIn("退款中", html)
        self.assertIn("待确认", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
