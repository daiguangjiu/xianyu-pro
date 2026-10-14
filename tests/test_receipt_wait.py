"""回执文案与「等截图补全订单号」的延时推送。

业务口径（2026-09）：
  1 模板消息刚归档时订单号还是 TMP- 临时号（截图还没 OCR 完）：
    先压住回执，等截图把正式订单号补上再推，群里不会看到临时编号；
  2 等待窗口（NOTIFY_WAIT_SCREENSHOT_SECONDS，默认 60 秒）过后仍没截图：
    由补发线程发出「不带临时单号」的回执，保证不漏回执；
  3 回执按「订单信息 / 金额 / 收货与供应商 / 待确认与链接」分组，组间空行，便于阅读。
"""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Order, PushBatchItem, RawMessage, VoiceEvent  # noqa: E402
from app.services import notify as notify_service  # noqa: E402
from app.timeutil import now  # noqa: E402


class FakeClient:
    enabled = True

    def __init__(self):
        self.replies: list[str] = []
        self.sent: list[str] = []

    def reply_text(self, message_id: str, text: str) -> dict:
        self.replies.append(text)
        return {"ok": True}

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        self.sent.append(text)
        return {"ok": True}


class ReceiptFormatTest(unittest.TestCase):
    """回执文案：分组空行、TMP- 临时单号隐藏、金额同排。"""

    def test_receipt_grouped_and_readable(self):
        msg = RawMessage(message_id="rm_1", sender_id="ou_person")
        order = Order(
            id=9,
            order_no="512729881514602021",
            product_name="10000毫安 30w q2电 自带线（双C口）",
            note="白色",
            sale_amount=45.0,
            cost_amount=40.0,
            receiver_name="史",
            receiver_phone="15139922648",
            supplier_name="安徽梦想电商（BH001）",
            needs_review=True,
        )
        text = notify_service.build_receipt(msg, order)
        lines = text.splitlines()

        # 群内回复会带上 @ 发送人，这里只校验正文
        self.assertTrue(lines[0].endswith("✅ 已收到并处理，订单已归档"))
        self.assertEqual(lines[1], "", "头部与订单信息之间要留空行")
        self.assertIn("订单号：512729881514602021", text)
        self.assertIn("产品：10000毫安 30w q2电 自带线（双C口）", text)
        self.assertIn("备注：白色", text)
        self.assertIn("金额：售价 ¥45.00 ｜ 成本 ¥40.00", text)
        self.assertIn("收货：史 151****2648", text)
        self.assertIn("供应商：安徽梦想电商（BH001）", text)
        self.assertIn("⚠️ 部分字段待人工确认，请到系统补录", text)
        # 分组之间有空行，手机上更易读
        self.assertGreaterEqual(text.count("\n\n"), 3)

    def test_tmp_order_no_hidden(self):
        msg = RawMessage(message_id="rm_2", sender_id="ou_person")
        order = Order(id=10, order_no="TMP-20260925133808-AB584E", product_name="枕头")
        text = notify_service.build_receipt(msg, order)
        self.assertNotIn("TMP-", text, "临时订单号不能发给群里")


