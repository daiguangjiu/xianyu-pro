"""回执推送策略：机器人消息过滤、时段判定、关闭下发与静默挂起。"""
from __future__ import annotations

import unittest
from datetime import datetime

from app.config import settings
from app.models import Order, RawMessage
from app.services import notify
from app.services.filter import is_bot_message


class NotifyPolicyTest(unittest.TestCase):
    def setUp(self):
        self._snapshot = {
            "mode": settings.notify_mode,
            "hours": settings.notify_active_hours,
            "action": settings.notify_quiet_action,
            "interval": settings.notify_min_interval_seconds,
            "max_per_hour": settings.notify_max_per_hour,
        }
        self._local_now = notify.local_now

    def tearDown(self):
        settings.notify_mode = self._snapshot["mode"]
        settings.notify_active_hours = self._snapshot["hours"]
        settings.notify_quiet_action = self._snapshot["action"]
        settings.notify_min_interval_seconds = self._snapshot["interval"]
        settings.notify_max_per_hour = self._snapshot["max_per_hour"]
        notify.local_now = self._local_now

    # ---------------- 根因：机器人自身消息 ----------------
    def test_bot_message_detected(self):
        self.assertTrue(is_bot_message("app", "普通文本"))
        self.assertTrue(is_bot_message("user", "✅ 已收到并处理，订单已归档"))
        self.assertTrue(is_bot_message("user", "📦 已收到并处理 3 条订单"))
        self.assertFalse(is_bot_message("user", "【闲鱼订单】订单号：12345"))

    def test_notify_skips_bot_message(self):
        settings.notify_mode = "reply"
        msg = RawMessage(message_id="m1", sender_type="app", raw_text="✅ 已收到并处理")
        status, _ = notify.notify_processed(msg, Order())
        self.assertEqual(status, "skipped")

    # ---------------- 时段 ----------------
    def test_parse_windows(self):
        self.assertEqual(notify.parse_active_windows("09:00-22:00"), [(540, 1320)])
        self.assertEqual(notify.parse_active_windows("22:00-08:00"), [(1320, 1440), (0, 480)])
        self.assertEqual(notify.parse_active_windows(""), [])
        self.assertEqual(notify.parse_active_windows("乱写"), [])

    def test_is_active_time(self):
        settings.notify_active_hours = "09:00-22:00"
        self.assertTrue(notify.is_active_time(datetime(2026, 1, 1, 9, 0)))
        self.assertTrue(notify.is_active_time(datetime(2026, 1, 1, 21, 59)))
        self.assertFalse(notify.is_active_time(datetime(2026, 1, 1, 8, 59)))
        self.assertFalse(notify.is_active_time(datetime(2026, 1, 1, 22, 0)))
        settings.notify_active_hours = "22:00-08:00"
        self.assertTrue(notify.is_active_time(datetime(2026, 1, 1, 23, 30)))
        self.assertTrue(notify.is_active_time(datetime(2026, 1, 1, 3, 0)))
        self.assertFalse(notify.is_active_time(datetime(2026, 1, 1, 12, 0)))
        settings.notify_active_hours = ""
        self.assertTrue(notify.is_active_time(datetime(2026, 1, 1, 3, 0)))

    # ---------------- 关闭下发 ----------------
    def test_mode_off_never_sends(self):
        settings.notify_mode = "off"
        status, err = notify.notify_processed(RawMessage(message_id="m2"), Order(), force=True)
        self.assertEqual(status, "skipped")
        self.assertIn("关闭", err)

    # ---------------- 静默期挂起 / 丢弃 ----------------
    def test_quiet_hours_hold(self):
        settings.notify_mode = "reply"
        settings.notify_active_hours = "09:00-22:00"
        settings.notify_quiet_action = "hold"
        notify.local_now = lambda: datetime(2026, 1, 1, 23, 0)
        status, err = notify.notify_processed(RawMessage(message_id="m3"), Order())
        self.assertEqual(status, "held")
        self.assertIn("补发", err)

    def test_quiet_hours_drop(self):
        settings.notify_mode = "reply"
        settings.notify_active_hours = "09:00-22:00"
        settings.notify_quiet_action = "drop"
        notify.local_now = lambda: datetime(2026, 1, 1, 23, 0)
        status, _ = notify.notify_processed(RawMessage(message_id="m4"), Order())
        self.assertEqual(status, "skipped")

    def test_flush_skipped_outside_window(self):
        settings.notify_mode = "reply"
        settings.notify_active_hours = "09:00-22:00"
        notify.local_now = lambda: datetime(2026, 1, 1, 23, 0)
        self.assertEqual(notify.flush_held_notifications()["sent"], 0)

    # ---------------- 合并摘要 ----------------
    def test_build_digest(self):
        order = Order(order_no="SN123", product_name="iPhone 13", sale_amount=2999.0)
        text = notify.build_digest([(RawMessage(message_id="m5"), order)] * 3)
        self.assertIn("3 条订单", text)
        self.assertIn("SN123", text)


if __name__ == "__main__":
    unittest.main()
