"""回执推送：同一订单不重复下发 + 回执前把临时订单号补齐。

线上实例（2026-10-14）：群里「一条模板 + 多张截图」会被拆成多条消息逐条重跑归档，
每条各回执一份 —— 同一单在群里连刷 2~3 条，且回执里「订单号」那一行还整行消失
（那一刻订单号还是临时编号 TMP-，模板里被判空），看着就像「还是临时单号」。

本文件的两条口径：
1. 同一订单的「归档回执」只发一次（退单 / 撤单 / 重复单提醒不受此限）；
2. 发回执前，若订单号仍是临时编号，用已归档的截图再认一次正式号再渲染文案。
"""
from __future__ import annotations

import unittest
from unittest import mock

from app.config import settings
from app.db import SessionLocal, init_db
from app.models import Order, RawMessage
from app.services import notify


def _rand() -> str:
    import uuid

    return uuid.uuid4().hex[:8]


def _clean(case, message_ids: tuple[str, ...]) -> None:
    """清掉测试数据：用全新会话 + expire，避免原会话里的对象在行被删后 flush 报
    StaleDataError（notify_processed 会改 msg 字段，删行再 commit 就会冲突）。"""
    case.session.rollback()
    case.session.close()
    with SessionLocal() as s:
        s.query(RawMessage).filter(RawMessage.message_id.in_(message_ids)).delete(
            synchronize_session=False
        )
        oid = getattr(case, "order_id", 0) or 0
        if oid:
            s.query(Order).filter(Order.id == oid).delete(synchronize_session=False)
        s.commit()
    _restore_settings(case)


def _restore_settings(case) -> None:
    settings.notify_mode = case._mode
    settings.notify_min_interval_seconds = case._interval
    settings.notify_max_per_hour = case._max_per_hour
    settings.notify_active_hours = case._active
    settings.notify_receipt_template = case._template


class FakeClient:
    """只记录发出去的文本，不打飞书接口。"""

    def __init__(self) -> None:
        self.enabled = True  # 走完整下发链路，只是不打真实接口
        self.replies: list[str] = []

    def reply_text(self, message_id: str, text: str) -> dict:
        self.replies.append(text)
        return {"message_id": "fake_reply"}

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        self.replies.append(text)
        return {"message_id": "fake_direct"}


class ReceiptDedupeTest(unittest.TestCase):
    """同一订单只回执一次。"""

    def setUp(self) -> None:
        init_db()
        self._mode = settings.notify_mode
        self._interval = settings.notify_min_interval_seconds
        self._max_per_hour = settings.notify_max_per_hour
        self._active = settings.notify_active_hours
        self._template = settings.notify_receipt_template
        settings.notify_mode = "reply"
        settings.notify_min_interval_seconds = 0  # 关掉间隔保护，单独验证订单级去重
        settings.notify_max_per_hour = 0
        settings.notify_active_hours = ""
        settings.notify_receipt_template = ""
        self.session = SessionLocal()
        order = Order(order_no="SN-DEDUPE-1-%s" % _rand(), product_name="小天才手表", deal_amount=550.0)
        self.session.add(order)
        self.session.commit()
        self.order = order
        self.order_id = order.id
        # 第一条消息：走完整流程发一次回执
        self.msg1 = RawMessage(
            message_id="om_dedupe_1",
            chat_id="oc_dedupe",
            chat_name="订单群",
            sender_id="ou_sender",
            sender_name="周玉萍",
            raw_text="订单编号：SN-DEDUPE-1",
            order_id=order.id,
        )
        self.session.add(self.msg1)
        self.session.commit()

    def tearDown(self) -> None:
        _clean(self, tuple(set(("om_dedupe_1",) + tuple(getattr(self, "msg_map", [])))))
    def _first_broadcast(self) -> None:
        """第一条回执发完要落库：notify_kind 由 notify 写，notify_status 由调用方回填
        （线上是 ingest 里 `msg.notify_status = status`），去重查的是库里的记录。"""
        status, err = notify.notify_processed(
            self.msg1, self.order, FakeClient(), session=self.session
        )
        self.assertEqual(status, "sent", err)
        self.msg1.notify_status = status
        self.session.commit()
        self.assertEqual(self.msg1.notify_kind, "receipt")

    def _notify(self, message_id: str) -> tuple[str, str]:
        msg = RawMessage(
            message_id=message_id,
            chat_id="oc_dedupe",
            chat_name="订单群",
            sender_id="ou_sender",
            sender_name="周玉萍",
            raw_text="订单截图",
            order_id=self.order.id,
        )
        self.session.add(msg)
        self.session.commit()
        status, err = notify.notify_processed(
            msg, self.order, FakeClient(), session=self.session
        )
        self.msg_map = getattr(self, "msg_map", [])
        self.msg_map.append(msg.message_id)
        return status, err

    def test_10_first_receipt_is_sent(self):
        client = FakeClient()
        status, err = notify.notify_processed(self.msg1, self.order, client, session=self.session)
        self.assertEqual(status, "sent", err)
        self.assertTrue(client.replies)
        self.assertIn("SN-DEDUPE-1", client.replies[0])

    def test_20_same_order_second_receipt_skipped(self):
        self._first_broadcast()
        status, err = self._notify("om_dedupe_2")
        self.assertEqual(status, "skipped")
        self.assertIn("已下发过", err)

    def test_30_third_receipt_also_skipped(self):
        self._first_broadcast()
        self.assertEqual(self._notify("om_dedupe_2")[0], "skipped")
        self.assertEqual(self._notify("om_dedupe_3")[0], "skipped")

    def test_40_other_order_still_sends(self):
        """换一单照样能发，别把别人的回执也挡了。"""
        self._first_broadcast()
        other = Order(order_no="SN-DEDUPE-2-%s" % _rand(), product_name="冲锋衣", deal_amount=174.0)
        self.session.add(other)
        self.session.commit()
        msg = RawMessage(
            message_id="om_dedupe_3",
            chat_id="oc_dedupe",
            chat_name="订单群",
            sender_id="ou_sender",
            raw_text="订单截图",
            order_id=other.id,
        )
        self.session.add(msg)
        self.session.commit()
        client = FakeClient()
        status, err = notify.notify_processed(msg, other, client, session=self.session)
        self.assertEqual(status, "sent", err)
        self.assertTrue(client.replies)
        self.assertIn("SN-DEDUPE-2", client.replies[0])
        self.session.delete(other)
        self.session.commit()

    def test_50_refund_receipt_not_deduped(self):
        """退单 / 撤单是要人马上去拦截的，不受「同订单回执去重」限制。"""
        self._first_broadcast()
        msg2 = RawMessage(
            message_id="om_dedupe_2",
            chat_id="oc_dedupe",
            chat_name="订单群",
            sender_id="ou_sender",
            raw_text="此单退",
            order_id=self.order.id,
        )
        self.session.add(msg2)
        self.session.commit()
        client = FakeClient()
        status, err = notify._send_receipt(
            msg2, self.order, client, self.session, "退单回执", kind="refund"
        )
        self.assertEqual(status, "sent", err)


