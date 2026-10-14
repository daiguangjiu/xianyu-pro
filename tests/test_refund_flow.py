"""逆向订单（推单 / 退单）口径 —— 2026-09-29 独立版本。

规则（业务口径）：
  1 未推单 + 用户撤单 -> 推单状态「用户撤单，无需推单」；
    未推单 + 用户退单 -> 「用户退单，无需推单」；
  2 已推单、供应商还没回物流单号（视作未发货）+ 退单 -> 「用户退单，同步撤回发货」；
  3 已推单、已回单、用户还没收到货 + 退单 -> 「用户退单，同步物流拦截」（**受理**）；
  4 已推单、已回单、用户已收到货 + 退单 -> 「用户收货退单，同步退单物流单号」，
    订单上要维护退单物流单号；
  5 收尾：已付款给供应商的 -> 线下核对后「标记供应商已退款」，订单自动「关闭完成」；
    未付款的 -> 不用付款（也不许付款），直接「关闭完成」；
  6 退款金额 = 订单成交价。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    ORDER_STATE_CLOSED,
    ORDER_STATE_REFUND_REQUEST,
    Order,
    RawMessage,
)
from app.services import order_state as order_state_service  # noqa: E402
from app.services import push as push_service  # noqa: E402
from app.services import refund as refund_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class RefundPushStatusTest(unittest.TestCase):
    """推单状态按订单上的事实推出来（撤单 / 退单 / 推没推 / 有没有单号 / 收没收货）。"""

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
            order = Order(
                platform="xianyu",
                order_no=order_no,
                deal_amount=kw.pop("deal_amount", 100.0),
                msg_time=now(),
                **kw,
            )
            s.add(order)
            s.commit()
            return order.id

    def _read(self, order_id: int) -> Order:
        with SessionLocal() as s:
            return s.get(Order, order_id)

    def test_10_canceled_before_push(self):
        """规则1：没推单就撤单 -> 用户撤单，无需推单。"""
        order_id = self._order("RF-1", is_recalled=True)
        self.assertEqual(
            push_service.refund_push_status(self._read(order_id)),
            push_service.PUSH_STATUS_CANCELED,
        )

    def test_20_refund_before_push(self):
        """规则1：没推单就退单 -> 用户退单，无需推单。"""
        order_id = self._order("RF-2", refund_status="pending")
        self.assertEqual(
            push_service.refund_push_status(self._read(order_id)),
            push_service.PUSH_STATUS_REFUND_NO_PUSH,
        )

    def test_30_refund_after_push_without_tracking(self):
        """规则2：已推单、供应商还没回单 -> 用户退单，同步撤回发货。"""
        order_id = self._order(
            "RF-3", refund_status="pending", supplier_status="pushed", pushed_at=now()
        )
        self.assertEqual(
            push_service.refund_push_status(self._read(order_id)),
            push_service.PUSH_STATUS_RECALL_SHIP,
        )

    def test_40_refund_after_shipping_not_received(self):
        """规则3：已回单、用户还没收到货 -> 用户退单，同步物流拦截。"""
        order_id = self._order(
            "RF-4",
            refund_status="pending",
            supplier_status="pushed",
            pushed_at=now(),
            tracking_no="SF001",
        )
        self.assertEqual(
            push_service.refund_push_status(self._read(order_id)),
            push_service.PUSH_STATUS_INTERCEPT,
        )

    def test_50_refund_after_received(self):
        """规则4：用户已收到货 -> 用户收货退单，同步退单物流单号。"""
        order_id = self._order(
            "RF-5",
            refund_status="pending",
            supplier_status="pushed",
            pushed_at=now(),
            tracking_no="SF002",
            is_received=True,
        )
        self.assertEqual(
            push_service.refund_push_status(self._read(order_id)),
            push_service.PUSH_STATUS_RETURN,
        )

    def test_60_normal_order_keeps_supplier_status(self):
        """正常订单：推单状态还是老的「待推单发货 / 已推单」。"""
        self.assertEqual(push_service.refund_push_status(self._read(self._order("RF-6"))), "")
        pushed = self._read(self._order("RF-7", supplier_status="pushed", pushed_at=now()))
        self.assertEqual(push_service.supplier_status_label(pushed), "已推单")


class RefundAcceptTest(unittest.TestCase):
    """已发货的退单同样受理（不再「不同意退单」），退款金额按成交价记。"""

    def setUp(self) -> None:
        init_db()
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def test_10_shipped_refund_is_accepted(self):
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no="RF-SHIPPED",
                order_state="shipped",
                deal_amount=396.0,
                supplier_status="pushed",
                pushed_at=now(),
                tracking_no="SF003",
                transfer_status="paid",
                msg_time=now(),
            )
            s.add(order)
            s.commit()
            order_id = order.id
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            _, accepted = refund_service.mark_refund(s, order, None, "此单退")
            self.assertTrue(accepted, "已发货的退单也要受理")
            self.assertEqual(order.refund_status, "pending")
            self.assertEqual(order.order_state, ORDER_STATE_REFUND_REQUEST)
            # 符号口径（2026-10-03）：退款金额按成交价记，存负值（钱出去）
            self.assertEqual(order.refund_amount, -396.0, "退款金额按成交价（存负值）")
            self.assertEqual(
                push_service.refund_push_status(order), push_service.PUSH_STATUS_INTERCEPT
            )


class RefundCloseFlowTest(unittest.TestCase):
    """收尾：已付款的等供应商退款、未付款的直接关，关闭完成是终态。"""

    def setUp(self) -> None:
        init_db()
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def _refund_order(self, order_no: str, transfer_status: str = "paid") -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                deal_amount=50.0,
                order_state=ORDER_STATE_REFUND_REQUEST,
                refund_status="pending",
                # 符号口径（2026-10-03）：退给客户的钱存负值
                refund_amount=-50.0,
                supplier_status="pushed",
                pushed_at=now(),
                transfer_status=transfer_status,
                msg_time=now(),
            )
            s.add(order)
            s.commit()
            return order.id

    def test_10_paid_order_waits_for_supplier_refund(self):
        """已付款：没标记供应商退款就关 -> 拦住（钱还在供应商那儿）。"""
        order_id = self._refund_order("RF-PAID", transfer_status="paid")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, msg = refund_service.close_refund_order(s, order, actor="ops")
            self.assertFalse(ok)
            self.assertIn("供应商已退款", msg)
            self.assertNotEqual(order.order_state, ORDER_STATE_CLOSED)

    def test_20_supplier_refunded_closes_order(self):
        """已付款 + 标记供应商已退款 -> 订单自动关闭完成。"""
        order_id = self._refund_order("RF-PAID2", transfer_status="paid")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, msg = refund_service.mark_supplier_refunded(s, order, actor="ops")
            self.assertTrue(ok, msg)
            self.assertEqual(order.supplier_refunded, "paid")
            self.assertEqual(order.order_state, ORDER_STATE_CLOSED)
            self.assertIsNotNone(order.closed_at)

    def test_30_unpaid_order_closes_directly(self):
        """未付款：退款给客户后直接关闭完成（不用追供应商退款）。"""
        order_id = self._refund_order("RF-UNPAID", transfer_status="unpaid")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            refund_service.set_refund_paid(s, order, True, actor="ops")
            refund_service.mark_stopped(s, order, actor="ops")
            ok, msg = refund_service.close_refund_order(s, order, actor="ops")
            self.assertTrue(ok, msg)
            self.assertEqual(order.order_state, ORDER_STATE_CLOSED)

    def test_35_close_blocked_until_customer_refunded(self):
        """硬拦：还没退款给客户不给关；线下已退的走强制关闭（必须写原因）。"""
        order_id = self._refund_order("RF-BLOCK", transfer_status="unpaid")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, msg = refund_service.close_refund_order(s, order, actor="ops")
            self.assertFalse(ok)
            self.assertIn("还没退款给客户", msg)
            # 不写原因的强制关闭也不给过
            ok, msg = refund_service.close_refund_order(
                s, order, actor="ops", force=True
            )
            self.assertFalse(ok)
            self.assertIn("强制关闭必须写明原因", msg)
            # 写明原因的强制关闭放行，并留痕
            ok, msg = refund_service.close_refund_order(
                s, order, actor="ops", note="已微信退款给客户 46 元", force=True
            )
            self.assertTrue(ok, msg)
            self.assertEqual(order.order_state, ORDER_STATE_CLOSED)

    def test_36_received_order_needs_return_tracking(self):
        """已收货的退单：没填退回物流单号不给关（货没着落）。"""
        order_id = self._refund_order("RF-RECV", transfer_status="unpaid")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            refund_service.set_refund_paid(s, order, True, actor="ops")
            refund_service.mark_stopped(s, order, actor="ops")
            refund_service.set_received(s, order, True, actor="ops")
            ok, msg = refund_service.close_refund_order(s, order, actor="ops")
            self.assertFalse(ok)
            self.assertIn("退回快递单号", msg)
            refund_service.set_return_tracking_no(s, order, "SF999", actor="ops")
            # 填了单号只说明客户寄了 —— 收到货之前还是不给关
            ok, msg = refund_service.close_refund_order(s, order, actor="ops")
            self.assertFalse(ok)
            self.assertIn("退回件还没收到", msg)
            refund_service.mark_return_received(s, order, actor="ops")
            ok, msg = refund_service.close_refund_order(s, order, actor="ops")
            self.assertTrue(ok, msg)

    def test_37_refund_steps_and_path(self):
        """路径与步骤：按订单推到哪一步派生，未付款的单不要求供应商退款。"""
        order_id = self._refund_order("RF-STEPS", transfer_status="unpaid")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            # 已推单未发货：已叫停（撤回发货）是必需步骤
            self.assertEqual(refund_service.refund_path(order), "pushed")
            steps = {s0["key"]: s0 for s0 in refund_service.refund_steps(order)}
            self.assertFalse(steps["supplier_refunded"]["required"])
            self.assertTrue(steps["refunded"]["required"])
            self.assertIn("已叫停", refund_service.missing_steps(order)[0])
            # 出了物流单号 -> 路径变成「已发货未收货」
            order.tracking_no = "SF1"
            s.commit()
            self.assertEqual(refund_service.refund_path(order), "shipped")
            # 已收货 -> 路径变成「已收货」，退回件成为必需步骤
            order.is_received = True
            s.commit()
            self.assertEqual(refund_service.refund_path(order), "received")
            self.assertIn("客户已寄回", "、".join(refund_service.missing_steps(order)))

    def test_38_scan_anomalies(self):
        """体检：已关闭但客户未退款的单要被挑出来。"""
        order_id = self._refund_order("RF-SCAN", transfer_status="unpaid")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            refund_service.close_refund_order(
                s, order, actor="ops", note="线下已退", force=True
            )
            rows = refund_service.scan_anomalies(s)
            hit = [r for r in rows if r["order_no"] == "RF-SCAN"]
            self.assertTrue(any(r["kind"] == "closed_unpaid" for r in hit), hit)

    def test_45_mark_stopped_and_return_received(self):
        """阶段二：已叫停 / 已收到退回件 单独记时间与动作。"""
        order_id = self._refund_order("RF-FACTS", transfer_status="unpaid")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            # 不指定动作时按路径自动选：已推单未发货 -> 撤回发货
            ok, msg = refund_service.mark_stopped(s, order, actor="ops")
            self.assertTrue(ok, msg)
            self.assertEqual(order.stopped_action, "withdraw")
            self.assertEqual(order.stopped_by, "ops")
            self.assertIsNotNone(order.stopped_at)
            # 没填退回单号时不给标记收到
            ok, msg = refund_service.mark_return_received(s, order, actor="ops")
            self.assertFalse(ok)
            self.assertIn("还没有退回快递单号", msg)
            refund_service.set_return_tracking_no(s, order, "SF777", actor="ops")
            order.is_received = True
            s.commit()
            ok, msg = refund_service.mark_return_received(s, order, actor="ops")
            self.assertTrue(ok, msg)
            self.assertIsNotNone(order.return_received_at)

    def test_46_tracking_status_fills_facts(self):
        """物流轨迹接入：只补空缺的事实，不覆盖人工标记。"""
        order_id = self._refund_order("RF-TRACK", transfer_status="unpaid")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.tracking_no = "SF1"
            s.commit()
            # 正向件已签收 -> 自动标记已收货
            ok, msg = refund_service.apply_tracking_status(s, order, "signed")
            self.assertTrue(ok, msg)
            self.assertTrue(order.is_received)
            # 正向件拦截成功 -> 自动标记已叫停（物流拦截）
            order2_id = self._refund_order("RF-TRACK2", transfer_status="unpaid")
            order2 = s.get(Order, order2_id)
            ok, msg = refund_service.apply_tracking_status(s, order2, "intercepted")
            self.assertTrue(ok, msg)
            self.assertIsNotNone(order2.stopped_at)
            self.assertEqual(order2.stopped_action, "intercept")
            # 人工已标记的不会被轨迹覆盖
            order2.is_received = True
            order2.return_tracking_no = "SF2"
            s.commit()
            refund_service.mark_return_received(s, order2, actor="ops")
            first_at = order2.return_received_at
            ok, msg = refund_service.apply_tracking_status(
                s, order2, "signed", on_return=True
            )
            self.assertTrue(ok, msg)
            self.assertEqual(order2.return_received_at, first_at)
            # 退回件签收会补上「已收到退回件」（这个单号还没标过）
            order.return_tracking_no = "SF3"
            s.commit()
            ok, msg = refund_service.apply_tracking_status(
                s, order, "signed", on_return=True
            )
            self.assertTrue(ok, msg)
            self.assertIsNotNone(order.return_received_at)

    def test_47_backfill_refund_facts(self):
        """回填：老数据按旧口径补「已叫停」，已关闭且已收货的补「已收到退回件」。"""
        order_id = self._refund_order("RF-BF", transfer_status="unpaid")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.refund_status = "intercepted"
            order.is_received = True
            order.return_tracking_no = "SF-BF"
            order.refund_paid = "paid"
            s.commit()
            refund_service.mark_return_received(s, order, actor="ops")
            refund_service.mark_stopped(s, order, actor="ops")
            refund_service.close_refund_order(s, order, actor="ops")
            order.stopped_at = None
            order.return_received_at = None
            s.commit()
            n = refund_service.backfill_refund_facts(s)
            self.assertGreaterEqual(n, 2)
            self.assertIsNotNone(order.stopped_at)
            self.assertIsNotNone(order.return_received_at)

    def test_48_blocking_uses_return_received(self):
        """已收货的退单：没收到退回件不给关（填单号不等于收到货）。"""
        order_id = self._refund_order("RF-RR", transfer_status="unpaid")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.is_received = True
            s.commit()
            refund_service.mark_stopped(s, order, actor="ops")
            refund_service.set_return_tracking_no(s, order, "SF-RR", actor="ops")
            refund_service.set_refund_paid(s, order, True, actor="ops")
            ok, msg = refund_service.close_refund_order(s, order, actor="ops")
            self.assertFalse(ok)
            self.assertIn("退回件还没收到", msg)
            refund_service.mark_return_received(s, order, actor="ops")
            ok, msg = refund_service.close_refund_order(s, order, actor="ops")
            self.assertTrue(ok, msg)

    def test_49_current_step_follows_progress(self):
        """「现在要做的」随进度自动走到下一步，全做完给 None（该收尾关单）。"""
        order_id = self._refund_order("RF-NOW", transfer_status="unpaid")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            step = refund_service.current_step(order)
            self.assertEqual(step["key"], "stopped")
            self.assertEqual(step["action"], "stopped")
            self.assertTrue(step["hint"] and step["button"])
            refund_service.mark_stopped(s, order, actor="ops")
            step = refund_service.current_step(order)
            self.assertEqual(step["key"], "refunded")
            self.assertEqual(step["action"], "pay")  # 退款这一步能顺手改金额
            self.assertEqual(step["input"], "refund_amount")
            refund_service.set_refund_paid(s, order, True, actor="ops")
            self.assertIsNone(refund_service.current_step(order))
            refund_service.close_refund_order(s, order, actor="ops")
            self.assertIsNone(refund_service.current_step(order))

    def test_51_detail_page_focuses_current_step(self):
        """详情页只展开「现在要做的」这一步，其它收进折叠区。"""
        order_id = self._refund_order("RF-PAGE", transfer_status="unpaid")
        client = TestClient(app)
        client.post("/login", data=ADMIN, follow_redirects=False)
        resp = client.get(f"/orders/{order_id}")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        html = resp.text
        self.assertIn("现在要做的：", html)
        self.assertIn("refund-track", html)  # 进度一行带走
        self.assertIn("更多设置：退单理由", html)
        self.assertIn("其它操作：同步供应商退单", html)

    def test_50_todo_page_groups_by_missing_step(self):
        """退单待办页：未关闭的退单按「第一个还缺的步骤」分组。"""
        self._refund_order("RF-TODO1", transfer_status="unpaid")
        order_id = self._refund_order("RF-TODO2", transfer_status="unpaid")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            refund_service.mark_stopped(s, order, actor="ops")
        client = TestClient(app)
        resp = client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:300])
        resp = client.get("/refund-todo")
        self.assertEqual(resp.status_code, 200, resp.text[:400])
        self.assertIn("RF-TODO1", resp.text)
        self.assertIn("RF-TODO2", resp.text)
        self.assertIn("还没叫停", resp.text)
        with SessionLocal() as s:
            groups = refund_service.refund_todo(s)
            keys = [g["key"] for g in groups]
            self.assertEqual(keys[0], "stopped")  # 先要做的排在前面
            self.assertIn("refunded", keys)

    def test_40_closed_order_is_terminal(self):
        """关闭完成后不能再改状态：要先「恢复正常订单」。"""
        order_id = self._refund_order("RF-CLOSED", transfer_status="unpaid")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            refund_service.set_refund_paid(s, order, True, actor="ops")
            refund_service.mark_stopped(s, order, actor="ops")
            refund_service.close_refund_order(s, order, actor="ops")
            ok, msg = order_state_service.check(order, ORDER_STATE_REFUND_REQUEST)
            self.assertFalse(ok)
            self.assertIn("关闭完成", msg)
            # 恢复正常订单后新字段一起清掉
            ok, _ = refund_service.restore_normal_order(s, order, actor="ops")
            self.assertTrue(ok)
            self.assertEqual(order.refund_amount, 0.0)
            self.assertEqual(order.return_tracking_no, "")
            self.assertFalse(order.is_received)
            self.assertEqual(order.supplier_refunded, "unpaid")
            self.assertIsNone(order.closed_at)


class NoPayAfterRefundTest(unittest.TestCase):
    """未付款的退单：忽略，且这单不要付款。"""

    def setUp(self) -> None:
        init_db()
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def test_10_refund_order_cannot_be_paid(self):
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no="RF-NOPAY",
                order_state=ORDER_STATE_REFUND_REQUEST,
                refund_status="pending",
                transfer_status="unpaid",
                msg_time=now(),
            )
            s.add(order)
            s.commit()
            order_id = order.id
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, reason = push_service.can_pay_supplier(order)
            self.assertFalse(ok)
            self.assertIn("不用再付款", reason)
            with self.assertRaises(ValueError):
                push_service.set_transfer(s, order, "paid")
        with SessionLocal() as s:
            self.assertEqual(s.get(Order, order_id).transfer_status, "unpaid")

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(Order).delete()
            s.commit()


class RefundDetailPageTest(unittest.TestCase):
    """订单详情页：推单状态话术、退单物流单号、关闭完成。"""

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

    def test_10_detail_shows_intercept_and_return_tracking(self):
        """已发货未收货 -> 页面写「同步物流拦截」；标记已收货后 -> 「退单物流单号」。"""
        order_id = self._order(
            "RF-PAGE-1",
            deal_amount=165.0,
            order_state=ORDER_STATE_REFUND_REQUEST,
            refund_status="pending",
            supplier_status="pushed",
            pushed_at=now(),
            tracking_no="SF100",
            transfer_status="paid",
        )
        page = self.client.get(f"/orders/{order_id}")
        self.assertIn("用户退单，同步物流拦截", page.text)
        self.assertIn("退单物流单号", page.text)
        self.assertIn("标记供应商已退款", page.text)

        self.client.post(
            f"/orders/{order_id}/refund/received", data={"received": "1"}, follow_redirects=True
        )
        self.client.post(
            f"/orders/{order_id}/refund/return-tracking",
            data={"return_tracking_no": "SF888"},
            follow_redirects=True,
        )
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertTrue(order.is_received)
            self.assertEqual(order.return_tracking_no, "SF888")
        page = self.client.get(f"/orders/{order_id}")
        self.assertIn("用户收货退单，同步退单物流单号", page.text)
        self.assertIn("SF888", page.text)

    def test_20_close_from_page(self):
        """未付款的退单：先标记已退款给客户，页面点「关闭完成」-> 订单落到关闭完成。"""
        order_id = self._order(
            "RF-PAGE-2",
            deal_amount=46.0,
            order_state=ORDER_STATE_REFUND_REQUEST,
            refund_status="pending",
            transfer_status="unpaid",
        )
        # 没退款给客户时页面不给关（按钮置灰，提交也会被拦）
        resp = self.client.post(
            f"/orders/{order_id}/refund/close", data={"note": ""}, follow_redirects=True
        )
        self.assertIn("还没退款给客户", resp.text)
        self.client.post(
            f"/orders/{order_id}/refund/paid", data={"paid": "1"}, follow_redirects=True
        )
        resp = self.client.post(
            f"/orders/{order_id}/refund/close", data={"note": ""}, follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        with SessionLocal() as s:
            self.assertEqual(s.get(Order, order_id).order_state, ORDER_STATE_CLOSED)

    def test_22_refund_process_one_form(self):
        """一次保存表单：理由 / 收货 / 退回单号 / 退款金额 / 是否退款 一起提交。"""
        order_id = self._order(
            "RF-PAGE-5",
            deal_amount=46.0,
            order_state=ORDER_STATE_REFUND_REQUEST,
            refund_status="pending",
            transfer_status="unpaid",
        )
        resp = self.client.post(
            f"/orders/{order_id}/refund/process",
            data={
                "reason": "客户拍错了",
                "refund_status": "pending",
                "received": "1",
                "return_tracking_no": "SF888",
                "refund_amount": "46",
                "refund_paid": "1",
                "refund_note": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.refund_reason, "客户拍错了")
            self.assertTrue(order.is_received)
            self.assertEqual(order.return_tracking_no, "SF888")
            # 符号口径（2026-10-03）：页面填 46（正数），订单上存 -46（钱出去）
            self.assertAlmostEqual(order.refund_amount, -46.0)
            self.assertAlmostEqual(order.refund_amount_abs, 46.0)
            self.assertEqual(order.refund_paid, "paid")

    def test_30_canceled_order_shows_no_push_needed(self):
        """撤单：推单状态写「用户撤单，无需推单」。"""
        order_id = self._order("RF-PAGE-3", is_recalled=True)
        page = self.client.get(f"/orders/{order_id}")
        self.assertIn("用户撤单，无需推单", page.text)

    def test_40_list_page_shows_refund_push_status(self):
        """订单列表：退单订单的推单状态列显示场景话术。"""
        order_id = self._order(
            "RF-PAGE-4",
            order_state=ORDER_STATE_REFUND_REQUEST,
            refund_status="pending",
            supplier_status="pushed",
            pushed_at=now(),
        )
        page = self.client.get("/orders")
        self.assertIn("用户退单，同步撤回发货", page.text)
        self.assertIn(f"/orders/{order_id}", page.text)


if __name__ == "__main__":
    unittest.main()
