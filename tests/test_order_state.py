"""订单状态（生命周期）：新建 / 已推单 / 已发货 / 已撤单 / 已退单。

业务口径（2026-09）：
  1 新建：飞书群同步订单、归档出订单记录即为新建；
  2 已推单：运营处理完转账等、把订单信息复制同步给上游（供应商）之后；
  3 已发货：上游返回物流单号、运营回填到订单之后；
  4 已撤单：还没发货前，卖家或运营把订单撤回 / 撤掉（只留档）；
  5 已退单：未发货时群里同步退单；**已发货的退单请求不受理**（让用户收到货后再退）。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    ORDER_STATE_CANCELED,
    ORDER_STATE_CHOICES,
    ORDER_STATE_LABELS,
    ORDER_STATE_NEW,
    ORDER_STATE_PUSHED,
    ORDER_STATE_REFUNDED,
    ORDER_STATE_REFUND_REQUEST,
    ORDER_STATE_REFUND_SYNCED,
    ORDER_STATE_SHIPPED,
    Order,
    RawMessage,
)
from app.services import order_state as order_state_service  # noqa: E402
from app.services import push as push_service  # noqa: E402
from app.services import refund as refund_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

ORDER_TEXT = (
    "【闲鱼订单】\n订单号：2026010199990001\n买家：小鱼儿\n商品：亚朵四代单枕头\n"
    "数量：1\n金额：¥115.00\n成本：60\n收货人：贲\n电话：14790021215\n"
    "地址：安徽省滁州市琅琊区清流街道\n供应商：华强北数码\n"
)


def _event(message_id: str, content: str) -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": f"evt-{message_id}",
            "event_type": "im.message.receive_v1",
            "create_time": str(int(time.time() * 1000)),
            "token": "",
            "app_id": "cli_test",
            "tenant_key": "t",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}, "sender_type": "user", "tenant_key": "t"},
            "message": {
                "message_id": message_id,
                "create_time": str(int(time.time() * 1000)),
                "chat_id": "oc_test",
                "chat_type": "group",
                "message_type": "text",
                "content": content,
            },
        },
    }


class OrderStateRulesTest(unittest.TestCase):
    """状态推导与流转校验（服务层）。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def _order(self, order_no: str = "STATE-1", **kw) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                order_state=kw.pop("order_state", ORDER_STATE_NEW),
                msg_time=now(),
                **kw,
            )
            s.add(order)
            s.commit()
            return order.id

    # ---------------------------------------------------------------- 推导
    def test_01_labels_cover_all_states(self):
        self.assertEqual(
            [ORDER_STATE_LABELS[s] for s in ORDER_STATE_CHOICES],
            [
                "新建",
                "已推单",
                "已发货",
                "已完成",  # 正向终态（2026-10-03）：已发货 + 签收 / 超时自动确认收货
                "买家申请退单",
                "已同步供应商退单",
                "已退款",
                "关闭完成",
                "已撤单",
            ],
        )

    def test_02_derive_by_facts(self):
        cases = [
            ({}, ORDER_STATE_NEW),
            ({"supplier_status": "pushed", "pushed_at": now()}, ORDER_STATE_PUSHED),
            ({"tracking_no": "SF123"}, ORDER_STATE_SHIPPED),
            ({"is_recalled": True}, ORDER_STATE_CANCELED),
            ({"refund_status": "pending"}, ORDER_STATE_REFUND_REQUEST),
        ]
        for kwargs, expected in cases:
            order_id = self._order(order_no=f"D-{expected}-{len(kwargs)}", **kwargs)
            with SessionLocal() as s:
                self.assertEqual(
                    order_state_service.derive(s.get(Order, order_id)),
                    expected,
                    f"{kwargs} 应推导为 {expected}",
                )

    def test_03_shipped_refund_request_keeps_shipped(self):
        """已发货无法拦截的退单：状态保持已发货（不同意退单）。"""
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no="D-SHIP-REFUND",
                order_state=ORDER_STATE_SHIPPED,
                tracking_no="SF123",
                refund_status="shipped",
                order_status="refund",
            )
            self.assertEqual(order_state_service.derive(order), ORDER_STATE_SHIPPED)
        # 用户退回、退款完成 -> 才算已退单
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no="D-SHIP-REFUNDED",
                order_state=ORDER_STATE_SHIPPED,
                tracking_no="SF123",
                refund_status="refunded",
                order_status="refund",
            )
            self.assertEqual(order_state_service.derive(order), ORDER_STATE_REFUNDED)

    # ---------------------------------------------------------------- 校验
    def test_04_shipped_order_cannot_cancel_but_can_refund(self):
        """已发货：不能撤单（走退单流程），但**退单要受理**（物流拦截 / 收货退单）。"""
        order_id = self._order("D-SHIPPED", order_state=ORDER_STATE_SHIPPED, tracking_no="SF001")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, reason = order_state_service.check(order, ORDER_STATE_CANCELED)
            self.assertFalse(ok, "已发货的订单不能撤单")
            self.assertIn("SF001", reason)
            # 2026-09-29 口径：已发货也受理退单（未收货同步物流拦截、已收货走退货退款）
            for target in (
                ORDER_STATE_REFUND_REQUEST,
                ORDER_STATE_REFUND_SYNCED,
                ORDER_STATE_REFUNDED,
            ):
                ok, reason = order_state_service.check(order, target)
                self.assertTrue(ok, f"已发货的订单要能受理退单（{target}）：{reason}")

    def test_05_terminal_states_cannot_be_pushed(self):
        for state in (ORDER_STATE_CANCELED, ORDER_STATE_REFUNDED):
            order_id = self._order(f"D-{state}", order_state=state)
            with SessionLocal() as s:
                ok, reason = order_state_service.check(s.get(Order, order_id), ORDER_STATE_PUSHED)
                self.assertFalse(ok)
                self.assertIn("不能再推单", reason)

    def test_06_shipped_requires_tracking_no(self):
        order_id = self._order("D-NO-TRACK", order_state=ORDER_STATE_PUSHED, pushed_at=now())
        with SessionLocal() as s:
            ok, reason = order_state_service.check(s.get(Order, order_id), ORDER_STATE_SHIPPED)
            self.assertFalse(ok)
            self.assertIn("物流单号", reason)

    def test_07_new_cannot_be_set_manually(self):
        order_id = self._order("D-NEW", order_state=ORDER_STATE_PUSHED, pushed_at=now())
        with SessionLocal() as s:
            ok, _ = order_state_service.check(s.get(Order, order_id), ORDER_STATE_NEW)
            self.assertFalse(ok, "新建是初始状态，不能手工改回")

    # ---------------------------------------------------------------- 落库
    def test_08_set_state_writes_related_fields(self):
        order_id = self._order("D-FLOW")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, _ = order_state_service.set_state(
                s, order, ORDER_STATE_PUSHED, actor="tester"
            )
            self.assertTrue(ok)
            self.assertEqual(order.order_state, ORDER_STATE_PUSHED)
            self.assertEqual(order.supplier_status, "pushed")
            self.assertIsNotNone(order.pushed_at)

            order.tracking_no = "SF888"
            ok, _ = order_state_service.set_state(s, order, ORDER_STATE_SHIPPED)
            self.assertTrue(ok)
            self.assertEqual(order.order_state, ORDER_STATE_SHIPPED)
            self.assertIsNotNone(order.shipped_at)

        # 未发货的订单可以撤单（撤单后会带上撤回标记，只留档）
        plain_id = self._order("D-CANCEL")
        with SessionLocal() as s:
            order = s.get(Order, plain_id)
            ok, reason = order_state_service.set_state(s, order, ORDER_STATE_CANCELED)
            self.assertTrue(ok, reason)
            self.assertTrue(order.is_recalled)
            self.assertEqual(order.order_state, ORDER_STATE_CANCELED)

    def test_09_set_state_refunded_marks_refund(self):
        order_id = self._order("D-REFUND")
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, _ = order_state_service.set_state(s, order, ORDER_STATE_REFUNDED)
            self.assertTrue(ok)
            self.assertEqual(order.order_state, ORDER_STATE_REFUNDED)
            self.assertEqual(order.refund_status, "pending")
            self.assertIsNotNone(order.refund_at)

    def test_10_backfill_old_orders(self):
        """老订单（新增列后默认是新建）按当前事实重算状态。"""
        self._order("BF-1")
        self._order("BF-2", supplier_status="pushed", pushed_at=now())
        self._order("BF-3", tracking_no="SF999")
        with SessionLocal() as s:
            changed = order_state_service.backfill(s)
            self.assertGreaterEqual(changed, 2)
            states = {
                o.order_no: o.order_state
                for o in s.query(Order).filter(Order.order_no.like("BF-%")).all()
            }
        self.assertEqual(states["BF-1"], ORDER_STATE_NEW)
        self.assertEqual(states["BF-2"], ORDER_STATE_PUSHED)
        self.assertEqual(states["BF-3"], ORDER_STATE_SHIPPED)


