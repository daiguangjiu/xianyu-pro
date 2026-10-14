"""订单处理日志 + 「标记退单 / 标记拦截」两个一键标记（2026-09-26 版本）。

业务口径：
  1 标记退单：用户**收到货之后**退回（退货退款）；
  2 标记拦截：供应商**已发货、用户退款但货还没收到**，把快递拦下退回；
  3 两个标记都让订单落到「已退单」、结算流水作废（这单不再结算给供应商），
    区别只在退单处理状态（已退款 / 已拦截）；已撤单的订单不允许再标记；
  4 凡是手动标记状态的动作（撤单、退单 / 拦截、推单、转账、改状态、回填单号），
    都要写一条订单处理日志，展示在订单详情页「推单信息」下面、「订单信息」上面。
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
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    ORDER_STATE_REFUNDED,
    ORDER_STATE_REFUND_REQUEST,
    ORDER_STATE_SHIPPED,
    SETTLEMENT_VOID,
    Order,
    OrderLog,
    RawMessage,
    Settlement,
)
from app.services import finance as finance_service  # noqa: E402
from app.services import recall as recall_service  # noqa: E402
from app.services import order_log as order_log_service  # noqa: E402
from app.services import order_state as order_state_service  # noqa: E402
from app.services import refund as refund_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class MarkManualTest(unittest.TestCase):
    """服务层：标记退单 / 标记拦截。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        self._clean()

    def tearDown(self) -> None:
        self._clean()

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(OrderLog).delete()
            s.query(Settlement).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def _order(self, order_no: str = "LOG-1", **kw) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="亚朵四代单枕头",
                receiver_name="贲",
                sale_amount=kw.pop("sale", 300.0),
                deal_amount=kw.pop("deal", 0.0),
                cost_amount=kw.pop("cost", 200.0),
                shipping_fee=kw.pop("shipping", 15.0),
                sender_name=kw.pop("sender", "小A"),
                supplier_name=kw.pop("supplier", "华强北数码"),
                ordered_at=kw.pop("day", datetime(2026, 9, 20)),
                **kw,
            )
            s.add(order)
            s.flush()
            finance_service.sync_order(s, order)
            s.commit()
            return order.id

    def _rows(self, order_id: int) -> dict[str, Settlement]:
        with SessionLocal() as s:
            rows = s.query(Settlement).filter(Settlement.order_id == order_id).all()
            return {r.kind: r for r in rows}

    def _logs(self, order_id: int) -> list[OrderLog]:
        with SessionLocal() as s:
            return list(
                s.scalars(
                    select(OrderLog)
                    .where(OrderLog.order_id == order_id)
                    .order_by(OrderLog.id)
                ).all()
            )

    # ---------------------------------------------------------------- 标记退单
    def test_01_mark_refund_falls_to_refunded_and_voids_settlement(self):
        """标记退单（用户收到货后退回）：订单→已退单，结算流水作废、不再结算给供应商。"""
        order_id = self._order("LOG-R1")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, msg = refund_service.mark_manual(
                s, order, refund_service.MARK_REFUNDED, actor="admin"
            )
        self.assertTrue(ok, msg)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.order_state, ORDER_STATE_REFUNDED)
            self.assertEqual(order.refund_status, "refunded")
            self.assertEqual(order.refund_by, "admin")
            self.assertIsNotNone(order.refund_handled_at)
        for row in self._rows(order_id).values():
            self.assertEqual(row.status, SETTLEMENT_VOID, "退单的订单不再结算")

    def test_02_mark_intercept_allows_shipped_order(self):
        """标记拦截：供应商已发货、用户退款但货没收到 —— 已发货订单也允许标记。"""
        order_id = self._order("LOG-I1", tracking_no="SF123456", shipped_at=datetime(2026, 9, 21))
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.order_state = ORDER_STATE_SHIPPED
            s.commit()
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertTrue(order_state_service.is_shipped(order))
            ok, msg = refund_service.mark_manual(
                s, order, refund_service.MARK_INTERCEPTED, actor="admin"
            )
        self.assertTrue(ok, msg)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.refund_status, "intercepted")
            self.assertEqual(order.order_state, ORDER_STATE_REFUNDED, "拦截成功 -> 已退单")
            self.assertEqual(order.tracking_no, "SF123456", "物流单号留着，便于查拦截件")
        for row in self._rows(order_id).values():
            self.assertEqual(row.status, SETTLEMENT_VOID)

    def test_03_recalled_order_cannot_be_marked(self):
        """已撤单的订单只留档：不允许再标记退单 / 拦截，字段也不该被改。"""
        order_id = self._order("LOG-C1", is_recalled=True, recalled_at=datetime(2026, 9, 22))
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, msg = refund_service.mark_manual(
                s, order, refund_service.MARK_REFUNDED, actor="admin"
            )
        self.assertFalse(ok)
        self.assertIn("已撤单", msg)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.refund_status, "")
            self.assertIsNone(order.refund_handled_at)

    def test_04_repeat_mark_is_idempotent(self):
        """重复标记同一状态：不再重复写日志，也不报错。"""
        order_id = self._order("LOG-R2")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            refund_service.mark_manual(s, order, refund_service.MARK_REFUNDED, actor="admin")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, msg = refund_service.mark_manual(
                s, order, refund_service.MARK_REFUNDED, actor="admin"
            )
        self.assertTrue(ok)
        self.assertIn("已经是", msg)
        actions = [row.action for row in self._logs(order_id)]
        self.assertEqual(actions.count(order_log_service.ACTION_REFUND), 1, "只留一条标记退单日志")

    def test_05_manual_mark_writes_order_log(self):
        """标记动作要留痕：动作、说明、操作人、时间。"""
        order_id = self._order("LOG-R3")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.refund_status = "pending"  # 群里先识别成「待拦截」，再被我方标记已退单
            s.commit()
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            refund_service.mark_manual(s, order, refund_service.MARK_REFUNDED, actor="运营小李")
        logs = self._logs(order_id)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].action, order_log_service.ACTION_REFUND)
        self.assertIn("待拦截 → 已退款", logs[0].detail)
        self.assertEqual(logs[0].operator, "运营小李")
        self.assertIsNotNone(logs[0].created_at)