class WaitScreenshotTest(unittest.TestCase):
    """等截图补全订单号：窗口内压住，窗口过后补发（不显示临时号）。"""

    def setUp(self) -> None:
        init_db()
        with SessionLocal() as s:
            s.query(PushBatchItem).delete()
            s.query(RawMessage).delete()
            s.query(VoiceEvent).delete()
            s.query(Order).delete()
            s.commit()
        self._snapshot = {
            "mode": settings.notify_mode,
            "wait": settings.notify_wait_screenshot_seconds,
            "hours": settings.notify_active_hours,
            "interval": settings.notify_min_interval_seconds,
        }

    def tearDown(self) -> None:
        settings.notify_mode = self._snapshot["mode"]
        settings.notify_wait_screenshot_seconds = self._snapshot["wait"]
        settings.notify_active_hours = self._snapshot["hours"]
        settings.notify_min_interval_seconds = self._snapshot["interval"]

    def _seed(self, order_no: str, seconds_ago: int = 5) -> tuple[int, int]:
        """建一条待回执的消息 + 订单，返回 (msg_id, order_id)。"""
        with SessionLocal() as s:
            order = Order(platform="xianyu", order_no=order_no, product_name="枕头", sale_amount=115.0)
            s.add(order)
            s.flush()
            msg = RawMessage(
                message_id="wm_1",
                chat_id="oc_wait",
                msg_type="text",
                sender_id="ou_person",
                raw_text="产品：枕头",
                parse_status="parsed",
                msg_time=now() - timedelta(seconds=seconds_ago),
                order_id=order.id,
            )
            s.add(msg)
            s.commit()
            return msg.id, order.id

    def test_hold_within_window_then_send_after(self):
        settings.notify_mode = "reply"
        settings.notify_wait_screenshot_seconds = 60
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        _, order_id = self._seed("TMP-20260925133808-AB584E", seconds_ago=5)

        fake = FakeClient()
        with SessionLocal() as s:
            msg = s.get(RawMessage, s.scalar(select_msg_id()))
            order = s.get(Order, order_id)
            status, reason = notify_service.notify_processed(msg, order, fake, session=s)
            self.assertEqual(status, "held")
            self.assertIn("截图", reason)
            msg.notify_status = status
            msg.notify_error = reason
            s.commit()
        self.assertEqual(fake.replies, [], "窗口内不发回执")

        # 补发线程在窗口内也不发
        with SessionLocal() as s:
            out = notify_service.flush_held_notifications(s, client=fake)
        self.assertEqual(out["sent"], 0)
        self.assertEqual(out["waiting"], 1)
        self.assertEqual(fake.replies, [])

        # 窗口过后：补发出去，且不显示临时订单号
        with SessionLocal() as s:
            msg = s.get(RawMessage, s.scalar(select_msg_id()))
            msg.msg_time = now() - timedelta(seconds=120)
            s.commit()
        with SessionLocal() as s:
            out = notify_service.flush_held_notifications(s, client=fake)
        self.assertEqual(out["sent"], 1)
        self.assertEqual(len(fake.replies), 1)
        self.assertNotIn("TMP-", fake.replies[0])
        with SessionLocal() as s:
            msg = s.get(RawMessage, s.scalar(select_msg_id()))
            self.assertEqual(msg.notify_status, "sent")

    def test_send_immediately_once_order_no_filled(self):
        """截图解析完成（正式订单号已补全）：立即回执正确信息。"""
        settings.notify_mode = "reply"
        settings.notify_wait_screenshot_seconds = 60
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        _, order_id = self._seed("TMP-20260925133808-AB584E", seconds_ago=5)

        # 模拟截图 OCR 补全正式订单号
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            order.order_no = "512729881514602021"
            s.commit()

        fake = FakeClient()
        with SessionLocal() as s:
            msg = s.get(RawMessage, s.scalar(select_msg_id()))
            order = s.get(Order, order_id)
            status, _ = notify_service.notify_processed(msg, order, fake, session=s)
        self.assertEqual(status, "sent")
        self.assertEqual(len(fake.replies), 1)
        self.assertIn("512729881514602021", fake.replies[0])


