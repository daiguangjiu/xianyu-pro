"""同步窗口与回执时效（2026-09）：消息不能漏、补历史时不能刷群。

背景：群里共计 283 条消息，库里只有 152 条 —— 定时同步只回看 15 分钟，
新群加进来也从「最近 15 分钟」开始拉，加群前的历史消息永远进不来。
现在首次同步自动回补 SYNC_BACKFILL_DAYS 天，续拉时以「已同步到的最后一条」为准；
补录进来的老消息照常入库解析，但不再往群里补发「已收到」回执。
"""
from __future__ import annotations

import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Order, RawMessage  # noqa: E402
from app.services import ingest as ingest_service  # noqa: E402
from app.services import notify as notify_service  # noqa: E402
from app.timeutil import now  # noqa: E402

MINUTE_MS = 60_000


class FakeClient:
    enabled = True

    def __init__(self):
        self.replies: list[str] = []

    def reply_text(self, message_id: str, text: str) -> dict:
        self.replies.append(text)
        return {"ok": True}

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        self.replies.append(text)
        return {"ok": True}


class SyncWindowTest(unittest.TestCase):
    def test_01_new_chat_backfills_history(self):
        """新群：首次同步要回补历史（默认 7 天），不能只拿最近 15 分钟。"""
        with mock.patch.object(settings, "sync_backfill_days", 7):
            start = ingest_service.auto_sync_start_ms(None)
        now_ms = int(time.time() * 1000)
        self.assertLessEqual(start, now_ms - 7 * 86400 * 1000)
        self.assertGreater(start, now_ms - 8 * 86400 * 1000)

    def test_02_resume_from_last_message(self):
        """已同步过的群：停摆几天后恢复，要从「最后一条已同步消息」续拉，不能漏。"""
        three_days_ago = now() - timedelta(days=3)
        state = SimpleNamespace(last_message_time=three_days_ago)
        with mock.patch.object(settings, "sync_lookback_minutes", 15):
            start = ingest_service.auto_sync_start_ms(state)
        # 起点落在「3 天前 - 1 分钟」附近，远早于 15 分钟窗口
        self.assertLessEqual(start, int(time.time() * 1000) - 3 * 86400 * 1000)
        self.assertGreater(start, int(time.time() * 1000) - 3 * 86400 * 1000 - 2 * MINUTE_MS)
        self.assertLess(start, int(time.time() * 1000) - 15 * MINUTE_MS)

    def test_03_recent_chat_uses_lookback(self):
        """一直正常同步的群：只回看 15 分钟即可，不必每次都翻历史。"""
        state = SimpleNamespace(last_message_time=now() - timedelta(minutes=2))
        with mock.patch.object(settings, "sync_lookback_minutes", 15):
            start = ingest_service.auto_sync_start_ms(state)
        now_ms = int(time.time() * 1000)
        # 15 分钟窗口更早 -> 取它；至少不晚于 15 分钟前
        self.assertLessEqual(start, now_ms - 15 * MINUTE_MS + 1000)
        self.assertGreater(start, now_ms - 16 * MINUTE_MS)


class ReceiptAgeTest(unittest.TestCase):
    """补录的历史消息：入库解析，但不往群里补发回执。"""

    def setUp(self) -> None:
        init_db()
        with SessionLocal() as s:
            s.query(RawMessage).filter(RawMessage.message_id.in_(("om_old", "om_new"))).delete(
                synchronize_session=False
            )
            s.query(Order).filter(Order.order_no.in_(("OLD-1", "NEW-1"))).delete(
                synchronize_session=False
            )
            s.commit()

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(RawMessage).filter(RawMessage.message_id.in_(("om_old", "om_new"))).delete(
                synchronize_session=False
            )
            s.query(Order).filter(Order.order_no.in_(("OLD-1", "NEW-1"))).delete(
                synchronize_session=False
            )
            s.commit()

    def _send(self, message_id: str, order_no: str, minutes_ago: float) -> tuple[str, str, FakeClient]:
        client = FakeClient()
        with SessionLocal() as s:
            order = Order(order_no=order_no, product_name="测试品", sale_amount=100)
            s.add(order)
            s.commit()
            msg = RawMessage(
                message_id=message_id,
                chat_id="oc_test",
                msg_type="text",
                sender_id="ou_1",
                sender_type="user",
                raw_text="产品： 测试品",
                msg_time=now() - timedelta(minutes=minutes_ago),
                parse_status="parsed",
            )
            s.add(msg)
            s.commit()
            with mock.patch.object(settings, "notify_mode", "reply"):
                status, err = notify_service.notify_processed(msg, order, client, session=s)
            return status, err, client

    def test_01_old_message_is_silent(self):
        """3 小时前的消息（补录 / 服务恢复）：不下发回执。"""
        status, err, client = self._send("om_old", "OLD-1", 180)
        self.assertEqual(status, "skipped")
        self.assertIn("不补发回执", err)
        self.assertEqual(client.replies, [], "老消息不能再往群里补「已收到」")

    def test_02_new_message_still_notifies(self):
        status, err, client = self._send("om_new", "NEW-1", 1)
        self.assertEqual(status, "sent", err)
        self.assertTrue(client.replies)

    def test_03_manual_resend_can_bypass(self):
        """管理员手动补发（force=True）不受时效限制。"""
        client = FakeClient()
        with SessionLocal() as s:
            order = s.scalar(select(Order).where(Order.order_no == "OLD-1")) or Order(
                order_no="OLD-2", product_name="测试品"
            )
            s.add(order)
            s.commit()
            msg = RawMessage(
                message_id="om_old2",
                chat_id="oc_test",
                msg_type="text",
                sender_id="ou_1",
                sender_type="user",
                raw_text="产品： 测试品",
                msg_time=now() - timedelta(minutes=180),
                parse_status="parsed",
            )
            s.add(msg)
            s.commit()
            with mock.patch.object(settings, "notify_mode", "reply"):
                status, err = notify_service.notify_processed(
                    msg, order, client, session=s, force=True
                )
            s.query(RawMessage).filter(RawMessage.message_id == "om_old2").delete()
            s.query(Order).filter(Order.order_no.in_(("OLD-1", "OLD-2"))).delete()
            s.commit()
        self.assertEqual(status, "sent", err)