class OrderStateFlowTest(unittest.TestCase):
    """页面 / 接口上的状态流转（端到端）。"""

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

    def _order(self, order_no: str = "FLOW-1", **kw) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="亚朵四代单枕头",
                receiver_name="贲",
                receiver_phone="14790021215",
                cost_amount=60.0,
                supplier_name="华强北数码",
                order_state=kw.pop("order_state", ORDER_STATE_NEW),
                msg_time=now(),
                **kw,
            )
            s.add(order)
            s.commit()
            return order.id

    def _state(self, order_id: int) -> str:
        with SessionLocal() as s:
            return s.get(Order, order_id).order_state

    # ---------------------------------------------------------------- 新建
    def test_11_group_sync_creates_new_order(self):
        resp = self.client.post("/feishu/event", json=_event("om_state_1", '{"text": "%s"}' % ORDER_TEXT.replace("\n", "\\n")))
        self.assertTrue(resp.json().get("ok"), resp.json())
        with SessionLocal() as s:
            order = s.query(Order).filter(Order.order_no == "2026010199990001").one()
            self.assertEqual(order.order_state, ORDER_STATE_NEW, "群里同步归档出来就是新建")

    # ---------------------------------------------------------------- 已推单
    def test_12_copy_push_marks_pushed(self):
        order_id = self._order("FLOW-PUSH")
        resp = self.client.post(
            f"/orders/{order_id}/push", data={"transfer_status": "paid"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self._state(order_id), ORDER_STATE_PUSHED)
        page = self.client.get(f"/orders/{order_id}")
        self.assertIn("已推单", page.text)

    # ---------------------------------------------------------------- 已发货
    def test_13_tracking_no_marks_shipped(self):
        order_id = self._order("FLOW-SHIP", supplier_status="pushed", pushed_at=now())
        resp = self.client.post(
            f"/batches/tracking/{order_id}",
            data={"tracking_no": "SF20260925", "sync": "0", "back": "/batches"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self._state(order_id), ORDER_STATE_SHIPPED)
        with SessionLocal() as s:
            self.assertIsNotNone(s.get(Order, order_id).shipped_at)

    def test_14_clearing_tracking_falls_back(self):
        """清空物流单号后，状态按事实回退到已推单（不再是已发货）。"""
        order_id = self._order(
            "FLOW-SHIP-CLEAR",
            order_state=ORDER_STATE_SHIPPED,
            supplier_status="pushed",
            pushed_at=now(),
            tracking_no="SF001",
        )
        self.client.post(
            f"/batches/tracking/{order_id}",
            data={"tracking_no": "", "sync": "0", "back": "/batches"},
            follow_redirects=False,
        )
        self.assertEqual(self._state(order_id), ORDER_STATE_PUSHED)

    # ---------------------------------------------------------------- 已撤单
    def test_15_recall_before_shipping_cancels_order(self):
        order_id = self._order("FLOW-CANCEL")
        resp = self.client.post(f"/orders/{order_id}/recall", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self._state(order_id), ORDER_STATE_CANCELED)

    def test_16_recall_after_shipping_keeps_shipped(self):
        order_id = self._order(
            "FLOW-CANCEL-SHIPPED",
            order_state=ORDER_STATE_SHIPPED,
            tracking_no="SF002",
            supplier_status="pushed",
            pushed_at=now(),
        )
        self.client.post(f"/orders/{order_id}/recall", follow_redirects=False)
        self.assertEqual(self._state(order_id), ORDER_STATE_SHIPPED, "已发货的不能撤单")

    def test_17_undo_recall_restores_state(self):
        order_id = self._order("FLOW-UNDO", supplier_status="pushed", pushed_at=now())
        self.client.post(f"/orders/{order_id}/recall", follow_redirects=False)
        self.assertEqual(self._state(order_id), ORDER_STATE_CANCELED)
        self.client.post(f"/orders/{order_id}/recall/undo", follow_redirects=False)
        self.assertEqual(self._state(order_id), ORDER_STATE_PUSHED)

    # ---------------------------------------------------------------- 已退单
    def test_18_refund_before_shipping_marks_refunded(self):
        order_id = self._order("FLOW-REFUND")
        resp = self.client.post(
            f"/orders/{order_id}/refund",
            data={"refund_status": "pending", "refund_note": "联系供应商拦截"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self._state(order_id), ORDER_STATE_REFUNDED)

    def test_19_refund_after_shipping_is_accepted(self):
        """已发货要求退单：**受理**（2026-09-29 口径）—— 未收货同步物流拦截、已收货走退货退款。"""
        order_id = self._order(
            "FLOW-REFUND-SHIPPED",
            order_state=ORDER_STATE_SHIPPED,
            tracking_no="SF003",
            supplier_status="pushed",
            pushed_at=now(),
            deal_amount=199.0,
        )
        resp = self.client.post(
            f"/orders/{order_id}/refund",
            data={"refund_status": "pending"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        # 受理：落到「买家申请退单」，等运营同步供应商
        self.assertEqual(self._state(order_id), ORDER_STATE_REFUND_REQUEST)
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(order.refund_status, "pending")
            # 推单状态：已发货、没收到货 -> 同步物流拦截
            self.assertEqual(
                push_service.refund_push_status(order), push_service.PUSH_STATUS_INTERCEPT
            )

    def test_20_group_refund_message_accepted_when_shipped(self):
        """群里同步的退单：已发货的同样受理（推单状态 = 用户退单，同步物流拦截）。"""
        order_id = self._order(
            "FLOW-GROUP-REFUND",
            order_state=ORDER_STATE_SHIPPED,
            tracking_no="SF004",
            supplier_status="pushed",
            pushed_at=now(),
            deal_amount=88.0,
        )
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            _, accepted = refund_service.mark_refund(s, order, None, "此单退")
            self.assertTrue(accepted, "已发货的退单请求也要受理")
            self.assertEqual(order.order_state, ORDER_STATE_REFUND_REQUEST)
            self.assertEqual(order.refund_status, "pending")
            # 符号口径（2026-10-03）：退款金额按成交价记，存负值（钱出去）
            self.assertEqual(order.refund_amount, -88.0, "退款金额按成交价记（存负值）")

    def test_21_group_refund_message_accepted_when_not_shipped(self):
        order_id = self._order("FLOW-GROUP-REFUND-OK", supplier_status="pushed", pushed_at=now())
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            _, accepted = refund_service.mark_refund(s, order, None, "此单退")
            self.assertTrue(accepted)
            self.assertEqual(order.order_state, ORDER_STATE_REFUND_REQUEST)

    def test_21b_refund_reason_extracted_and_supplier_synced(self):
        """退单流程：解析理由 -> 买家申请退单 -> 同步供应商 -> 已同步供应商退单。"""
        order_id = self._order(
            "FLOW-REFUND-REASON", supplier_status="pushed", pushed_at=now()
        )
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            reason = refund_service.extract_reason("此单退\n退单理由：客户拍错了", "此单退")
            self.assertEqual(reason, "客户拍错了", "要能从消息里解析出退单理由")
            _, accepted = refund_service.mark_refund(
                s, order, None, "此单退", reason=reason
            )
            self.assertTrue(accepted)
            self.assertEqual(order.refund_reason, "客户拍错了")
            self.assertEqual(order.order_state, ORDER_STATE_REFUND_REQUEST)
            ok, msg = refund_service.mark_supplier_synced(s, order, actor="tester")
            self.assertTrue(ok, msg)
            self.assertEqual(order.order_state, ORDER_STATE_REFUND_SYNCED)

    def test_21c_refund_without_reason_needs_review(self):
        """没写退单理由的退单：挂「待确认」催补，补上后自动消掉。"""
        order_id = self._order(
            "FLOW-REFUND-NOREASON", supplier_status="pushed", pushed_at=now()
        )
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            self.assertEqual(refund_service.extract_reason("此单退", "此单退"), "")
            _, accepted = refund_service.mark_refund(s, order, None, "此单退")
            self.assertTrue(accepted)
            self.assertTrue(order.needs_review, "缺退单理由要挂待确认")
            self.assertIn("缺退单理由", order.review_reason)
            ok, msg = refund_service.set_refund_reason(
                s, order, "客户不想要了", actor="tester"
            )
            self.assertTrue(ok, msg)
            self.assertFalse(order.needs_review, "理由补齐后待确认消掉")

    # ---------------------------------------------------------------- 页面
    def test_22_manual_state_change_is_validated(self):
        order_id = self._order(
            "FLOW-MANUAL",
            order_state=ORDER_STATE_SHIPPED,
            tracking_no="SF005",
            supplier_status="pushed",
            pushed_at=now(),
        )
        resp = self.client.post(
            f"/orders/{order_id}/edit",
            data={"order_state": ORDER_STATE_REFUNDED},
            follow_redirects=True,
        )
        # 已发货也受理退单：手工改状态到「已退款」是允许的
        self.assertEqual(self._state(order_id), ORDER_STATE_REFUNDED)

    def test_23_orders_page_filters_and_shows_state(self):
        shipped = self._order(
            "FLOW-LIST-SHIPPED", order_state=ORDER_STATE_SHIPPED, tracking_no="SF006"
        )
        self._order("FLOW-LIST-NEW")
        page = self.client.get("/orders?state=shipped")
        self.assertEqual(page.status_code, 200)
        self.assertIn("FLOW-LIST-SHIPPED", page.text)
        self.assertNotIn("FLOW-LIST-NEW", page.text, "新建的订单不在已发货筛选里")
        self.assertIn("已发货", page.text)
        self.assertIn(f"/orders/{shipped}", page.text)

    def test_25_edit_form_tracking_marks_shipped(self):
        """编辑订单里回填物流单号（状态下拉没动）-> 自动变已发货。"""
        order_id = self._order("FLOW-EDIT-TRACK", supplier_status="pushed", pushed_at=now())
        resp = self.client.post(
            f"/orders/{order_id}/edit",
            data={"order_state": ORDER_STATE_PUSHED, "tracking_no": "SF777"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._state(order_id), ORDER_STATE_SHIPPED)
        self.assertIn("已发货", resp.text)

    def test_24_dashboard_shows_five_states(self):
        self._order("FLOW-DASH")
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        for value in ORDER_STATE_CHOICES:
            self.assertIn(ORDER_STATE_LABELS[value], page.text, f"首页要展示「{value}」")
            self.assertIn(f"/orders?state={value}", page.text)


if __name__ == "__main__":
    unittest.main()