class HeldReceiptKeepsKindTest(unittest.TestCase):
    """挂起的回执补发时不能改口径。

    线上实例（2026-09-27 11:35）：群里发「此单退」，系统正确识别并对订单打了退单标记，
    但退单回执因为订单号还是 TMP- 被挂起，89 秒后由补发线程发出时被统一按「订单已归档」
    重新渲染 —— 群里收到的是「✅ 你的订单已收到，系统已经处理订单归档」，
    看着完全不像退单。这里锁死修复后的行为。
    """

    def setUp(self) -> None:
        init_db()
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        self._saved = {
            key: getattr(settings, key)
            for key in (
                "notify_mode",
                "notify_wait_screenshot_seconds",
                "notify_active_hours",
                "notify_quiet_action",
                "notify_min_interval_seconds",
                "notify_max_per_hour",
                "refund_notify",
                "cancel_notify",
                "notify_digest",
            )
        }

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            setattr(settings, key, value)

    def _seed(
        self,
        order_no: str,
        intent: str = "",
        notify_kind: str = "",
        seconds_ago: int = 5,
    ) -> tuple[int, int]:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="苹果磁吸充电宝",
                sale_amount=46.0,
                cost_amount=38.0,
                refund_status="pending",
                refund_reason="客户拍错了",
            )
            s.add(order)
            s.flush()
            msg = RawMessage(
                message_id="wm_refund",
                chat_id="oc_refund_group",
                msg_type="text",
                sender_id="ou_person",
                raw_text="此单退\n产品名称：苹果磁吸充电宝",
                parse_status="parsed",
                kind="intent",
                intent=intent,
                notify_kind=notify_kind,
                msg_time=now() - timedelta(seconds=seconds_ago),
                order_id=order.id,
            )
            s.add(msg)
            s.commit()
            return msg.id, order.id

    def _get(self, session) -> tuple[RawMessage, Order]:
        msg = session.scalar(select(RawMessage).where(RawMessage.message_id == "wm_refund"))
        return msg, session.get(Order, msg.order_id)

    def test_refund_receipt_not_held_even_when_order_no_is_temp(self):
        """退单回执不该因为「订单号还是临时编号」被压住：它是要人来拦截的关键提醒。"""
        settings.notify_mode = "reply"
        settings.notify_wait_screenshot_seconds = 60
        self._seed("TMP-20260927110052-61CAAE", intent="refund")
        fake = FakeClient()
        with SessionLocal() as s:
            msg, order = self._get(s)
            status, reason = notify_service.notify_refund(
                msg, order, fake, session=s, keyword="此单退"
            )
            self.assertEqual(status, "sent", reason)
            self.assertEqual(msg.notify_kind, "refund", "回执类型要记下来供补发使用")
        self.assertEqual(len(fake.replies), 1)
        self.assertIn("退单", fake.replies[0])
        self.assertNotIn("订单已收到", fake.replies[0])
        self.assertNotIn("订单已归档", fake.replies[0])

    def test_held_refund_rebuilt_as_refund_not_receipt(self):
        """被限流挂起的退单回执：补发出来必须还是退单口径。"""
        settings.notify_mode = "reply"
        settings.notify_wait_screenshot_seconds = 0
        settings.notify_max_per_hour = 1  # 先把每小时下发上限占满，逼出 held
        with SessionLocal() as s:
            s.add(
                RawMessage(
                    message_id="wm_sent",
                    chat_id="oc_refund_group",
                    notify_status="sent",
                    notified_at=now(),
                )
            )
            s.commit()
        self._seed("3316902457144020685", intent="refund")
        fake = FakeClient()
        with SessionLocal() as s:
            msg, order = self._get(s)
            status, reason = notify_service.notify_refund(
                msg, order, fake, session=s, keyword="此单退"
            )
            self.assertEqual(status, "held", reason)
            self.assertEqual(msg.notify_kind, "refund")
            msg.notify_status = status
            msg.notify_error = reason
            s.commit()
        self.assertEqual(fake.replies, [], "达到上限时挂起")

        with SessionLocal() as s:
            out = notify_service.flush_held_notifications(s, client=fake)
        self.assertEqual(out["sent"], 1, out)
        self.assertEqual(len(fake.replies), 1)
        self.assertIn("退单", fake.replies[0], "补发必须是退单口径")
        self.assertNotIn("订单已收到", fake.replies[0], "不能被改写成归档回执")
        self.assertNotIn("订单已归档", fake.replies[0])

    def test_legacy_refund_without_notify_kind_still_refund(self):
        """老数据没记回执类型：按消息意图（intent=refund）兜底，同样不能变归档文案。"""
        settings.notify_mode = "reply"
        settings.notify_wait_screenshot_seconds = 0
        self._seed("3316902457144020686", intent="refund")
        with SessionLocal() as s:
            msg = s.scalar(select(RawMessage).where(RawMessage.message_id == "wm_refund"))
            msg.notify_status = "held"
            msg.notify_error = "静默期，已挂起等待合并补发"
            msg.notify_kind = ""
            s.commit()
        fake = FakeClient()
        with SessionLocal() as s:
            out = notify_service.flush_held_notifications(s, client=fake)
        self.assertEqual(out["sent"], 1)
        self.assertIn("退单", fake.replies[0])
        self.assertNotIn("订单已收到", fake.replies[0])

    def test_held_cancel_rebuilt_as_cancel(self):
        """撤单回执同理：补发要保留撤单口径。"""
        settings.notify_mode = "reply"
        settings.notify_wait_screenshot_seconds = 0
        self._seed("3316902457144020687", intent="cancel")
        fake = FakeClient()
        with SessionLocal() as s:
            msg, order = self._get(s)
            status, _ = notify_service.notify_cancel(msg, order, fake, session=s, keyword="撤单")
            self.assertEqual(status, "sent")
        self.assertIn("撤单", fake.replies[0])
        self.assertNotIn("订单已收到", fake.replies[0])

    def test_digest_marks_refund_orders(self):
        """多条合并补发时，退单要在明细里标出来，别混在普通订单里。"""
        settings.notify_digest = True
        msg = RawMessage(message_id="m9", intent="refund")
        order = Order(id=99, order_no="512729881514602021", product_name="苹果磁吸充电宝")
        self.assertIn("退单", notify_service.build_digest([(msg, order)]))


def select_msg_id():
    """取那条待回执消息的 id（用例里只建一条，避免到处写 message_id）。"""
    return select(RawMessage.id).where(RawMessage.message_id == "wm_1")


if __name__ == "__main__":
    unittest.main()