class ReceiptOrderNoUpgradeTest(unittest.TestCase):
    """回执前补认订单号：截图配上后不该再缺订单号那一栏。"""

    def setUp(self) -> None:
        init_db()
        self._mode = settings.notify_mode
        self._interval = settings.notify_min_interval_seconds
        self._max_per_hour = settings.notify_max_per_hour
        self._active = settings.notify_active_hours
        self._template = settings.notify_receipt_template
        settings.notify_mode = "reply"
        settings.notify_min_interval_seconds = 0
        settings.notify_max_per_hour = 0
        settings.notify_active_hours = ""
        settings.notify_receipt_template = "订单号 {{ 订单号 }} | {{ 产品 }}"
        self.session = SessionLocal()
        order = Order(order_no="TMP-%s" % _rand(), product_name="小天才Z6", deal_amount=550.0)
        self.session.add(order)
        self.session.commit()
        self.order = order
        self.order_id = order.id
        self.msg = RawMessage(
            message_id="om_upgrade_1",
            chat_id="oc_upgrade",
            chat_name="订单群",
            sender_id="ou_sender",
            sender_name="周玉萍",
            raw_text="订单截图",
            order_id=order.id,
        )
        self.session.add(self.msg)
        self.session.commit()

    def tearDown(self) -> None:
        _clean(self, ("om_upgrade_1",))
    def test_10_temp_no_is_upgraded_before_receipt(self):
        """订单号还是临时编号时，先用截图再认一次，回执里带上正式单号。"""

        def fake_reocr(session, order, limit_images=3):
            order.order_no = "3317116440169000158"
            return True, "已补齐正式订单号"

        with mock.patch("app.services.ingest.reocr_order", fake_reocr):
            client = FakeClient()
            status, err = notify.notify_processed(
                self.msg, self.order, client, session=self.session
            )
        self.assertEqual(status, "sent", err)
        self.assertEqual(self.order.order_no, "3317116440169000158")
        self.assertTrue(client.replies)
        self.assertIn("3317116440169000158", client.replies[0])
        self.assertNotIn("TMP-", client.replies[0])

    def test_20_formal_no_skips_reocr(self):
        """已经是正式单号就不必再重识别一遍（省一次 OCR）。"""
        self.order.order_no = "3316488855167078379"
        self.session.commit()
        called = []

        def fake_reocr(session, order, limit_images=3):
            called.append(order.id)
            return False, "无需处理"

        with mock.patch("app.services.ingest.reocr_order", fake_reocr):
            notify.notify_processed(self.msg, self.order, FakeClient(), session=self.session)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
