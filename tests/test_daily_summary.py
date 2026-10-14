"""每日出单汇总：按「人」统计店铺 / 出单 / 退单，定时推送到群。

业务口径（2026-09）：
  1 每天定时（默认 24:00，即次日 00:00）按人汇总当天出单店铺数、出单数、出单金额、退单数、退单金额；
  2 消息头部固定为「📊 今日（YYYY-MM-DD）出单情况汇总」；
  3 同一天只推一次；推送开关 / 时刻 / 目标群 / 是否 @ 人 / 无单是否推送都在系统设置页配置。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    DailySummaryLog,
    Order,
    PushBatchItem,
    RawMessage,
    VoiceEvent,
)
from app.services import daily_summary as ds  # noqa: E402
from app.services import settings_store as store  # noqa: E402
from app.timeutil import beijing_now, now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
ENV_BACKUP = store.ENV_PATH.read_text(encoding="utf-8") if store.ENV_PATH.exists() else ""


class FakeClient:
    """假飞书客户端：记录下发内容，不真正联网。"""

    enabled = True

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        self.sent.append((receive_id, text))
        return {"ok": True}


def _order(
    no: str,
    sender: str,
    sender_id: str,
    shop: str,
    amount: float,
    day: datetime,
    hour: int = 10,
    refund: bool = False,
    deal: float = 0.0,
) -> Order:
    # 库里存 UTC（2026-10-03 口径）：把「北京 day 日 hour 点」写成 UTC（-8h），
    # 这样按天的统计（date(col, '+8 hours')）算出来的就是北京的自然日
    ordered_at = datetime(day.year, day.month, day.day, hour, 0, 0) - timedelta(hours=8)
    return Order(
        platform="xianyu",
        order_no=no,
        supplier_name="BH001",
        xianyu_account=shop,
        sender_name=sender,
        sender_id=sender_id,
        sale_amount=amount,
        deal_amount=deal,
        quantity=1,
        ordered_at=ordered_at,
        refund_status="pending" if refund else "",
        refund_at=ordered_at.replace(hour=hour + 1) if refund else None,
    )


class DailySummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(PushBatchItem).delete()
            s.query(RawMessage).delete()
            s.query(VoiceEvent).delete()
            s.query(Order).delete()
            s.commit()
        with SessionLocal() as s:
            s.query(DailySummaryLog).delete()
            s.commit()
        self._snapshot = {
            "enabled": settings.daily_summary_enabled,
            "hour": settings.daily_summary_hour,
            "chat_ids": list(settings.daily_summary_chat_ids),
            "at_users": settings.daily_summary_at_users,
            "skip_empty": settings.daily_summary_skip_empty,
            "feishu_chat_ids": list(settings.feishu_chat_ids),
        }

    def tearDown(self) -> None:
        settings.daily_summary_enabled = self._snapshot["enabled"]
        settings.daily_summary_hour = self._snapshot["hour"]
        settings.daily_summary_chat_ids = list(self._snapshot["chat_ids"])
        settings.daily_summary_at_users = self._snapshot["at_users"]
        settings.daily_summary_skip_empty = self._snapshot["skip_empty"]
        settings.feishu_chat_ids = list(self._snapshot["feishu_chat_ids"])
        store.ENV_PATH.write_text(ENV_BACKUP, encoding="utf-8")

    # ---------------- 统计口径 ----------------
    def test_collect_by_person(self):
        today = beijing_now().date()
        with SessionLocal() as s:
            # 张三：3 单（两家店铺），其中 1 单退单
            s.add_all(
                [
                    _order("T-1", "张三", "ou_zhang", "闲鱼小店A", 100.0, today, 9),
                    _order("T-2", "张三", "ou_zhang", "闲鱼小店A", 200.0, today, 11),
                    _order("T-3", "张三", "ou_zhang", "闲鱼小店B", 300.0, today, 12),
                    _order("T-4", "张三", "ou_zhang", "闲鱼小店B", 400.0, today, 13, refund=True),
                    # 李四：1 单
                    _order("T-5", "李四", "ou_li", "闲鱼小店C", 500.0, today, 15),
                    # 昨天的数据不计入今天
                    _order(
                        "T-6",
                        "张三",
                        "ou_zhang",
                        "闲鱼小店A",
                        999.0,
                        datetime(today.year, today.month, today.day) - timedelta(days=1),
                    ),
                ]
            )
            s.commit()
            payload = ds.collect(s, today)

        self.assertEqual(payload["date"], today.isoformat())
        people = {p["name"]: p for p in payload["people"]}
        self.assertEqual(set(people), {"张三", "李四"})
        zhang = people["张三"]
        self.assertEqual(zhang["shops"], 2)          # 出单店铺去重
        self.assertEqual(zhang["orders"], 3)         # 退单不计出单
        self.assertEqual(zhang["amount"], 600.0)     # 100+200+300
        self.assertEqual(zhang["refunds"], 1)
        self.assertEqual(zhang["refund_amount"], 400.0)
        self.assertEqual(people["李四"]["orders"], 1)
        self.assertEqual(people["李四"]["refunds"], 0)
        self.assertEqual(payload["total"]["orders"], 4)
        self.assertEqual(payload["total"]["amount"], 1100.0)
        self.assertEqual(payload["total"]["refunds"], 1)
        self.assertEqual(payload["total"]["refund_amount"], 400.0)
        self.assertEqual(payload["total"]["shops"], 3)   # A/B/C 三家
        self.assertEqual(payload["total"]["people"], 2)

    def test_amount_falls_back_to_deal(self):
        """售价缺失（未补录）时，出单金额退回成交价。"""
        today = beijing_now().date()
        with SessionLocal() as s:
            s.add(_order("D-1", "王五", "ou_wang", "店A", 0.0, today, deal=88.5))
            s.commit()
            payload = ds.collect(s, today)
        self.assertEqual(payload["people"][0]["amount"], 88.5)

    def test_person_without_name(self):
        today = beijing_now().date()
        with SessionLocal() as s:
            s.add(_order("N-1", "", "ou_abcdef123456", "店A", 10.0, today))
            s.commit()
            payload = ds.collect(s, today)
        self.assertIn("123456", payload["people"][0]["name"])

    # ---------------- 文案 ----------------
    def test_build_text_head_and_lines(self):
        today = beijing_now().date()
        with SessionLocal() as s:
            s.add_all(
                [
                    _order("B-1", "张三", "ou_zhang", "店A", 100.0, today),
                    _order("B-2", "张三", "ou_zhang", "店A", 50.0, today, refund=True),
                ]
            )
            s.commit()
            payload = ds.collect(s, today)
        text = ds.build_text(payload, at_users=True)
        lines = text.splitlines()
        # 这条是整群日报：勾了 @ 就在最前面加 @所有人，明细里不再逐个 @
        self.assertEqual(
            lines[0], f'<at user_id="all"></at> 📊 今日（{today.isoformat()}）出单情况汇总'
        )
        self.assertIn("张三", lines[1])
        self.assertNotIn("ou_zhang", text, "明细里不再逐个 @ 人")
        self.assertIn("店铺 1 家", lines[1])
        self.assertIn("出单 1 单 ¥100.00", lines[1])
        self.assertIn("退单 1 单 ¥50.00", lines[1])
        self.assertTrue(lines[-1].startswith("合计："))

        # 关闭 @ 人：只保留姓名
        plain = ds.build_text(payload, at_users=False)
        self.assertNotIn("<at ", plain)
        self.assertIn("张三", plain)

    # ---------------- 推送 ----------------
    def test_send_summary_and_state(self):
        today = beijing_now().date()
        with SessionLocal() as s:
            s.add(_order("S-1", "张三", "ou_zhang", "店A", 100.0, today))
            s.commit()
        client = FakeClient()
        settings.daily_summary_chat_ids = ["oc_summary"]
        out = ds.send_summary(day=today, client=client)
        self.assertEqual(out["sent"], 1)
        self.assertEqual(out["chat_ids"], ["oc_summary"])
        self.assertEqual(client.sent[0][0], "oc_summary")
        self.assertIn(f"📊 今日（", client.sent[0][1])
        self.assertIn('<at user_id="all"></at>', client.sent[0][1])
        state = ds.load_state()
        self.assertEqual(state["last_date"], today.isoformat())
        self.assertEqual(state["orders"], 1)

    def test_send_summary_skipped_when_empty(self):
        settings.daily_summary_chat_ids = ["oc_summary"]
        settings.daily_summary_skip_empty = True
        out = ds.send_summary(client=FakeClient())
        self.assertEqual(out["sent"], 0)
        self.assertEqual(out["skipped"], "当天没有出单与退单记录")

    def test_send_summary_reports_no_chat(self):
        today = beijing_now().date()
        with SessionLocal() as s:
            s.add(_order("E-1", "张三", "ou_zhang", "店A", 100.0, today))
            s.commit()
        settings.daily_summary_chat_ids = []
        settings.feishu_chat_ids = []
        out = ds.send_summary(day=today, client=FakeClient())
        self.assertEqual(out["sent"], 0)
        self.assertTrue(out["errors"])

    # ---------------- 定时与幂等 ----------------
    def test_run_if_due_only_once_a_day(self):
        today = beijing_now().date()
        with SessionLocal() as s:
            s.add(_order("R-1", "张三", "ou_zhang", "店A", 100.0, today))
            s.commit()
        # 24 点模式：次日 00:05 推「昨天」= 这里的 today
        fake_now = datetime(today.year, today.month, today.day, 0, 5, 0) + timedelta(days=1)
        settings.daily_summary_enabled = True
        settings.daily_summary_hour = 24
        settings.daily_summary_chat_ids = ["oc_summary"]
        client = FakeClient()
        with mock.patch.object(ds, "beijing_now", lambda: fake_now), mock.patch.object(
            ds, "now", lambda: fake_now - timedelta(hours=8)
        ):
            first = ds.run_if_due(client=client)
            self.assertIsNotNone(first)
            self.assertEqual(first["date"], today.isoformat())
            self.assertEqual(first["sent"], 1)
            # 同一天再跑：不再推送
            self.assertIsNone(ds.run_if_due(client=client))
        self.assertEqual(len(client.sent), 1)

    def test_run_if_due_before_hour(self):
        settings.daily_summary_enabled = True
        settings.daily_summary_hour = 8
        fake_now = beijing_now().replace(hour=7, minute=0, second=0, microsecond=0)
        with mock.patch.object(ds, "beijing_now", lambda: fake_now), mock.patch.object(
            ds, "now", lambda: fake_now - timedelta(hours=8)
        ):
            self.assertIsNone(ds.run_if_due(client=FakeClient()))

    def test_default_target_date_yesterday_when_24(self):
        """统计日按**北京的自然日**算（库里存 UTC，别拿 UTC 的日期当业务日）。"""
        settings.daily_summary_hour = 24
        self.assertEqual(ds.default_target_date(), (beijing_now() - timedelta(days=1)).date())
        settings.daily_summary_hour = 8
        self.assertEqual(ds.default_target_date(), beijing_now().date())


class DailySummarySettingsTest(unittest.TestCase):
    """系统设置页：汇总配置保存在 .env 并立即生效 + 手动推送。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(PushBatchItem).delete()
            s.query(RawMessage).delete()
            s.query(VoiceEvent).delete()
            s.query(Order).delete()
            s.commit()
        with SessionLocal() as s:
            s.query(DailySummaryLog).delete()
            s.commit()
        self._snapshot = {
            "enabled": settings.daily_summary_enabled,
            "hour": settings.daily_summary_hour,
            "chat_ids": list(settings.daily_summary_chat_ids),
            "at_users": settings.daily_summary_at_users,
            "skip_empty": settings.daily_summary_skip_empty,
            "feishu_chat_ids": list(settings.feishu_chat_ids),
        }

    def tearDown(self) -> None:
        settings.daily_summary_enabled = self._snapshot["enabled"]
        settings.daily_summary_hour = self._snapshot["hour"]
        settings.daily_summary_chat_ids = list(self._snapshot["chat_ids"])
        settings.daily_summary_at_users = self._snapshot["at_users"]
        settings.daily_summary_skip_empty = self._snapshot["skip_empty"]
        settings.feishu_chat_ids = list(self._snapshot["feishu_chat_ids"])
        store.ENV_PATH.write_text(ENV_BACKUP, encoding="utf-8")

    def test_save_and_manual_send(self):
        today = beijing_now().date()
        with SessionLocal() as s:
            s.add(_order("W-1", "张三", "ou_zhang", "店A", 120.0, today))
            s.commit()

        client = TestClient(app)
        self.assertEqual(client.post("/login", data=ADMIN, follow_redirects=False).status_code, 303)
        page = client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn("每日出单汇总通知", page.text)

        resp = client.post(
            "/settings/daily-summary",
            data={
                "daily_summary_enabled": "1",
                "daily_summary_hour": "24",
                "daily_summary_chat_ids": "oc_web_summary",
                "daily_summary_at_users": "1",
                "daily_summary_skip_empty": "0",
            },
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))
        self.assertTrue(settings.daily_summary_enabled)
        self.assertEqual(settings.daily_summary_hour, 24)
        self.assertEqual(settings.daily_summary_chat_ids, ["oc_web_summary"])
        self.assertFalse(settings.daily_summary_skip_empty)
        self.assertIn("DAILY_SUMMARY_CHAT_IDS=oc_web_summary", store.ENV_PATH.read_text(encoding="utf-8"))

        fake = FakeClient()
        with mock.patch("app.feishu.client.get_client", lambda: fake):
            resp = client.post("/settings/daily-summary/send", follow_redirects=False)
            self.assertIn(resp.status_code, (302, 303))
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0][0], "oc_web_summary")
        self.assertIn(f"📊 今日（{today.isoformat()}）出单情况汇总", fake.sent[0][1])


if __name__ == "__main__":
    unittest.main()
