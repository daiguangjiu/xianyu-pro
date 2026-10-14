"""语音播报 + 订单数据备份。

业务口径（2026-09）：
  1 群里同步到新订单 -> 语音播报「你有一笔订单要处理，请实际处理！」；
  2 供应商回单物流更新 -> 语音播报「你有一笔订单供应商已发货，请及时完成店铺发货操作！」；
  3 播报开关 / 音量 / 语速 / 音调 / 语言 / 两条文案都在系统设置页配置，写入 .env 即时生效；
  4 每天北京时间 03:00 自动备份订单数据，只保留最近 7 天，后台「数据备份」页可查看/下载/删除。
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, RawMessage, VoiceEvent  # noqa: E402
from app.services import backup as backup_service  # noqa: E402
from app.services import settings_store as store  # noqa: E402
from app.services import voice as voice_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

ORDER_TEXT = (
    "产品名称： 亚朵四代单枕头\n"
    "收货人： 贲\n"
    "手机号码： 14790021215\n"
    "地址信息： 安徽省 滁州市 琅琊区 清流街道\n"
    "售价：115\n"
    "成本价：60\n"
    "所属供应链：华强北数码\n"
    "出货闲鱼号：卖货的小姑娘\n"
    "回款支付宝归属人：卫玉珍\n"
)

ENV_BACKUP = store.ENV_PATH.read_text(encoding="utf-8") if store.ENV_PATH.exists() else ""
VOICE_ORIG = (
    settings.voice_enabled,
    settings.voice_volume,
    settings.voice_rate,
    settings.voice_pitch,
    settings.voice_lang,
    settings.voice_name,
    settings.voice_repeat,
    settings.voice_order_text,
    settings.voice_tracking_text,
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
                "chat_id": "oc_voice_test",
                "chat_type": "group",
                "message_type": "text",
                "content": content,
            },
        },
    }


class VoiceBackupTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(VoiceEvent).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

        # 播报配置回到默认，避免其它用例改动串味
        (
            settings.voice_enabled,
            settings.voice_volume,
            settings.voice_rate,
            settings.voice_pitch,
            settings.voice_lang,
            settings.voice_name,
            settings.voice_repeat,
            settings.voice_order_text,
            settings.voice_tracking_text,
        ) = VOICE_ORIG

        # 备份目录隔离到测试目录，不动真实备份
        self.backup_dir = ROOT / "data" / "test_backups"
        if self.backup_dir.exists():
            shutil.rmtree(self.backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._orig_backup_dir = settings.backup_dir
        settings.backup_dir = str(self.backup_dir)
        self._orig_backup_enabled = settings.backup_enabled
        settings.backup_enabled = True

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(VoiceEvent).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        settings.backup_dir = self._orig_backup_dir
        settings.backup_enabled = self._orig_backup_enabled
        try:
            store.ENV_PATH.write_text(ENV_BACKUP, encoding="utf-8")
        except OSError:
            pass
        (
            settings.voice_enabled,
            settings.voice_volume,
            settings.voice_rate,
            settings.voice_pitch,
            settings.voice_lang,
            settings.voice_name,
            settings.voice_repeat,
            settings.voice_order_text,
            settings.voice_tracking_text,
        ) = VOICE_ORIG

    # ---------------------------------------------------------------- 语音播报
    def test_01_default_texts(self):
        self.assertEqual(voice_service.template_of("order"), "你有一笔订单要处理，请实际处理！")
        self.assertEqual(
            voice_service.template_of("tracking"),
            "你有一笔订单供应商已发货，请及时完成店铺发货操作！",
        )

    def test_02_render_placeholders(self):
        order = Order(
            order_no="VOICE-1",
            product_name="亚朵四代单枕头",
            supplier_name="华强北数码",
            receiver_name="贲",
            tracking_no="SF123456",
        )
        text = voice_service.render("订单 {订单号} {产品} 已发货，单号 {物流单号}", order)
        self.assertIn("VOICE-1", text)
        self.assertIn("亚朵四代单枕头", text)
        self.assertIn("SF123456", text)
        self.assertNotIn("{", text)

    def test_03_new_order_emits_voice_event(self):
        resp = self.client.post(
            "/feishu/event",
            json=_event("om_voice_1", json.dumps({"text": ORDER_TEXT}, ensure_ascii=False)),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.json().get("order_id"), resp.text)

        pending = self.client.get("/api/voice/pending?since=0").json()
        self.assertTrue(pending["enabled"])
        self.assertEqual(pending["config"]["templates"]["order"], "你有一笔订单要处理，请实际处理！")
        kinds = [e["kind"] for e in pending["events"]]
        self.assertIn("order", kinds)
        event = [e for e in pending["events"] if e["kind"] == "order"][0]
        self.assertEqual(event["text"], "你有一笔订单要处理，请实际处理！")

    def test_04_tracking_update_emits_voice_event(self):
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no="VOICE-TRACK-1",
                product_name="亚朵四代单枕头",
                supplier_name="华强北数码",
                supplier_status="pushed",
                pushed_at=now(),
                msg_time=now(),
            )
            s.add(order)
            s.commit()
            order_id = order.id
        since = self.client.get("/api/voice/pending?since=0").json()["latest_id"]

        resp = self.client.post(
            f"/batches/tracking/{order_id}",
            data={"tracking_no": "SF888888", "sync": "0", "back": "/batches"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        pending = self.client.get(f"/api/voice/pending?since={since}").json()
        events = [e for e in pending["events"] if e["kind"] == "tracking"]
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0]["text"], "你有一笔订单供应商已发货，请及时完成店铺发货操作！"
        )
        self.assertEqual(events[0]["order_id"], order_id)

    def test_05_disabled_emits_nothing(self):
        settings.voice_enabled = False
        with SessionLocal() as s:
            order = Order(
                platform="xianyu", order_no="VOICE-OFF-1", supplier_status="pushed", pushed_at=now()
            )
            s.add(order)
            s.commit()
            order_id = order.id
        self.client.post(
            f"/batches/tracking/{order_id}",
            data={"tracking_no": "SF000", "sync": "0", "back": "/batches"},
            follow_redirects=False,
        )
        pending = self.client.get("/api/voice/pending?since=0").json()
        self.assertFalse(pending["enabled"])
        self.assertEqual(pending["events"], [])

    def test_06_settings_page_and_save(self):
        page = self.client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn("语音播报", page.text)
        self.assertIn("你有一笔订单要处理，请实际处理！", page.text)
        self.assertIn("你有一笔订单供应商已发货，请及时完成店铺发货操作！", page.text)

        resp = self.client.post(
            "/settings/voice",
            data={
                "voice_enabled": "1",
                "voice_volume": "0.8",
                "voice_rate": "1.2",
                "voice_pitch": "1",
                "voice_lang": "zh-CN",
                "voice_name": "",
                "voice_repeat": "2",
                "voice_order_text": "订单 {订单号} 到啦，请及时处理",
                "voice_tracking_text": "供应商已发货，单号 {物流单号}",
            },
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))
        self.assertTrue(settings.voice_enabled)
        self.assertAlmostEqual(settings.voice_volume, 0.8)
        self.assertAlmostEqual(settings.voice_rate, 1.2)
        self.assertEqual(settings.voice_repeat, 2)
        self.assertEqual(settings.voice_order_text, "订单 {订单号} 到啦，请及时处理")
        self.assertIn("VOICE_ORDER_TEXT=订单 {订单号} 到啦，请及时处理", store.ENV_PATH.read_text(encoding="utf-8"))

    def test_06b_voice_picker_select_and_custom_timbre(self):
        """音色用下拉选择（本机音色由 JS 填充），保存后写进 .env 并下发给前端。"""
        page = self.client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="voice-name-select"', page.text, "音色改成下拉选择")
        self.assertIn('id="voice-picker-hint"', page.text)
        self.assertNotIn('name="voice_name" type="text"', page.text)

        resp = self.client.post(
            "/settings/voice",
            data={
                "voice_enabled": "1",
                "voice_volume": "1",
                "voice_rate": "1",
                "voice_pitch": "1",
                "voice_lang": "zh-CN",
                "voice_name": "Ting-Ting",
                "voice_repeat": "1",
            },
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))
        self.assertEqual(settings.voice_name, "Ting-Ting")
        self.assertIn('data-current="Ting-Ting"', self.client.get("/settings").text)
        self.assertEqual(voice_service.payload()["voice_name"], "Ting-Ting")

    def test_07_pending_since_filters(self):
        with SessionLocal() as s:
            s.add(VoiceEvent(kind="order", order_no="OLD-1", text="旧播报", created_at=now()))
            s.commit()
            old_id = s.scalar(select(VoiceEvent.id).order_by(VoiceEvent.id.desc()).limit(1))
        pending = self.client.get(f"/api/voice/pending?since={old_id}").json()
        self.assertEqual(pending["events"], [])

    # ---------------------------------------------------------------- 数据备份
    def _make_order(self, order_no: str) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="亚朵四代单枕头",
                sale_amount=115.0,
                cost_amount=60.0,
                supplier_name="华强北数码",
                tracking_no="SF123",
            )
            s.add(order)
            s.commit()
            return order.id

    def test_08_backup_creates_json_snapshot(self):
        self._make_order("BACKUP-1")
        meta = backup_service.create_backup(label="test")
        target = self.backup_dir / meta["file"]
        self.assertTrue(target.exists())
        self.assertEqual(meta["order_count"], 1)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "orders")
        self.assertEqual(payload["orders"][0]["order_no"], "BACKUP-1")
        self.assertEqual(payload["orders"][0]["tracking_no"], "SF123")

        rows = backup_service.list_backups()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file"], meta["file"])
        self.assertEqual(rows[0]["order_count"], 1)

    def test_09_prune_keeps_last_7_days(self):
        meta = backup_service.create_backup(label="test")
        old = self.backup_dir / "orders_20200101_030000.json"
        old.write_text("{}", encoding="utf-8")
        (self.backup_dir / "orders_20200101_030000.json.meta.json").write_text("{}", encoding="utf-8")

        out = backup_service.prune_backups(7)
        self.assertEqual(out["count"], 1)
        self.assertFalse(old.exists())
        self.assertFalse((self.backup_dir / "orders_20200101_030000.json.meta.json").exists())
        self.assertTrue((self.backup_dir / meta["file"]).exists())
        # 保留天数调大时，7 天前的备份也不该被删
        keep = backup_service.prune_backups(3650)
        self.assertEqual(keep["count"], 0)

    def test_10_backups_page_download_and_delete(self):
        self._make_order("BACKUP-2")
        meta = backup_service.create_backup(label="manual")

        page = self.client.get("/backups")
        self.assertEqual(page.status_code, 200)
        self.assertIn(meta["file"], page.text)
        self.assertIn("数据备份", page.text)

        resp = self.client.post("/backups/create", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(len(backup_service.list_backups()), 2)

        down = self.client.get(f"/backups/download/{meta['file']}")
        self.assertEqual(down.status_code, 200)
        self.assertIn("BACKUP-2", down.text)

        self.assertEqual(self.client.get("/backups/download/evil.json").status_code, 404)

        resp = self.client.post(f"/backups/delete/{meta['file']}", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertFalse((self.backup_dir / meta["file"]).exists())

    def test_11_next_backup_time_is_3am(self):
        settings.backup_hour = 3
        nxt = backup_service.next_backup_time()
        self.assertEqual(nxt.hour, 3)
        self.assertEqual(nxt.minute, 0)
        self.assertGreater(nxt, now())

    def test_12_run_if_due_only_once_per_day(self):
        self._make_order("BACKUP-3")
        settings.backup_hour = 0  # 已过备份点，允许补跑
        first = backup_service.run_if_due()
        self.assertIsNotNone(first)
        second = backup_service.run_if_due()
        self.assertIsNone(second)


if __name__ == "__main__":
    unittest.main()
