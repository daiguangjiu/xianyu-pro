"""通知下发：群里 @ 发送人 + 通知同步人（牛磊）；私信模式下同步发给这两个人。

群里消息一多，@ 很容易被刷过去漏掉，所以通知支持「私聊同步一份」，
下发方式由设置页的「回执推送策略」切换（reply 群内回复 / direct 私信 / off 关闭）。
"""
from __future__ import annotations

import unittest

from app.config import settings
from app.models import RawMessage
from app.services import notify


class FakeClient:
    """记录发出去的消息：群回复与私信分开存。"""

    def __init__(self) -> None:
        self.enabled = False  # 不需要真的打飞书接口
        self.replies: list[str] = []
        self.directs: list[tuple[str, str]] = []

    def reply_text(self, message_id: str, text: str) -> dict:
        self.replies.append(text)
        return {"message_id": "fake_reply"}

    def send_text(
        self, receive_id: str, text: str, receive_id_type: str = "chat_id"
    ) -> dict:
        self.directs.append((receive_id_type, receive_id))
        self.replies.append(text)
        return {"message_id": "fake_direct"}


class NotifyAtAndPmTest(unittest.TestCase):
    def setUp(self) -> None:
        self._mode = settings.notify_mode
        self._pm_sync = settings.notify_pm_sync
        self._cc_open = settings.refund_cc_open_ids
        # 直接给 open_id，省掉按姓名查群成员（「牛磊」）
        settings.refund_cc_open_ids = ["ou_niulei"]

    def tearDown(self) -> None:
        settings.notify_mode = self._mode
        settings.notify_pm_sync = self._pm_sync
        settings.refund_cc_open_ids = self._cc_open

    def _msg(self) -> RawMessage:
        return RawMessage(
            message_id="om_notify_1",
            chat_id="oc_notify",
            chat_name="订单群",
            sender_id="ou_sender",
            sender_name="周玉萍",
            raw_text="退单",
        )

    def test_group_reply_ats_sender_and_sync_person(self):
        """群里回复：自动 @ 消息发送人和通知同步人（牛磊）。"""
        settings.notify_mode = "reply"
        settings.notify_pm_sync = False  # 这条只看群里那条
        fake = FakeClient()
        notify._send_one(fake, self._msg(), "订单已收到")
        self.assertEqual(len(fake.replies), 1)
        self.assertIn('<at user_id="ou_sender"></at>', fake.replies[0])
        self.assertIn('<at user_id="ou_niulei"></at>', fake.replies[0])

    def test_group_reply_also_pms_the_sender(self):
        """群里照发之外，再私聊同步一条给发送人本人 —— 他盯自己的私信就不会漏。"""
        settings.notify_mode = "reply"
        settings.notify_pm_sync = True
        fake = FakeClient()
        notify._send_one(fake, self._msg(), "订单已收到")
        self.assertEqual(len(fake.replies), 2, "群里一条 + 私信同步一条")
        self.assertEqual(fake.directs, [("open_id", "ou_sender")], "只私聊同步给本人")
        self.assertIn('<at user_id="ou_sender"></at>', fake.replies[0])

    def test_text_with_at_is_untouched(self):
        """已经 @ 过人的回执（退单回执按配置 @、整群日报 @所有人）不再重复加。"""
        settings.notify_mode = "reply"
        fake = FakeClient()
        notify._send_one(fake, self._msg(), '<at user_id="ou_x"></at> 退单已收到')
        self.assertEqual(fake.replies[0].count("<at user_id="), 1)

    def test_private_mode_sends_to_sender_and_sync_person(self):
        """私信模式：群里不再下发，改私聊发给发送人和牛磊，一个都不少。"""
        settings.notify_mode = "direct"
        fake = FakeClient()
        notify._send_one(fake, self._msg(), "订单已收到")
        self.assertEqual(
            sorted(fake.directs),
            [("open_id", "ou_niulei"), ("open_id", "ou_sender")],
        )

    def test_private_mode_has_no_at(self):
        """私信里 @ 是无效的：文案保持纯文本。"""
        settings.notify_mode = "direct"
        fake = FakeClient()
        notify._send_one(fake, self._msg(), "订单已收到")
        self.assertNotIn("<at user_id=", fake.replies[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
