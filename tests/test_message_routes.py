"""消息中心：每条消息的发送方式按条可配，但**默认必须等于改造前的老行为**。

这里把「默认钩选型」钉死：以后谁改坏了，测试会先拦下来。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.messages_registry import (  # noqa: E402
    CHANNELS,
    CH_GROUP,
    CH_PM_SENDER,
    CH_PM_SYNC,
    KINDS,
)
from app.services import message_rules, notify  # noqa: E402


class Rec:
    """假飞书客户端：把发出去的消息都记下来，不真的发。"""

    enabled = True

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.directs: list[tuple[str, str]] = []

    def reply_text(self, message_id: str, text: str) -> dict:
        self.replies.append(text)
        return {"message_id": "om_x"}

    def send_text(self, target: str, text: str, receive_id_type: str = "chat_id") -> dict:
        self.directs.append((target, text))
        return {"message_id": "om_x"}


class _Msg:
    message_id = "om_src"
    sender_id = "ou_sender"
    sender_name = "周玉萍"
    chat_id = "oc_group"
    raw_text = "订单模板消息"
    notify_kind = ""


class DefaultRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._mode = settings.notify_mode
        self._pm_sync = settings.notify_pm_sync
        self._routes = dict(settings.message_routes)
        settings.notify_mode = "reply"
        settings.notify_pm_sync = True

    def tearDown(self) -> None:
        settings.notify_mode = self._mode
        settings.notify_pm_sync = self._pm_sync
        settings.message_routes = self._routes

    def test_default_routes_equal_old_behaviour(self):
        """没在消息中心里改过时，发送方式就是这套功能上线前的样子。"""
        expected = {
            "receipt": (CH_GROUP, CH_PM_SENDER),
            "missing_image": (CH_GROUP,),
            "duplicate": (CH_GROUP, CH_PM_SENDER),
            "refund": (CH_GROUP, CH_PM_SENDER),
            "cancel": (CH_GROUP, CH_PM_SENDER),
            "intent": (CH_GROUP, CH_PM_SENDER),
            "digest": (CH_GROUP, CH_PM_SENDER),
            "tracking": (CH_GROUP, CH_PM_SENDER),
            "refund_reason": (CH_GROUP,),
            "daily_summary": (CH_GROUP,),
        }
        for key, want in expected.items():
            self.assertEqual(message_rules.channels_of(key), want, key)

    def test_registry_covers_every_channel_flag(self):
        """登记表里的默认值不能写出登记表以外的渠道。"""
        for kind in KINDS:
            for ch in kind.default_channels:
                self.assertIn(ch, [c for c, _ in CHANNELS], f"{kind.key}: {ch}")
            self.assertIn(CH_PM_SYNC, [c for c, _ in CHANNELS])

    def test_global_off_and_direct_still_win(self):
        settings.notify_mode = "off"
        for key in ("receipt", "missing_image", "daily_summary"):
            for ch, _ in CHANNELS:
                self.assertFalse(message_rules.wants(key, ch), f"{key}/{ch}")
        settings.notify_mode = "direct"
        self.assertFalse(message_rules.wants("receipt", CH_GROUP))
        self.assertTrue(message_rules.wants("receipt", CH_PM_SENDER))


class SendRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._mode = settings.notify_mode
        self._routes = dict(settings.message_routes)
        self._sync_fn = notify.sync_open_ids
        settings.notify_mode = "reply"

    def tearDown(self) -> None:
        settings.notify_mode = self._mode
        settings.message_routes = self._routes
        notify.sync_open_ids = self._sync_fn

    def _send(self, key: str) -> Rec:
        client = Rec()
        notify._send_one(client, _Msg(), "✅ 收到了 " + '<at user_id="ou_sender"></at>', key=key)
        return client

    def test_receipt_goes_to_group_and_sender_pm(self):
        client = self._send("receipt")
        self.assertEqual(len(client.replies), 1, "群里那条照旧要发")
        self.assertEqual([t for t, _ in client.directs], ["ou_sender"])

    def test_pm_text_has_no_at_tag(self):
        """私信里再带一排 @ 标签就是一串乱码，统一剥掉。"""
        client = self._send("receipt")
        self.assertTrue(client.directs)
        self.assertNotIn("<at ", client.directs[0][1])

    def test_missing_image_group_only_by_default(self):
        client = self._send("missing_image")
        self.assertEqual(len(client.replies), 1)
        self.assertEqual(client.directs, [], "缺截图提醒默认不走私信")

    def test_turn_off_group_channel(self):
        """消息中心里把「群里回复」勾掉：这条消息就只在私信出现。"""
        message_rules.set_route("receipt", [CH_PM_SENDER])
        client = self._send("receipt")
        self.assertEqual(client.replies, [])
        self.assertEqual([t for t, _ in client.directs], ["ou_sender"])

    def test_turn_on_pm_to_sync_person(self):
        """勾上「私信发给通知同步人」：除了本人，同步人也收到一份。"""
        notify.sync_open_ids = lambda client, msg: ["ou_niulei"]  # 固定的通知同步人
        message_rules.set_route("receipt", [CH_GROUP, CH_PM_SENDER, CH_PM_SYNC])
        client = Rec()
        notify._send_one(client, _Msg(), "收到", key="receipt")
        self.assertEqual(
            sorted(t for t, _ in client.directs), ["ou_niulei", "ou_sender"]
        )


if __name__ == "__main__":
    unittest.main()
