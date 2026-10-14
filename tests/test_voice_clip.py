"""语音播报「自定义录音」：上传 / 替换 / 删除 + 播报时优先播录音。

业务口径（2026-09）：
  三个播报场景（新订单 / 供应商已发货 / 退单拦截）各可上传一段自己录的音频，
  配了录音就播录音、不再用浏览器合成音；录音是固定一段话，读不出 {订单号} 这类变量，
  需要读出动态内容时不给该场景传录音即可（留空自动回落到合成音）。
  音频存进 media/voice（docker 是数据卷，重建容器不丢），文件名写进 .env。
"""
from __future__ import annotations

import pathlib
import shutil
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import warnings  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

warnings.filterwarnings("ignore", message="fields may not start with 'st'")

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, RawMessage, VoiceEvent  # noqa: E402
from app.services import settings_store as store  # noqa: E402
from app.services import voice as voice_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
ENV_BACKUP = store.ENV_PATH.read_text(encoding="utf-8") if store.ENV_PATH.exists() else ""
CLIP_KEYS = ("voice_clip_order", "voice_clip_tracking", "voice_clip_refund")

MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00boss-clip"


class VoiceClipTest(unittest.TestCase):
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

        # 录音目录隔离到测试目录，不碰真实 media
        self.media_dir = ROOT / "data" / "test_media"
        if self.media_dir.exists():
            shutil.rmtree(self.media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._orig_media_dir = settings.media_dir
        settings.media_dir = str(self.media_dir)
        self._orig_clips = {k: getattr(settings, k) for k in CLIP_KEYS}
        for key in CLIP_KEYS:
            setattr(settings, key, "")
        self._orig_enabled = settings.voice_enabled
        settings.voice_enabled = True

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(VoiceEvent).delete()
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()
        settings.media_dir = self._orig_media_dir
        for key, value in self._orig_clips.items():
            setattr(settings, key, value)
        settings.voice_enabled = self._orig_enabled
        try:
            store.ENV_PATH.write_text(ENV_BACKUP, encoding="utf-8")
        except OSError:
            pass
        if self.media_dir.exists():
            shutil.rmtree(self.media_dir, ignore_errors=True)

    # ------------------------------------------------------------ 服务层
    def test_01_save_clip_validates_format_and_size(self):
        name, err = voice_service.save_clip("order", "老板语音.txt", b"x")
        self.assertEqual(name, "")
        self.assertIn("只支持", err)

        name, err = voice_service.save_clip("order", "老板语音", b"x" * 2)
        self.assertEqual(name, "")
        self.assertIn("只支持", err)

        big = b"x" * (voice_service.CLIP_MAX_BYTES + 1)
        name, err = voice_service.save_clip("order", "老板语音.mp3", big)
        self.assertEqual(name, "")
        self.assertIn("不能超过", err)

        name, err = voice_service.save_clip("order", "老板语音.mp3", b"")
        self.assertEqual(name, "")
        self.assertIn("空的", err)

        name, err = voice_service.save_clip("unknown_kind", "a.mp3", MP3)
        self.assertEqual(name, "")

    def test_02_save_and_replace_and_url(self):
        name, err = voice_service.save_clip("order", "来单了.mp3", MP3)
        self.assertEqual(err, "")
        self.assertEqual(name, "order.mp3")
        self.assertEqual(settings.voice_clip_order, "order.mp3")
        path = voice_service.clip_dir() / "order.mp3"
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), MP3)

        url = voice_service.clip_url("order")
        self.assertTrue(url.startswith("/media/voice/order.mp3"))
        self.assertEqual(voice_service.clip_url("tracking"), "", "没配的场景没有录音地址")

        # 换一个格式：旧文件清掉，配置指向新的
        name, err = voice_service.save_clip("order", "来单了.wav", b"RIFFfake")
        self.assertEqual((name, err), ("order.wav", ""))
        self.assertFalse(path.exists(), "旧录音被替换")
        self.assertTrue((voice_service.clip_dir() / "order.wav").exists())

    def test_03_clip_appears_in_pending_payload(self):
        voice_service.save_clip("order", "a.mp3", MP3)
        data = self.client.get("/api/voice/pending?since=0").json()
        self.assertTrue(data["enabled"])
        self.assertTrue(data["config"]["clips"]["order"], "前端能拿到录音地址")
        self.assertEqual(data["config"]["clips"]["tracking"], "")

    # ------------------------------------------------------------ 上传 / 删除（管理员）
    def test_04_upload_via_page_and_delete(self):
        resp = self.client.post(
            "/settings/voice/clip",
            data={"kind": "order"},
            files={"clip": ("老板来单.mp3", MP3, "audio/mpeg")},
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))
        self.assertEqual(settings.voice_clip_order, "order.mp3")
        self.assertTrue((voice_service.clip_dir() / "order.mp3").exists())
        # 持久化：写进 .env，重启后仍在
        self.assertIn("VOICE_CLIP_ORDER=order.mp3", store.ENV_PATH.read_text(encoding="utf-8"))

        page = self.client.get("/settings")
        self.assertIn("已上传录音", page.text, "设置页显示已上传")
        self.assertIn("/media/voice/order.mp3", page.text, "页面给出可直接试听的地址")

        # 再传一次 -> 同场景替换
        resp = self.client.post(
            "/settings/voice/clip",
            data={"kind": "order"},
            files={"clip": ("老板来单2.mp3", b"new-clip-body", "audio/mpeg")},
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))
        self.assertEqual(
            (voice_service.clip_dir() / "order.mp3").read_bytes(), b"new-clip-body", "替换成最新录音"
        )

        resp = self.client.post(
            "/settings/voice/clip/delete", data={"kind": "order"}, follow_redirects=False
        )
        self.assertIn(resp.status_code, (302, 303))
        self.assertEqual(settings.voice_clip_order, "")
        self.assertFalse((voice_service.clip_dir() / "order.mp3").exists())
        self.assertEqual(voice_service.clip_url("order"), "", "删掉后回落到合成音")
        page = self.client.get("/settings")
        self.assertIn("未上传", page.text)

    def test_05_upload_rejects_bad_input(self):
        resp = self.client.post(
            "/settings/voice/clip",
            data={"kind": "order"},
            files={"clip": ("铃声.txt", b"plain", "text/plain")},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(settings.voice_clip_order, "", "格式不允许时不落库")
        # 提示走一次性 cookie（URL 不再带中文 msg），跟随后在页面上就能看到
        self.assertIn("只支持", resp.text)

        resp = self.client.post(
            "/settings/voice/clip", data={"kind": "no-such-kind"}, follow_redirects=True
        )
        self.assertIn("未知", resp.text)

    def test_06_three_kinds_are_independent(self):
        for kind, ext in (("order", "mp3"), ("tracking", "wav"), ("refund", "m4a")):
            self.client.post(
                "/settings/voice/clip",
                data={"kind": kind},
                files={"clip": (f"{kind}.{ext}", MP3, "audio/mpeg")},
                follow_redirects=False,
            )
        clips = voice_service.payload()["clips"]
        self.assertTrue(all(clips[k] for k in ("order", "tracking", "refund")))
        self.client.post("/settings/voice/clip/delete", data={"kind": "tracking"}, follow_redirects=False)
        clips = voice_service.payload()["clips"]
        self.assertEqual(clips["tracking"], "")
        self.assertTrue(clips["order"] and clips["refund"], "删一个不影响其它场景")

    # ------------------------------------------------------------ 系统默认人声
    def test_08_default_clips_available_and_served(self):
        """没自定义录音时，三个场景都有一套系统自带人声（可直接播放）。"""
        self.assertTrue(settings.voice_default_clip)
        clips = voice_service.payload()["default_clips"]
        self.assertTrue(all(clips[k] for k in ("order", "tracking", "refund")), clips)
        for kind in ("order", "tracking", "refund"):
            self.assertTrue(
                (voice_service.STATIC_VOICE_DIR / voice_service.DEFAULT_CLIP_FILES[kind]).exists(),
                f"{kind} 的默认人声文件要随包带上",
            )
        url = clips["order"].split("?")[0]
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("audio", resp.headers.get("content-type", ""))

    def test_09_default_clip_skipped_when_text_customized(self):
        """文案被改过就不播自带录音（自带录音是固定一句话），回落到合成音。"""
        self.assertTrue(voice_service.default_clip_url("order"))
        settings.voice_order_text = "订单 {订单号} 到啦，请及时处理"
        try:
            self.assertEqual(voice_service.default_clip_url("order"), "")
            # 没改过的场景仍然用得上自带人声
            self.assertTrue(voice_service.default_clip_url("tracking"))
        finally:
            settings.voice_order_text = voice_service.DEFAULT_TEXTS["order"]

    def test_10_default_clip_can_be_switched_off(self):
        """关掉开关就完全回到旧的「浏览器合成音」策略。"""
        settings.voice_default_clip = False
        try:
            clips = voice_service.payload()["default_clips"]
            self.assertEqual([clips[k] for k in ("order", "tracking", "refund")], ["", "", ""])
            self.assertFalse(voice_service.payload()["default_clips_enabled"])
        finally:
            settings.voice_default_clip = True

    def test_07_event_text_unaffected_by_clip(self):
        """配了录音后事件仍照常落库（前端据此决定是否播录音）。"""
        voice_service.save_clip("order", "a.mp3", MP3)
        with SessionLocal() as s:
            order = Order(order_no="CLIP-1", product_name="亚朵枕头", created_at=now(), updated_at=now())
            s.add(order)
            s.commit()
            event = voice_service.order_event(s, order)
            s.refresh(order)
        self.assertIsNotNone(event)
        pending = self.client.get("/api/voice/pending?since=0").json()
        self.assertEqual(pending["events"][-1]["kind"], "order")
        self.assertEqual(pending["events"][-1]["text"], voice_service.template_of("order"))


if __name__ == "__main__":
    unittest.main()