class OrderLogPageTest(unittest.TestCase):
    """页面：两个一键标记按钮 + 订单处理日志区。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(OrderLog).delete()
            s.query(Settlement).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(OrderLog).delete()
            s.query(Settlement).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def _order(self, order_no: str = "PAGE-LOG", **kw) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="亚朵四代单枕头",
                receiver_name="贲",
                sale_amount=kw.pop("sale", 300.0),
                deal_amount=0.0,
                cost_amount=kw.pop("cost", 200.0),
                shipping_fee=15.0,
                sender_name="小A",
                supplier_name="华强北数码",
                ordered_at=datetime(2026, 9, 20),
                **kw,
            )
            s.add(order)
            s.flush()
            finance_service.sync_order(s, order)
            s.commit()
            return order.id

    def test_10_detail_page_has_mark_buttons(self):
        """详情页按钮区：标记撤回 / 标记退单（系统没识别到时的人工补救入口）。"""
        order_id = self._order()
        page = self.client.get(f"/orders/{order_id}")
        self.assertIn("标记退单", page.text)
        self.assertIn("标记撤回", page.text)
        self.assertIn(f"/orders/{order_id}/refund/mark", page.text)
        self.assertIn(f"/orders/{order_id}/recall", page.text)

    def test_10b_detail_page_has_restore_button(self):
        """被撤单 / 退单的订单：详情页给出「标记恢复正常订单」补救按钮。"""
        order_id = self._order("LOG-RESTORE")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            recall_service.mark_recalled(
                s, order, recaller="tester", reason="运营手工标记撤单"
            )
        page = self.client.get(f"/orders/{order_id}")
        self.assertIn("标记恢复正常订单", page.text)
        self.assertIn(f"/orders/{order_id}/restore", page.text)

    def test_11_buttons_mark_and_log_on_page(self):
        """点「标记退单」：与群里同步退单同一流程，落到买家申请退单，详情页出现处理日志。"""
        order_id = self._order()
        resp = self.client.post(f"/orders/{order_id}/refund/mark", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(
                s.get(Order, order_id).order_state, ORDER_STATE_REFUND_REQUEST
            )
        page = self.client.get(f"/orders/{order_id}")
        self.assertIn("订单处理日志", page.text)
        self.assertIn("标记退单", page.text)
        # 日志区位置：推单信息下面、订单信息上面
        html = page.text
        self.assertLess(html.index("推单信息（单笔推单）"), html.index("订单处理日志"))
        self.assertLess(html.index("订单处理日志"), html.index("订单信息"))

    def test_12_intercept_button_on_shipped_order(self):
        """已发货订单也能一键标记拦截（拦截就是针对已发货场景的）。"""
        order_id = self._order("PAGE-SHIP", tracking_no="SF987654")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.order_state = ORDER_STATE_SHIPPED
            s.commit()
        resp = self.client.post(f"/orders/{order_id}/refund/intercept", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.refund_status, "intercepted")
            self.assertEqual(order.order_state, ORDER_STATE_REFUNDED)
        page = self.client.get(f"/orders/{order_id}")
        self.assertIn("已拦截", page.text)
        self.assertIn("标记恢复正常订单", page.text)

    def test_13_edit_recall_and_transfer_write_logs(self):
        """撤单 / 撤销撤回 / 改转账 / 改订单状态都要写日志。"""
        order_id = self._order("PAGE-MIX")
        self.client.post(f"/orders/{order_id}/recall", follow_redirects=False)
        self.client.post(f"/orders/{order_id}/recall/undo", follow_redirects=False)
        self.client.post(
            f"/orders/{order_id}/push/transfer", data={"transfer_status": "paid"},
            follow_redirects=False,
        )
        self.client.post(
            f"/orders/{order_id}/edit",
            data={
                "order_no": "PAGE-MIX",
                "supplier_status": "pushed",
                "order_state": "pushed",
                "transfer_status": "paid",
            },
            follow_redirects=False,
        )
        with SessionLocal() as s:
            actions = [
                row.action
                for row in s.scalars(
                    select(OrderLog).where(OrderLog.order_id == order_id).order_by(OrderLog.id)
                ).all()
            ]
        self.assertIn(order_log_service.ACTION_RECALL, actions)
        self.assertIn(order_log_service.ACTION_RECALL_UNDO, actions)
        self.assertIn(order_log_service.ACTION_TRANSFER, actions)
        self.assertIn(order_log_service.ACTION_STATE, actions)
        # 每次动作都有操作人（admin）
        with SessionLocal() as s:
            operators = {
                row.operator
                for row in s.scalars(select(OrderLog).where(OrderLog.order_id == order_id)).all()
            }
        self.assertEqual(operators, {"admin"})

    def test_14_log_operator_defaults_to_system(self):
        """没记操作人的日志（自动流程）展示为「系统自动」。"""
        order_id = self._order("PAGE-SYS")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order_log_service.log(s, order, order_log_service.ACTION_STATE, detail="群里自动撤单")
            s.commit()
        page = self.client.get(f"/orders/{order_id}")
        self.assertIn("系统自动", page.text)


if __name__ == "__main__":
    unittest.main()
