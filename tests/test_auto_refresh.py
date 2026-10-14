"""页面自动刷新：首页 / 订单页停留时定时拉最新数据。

业务口径（2026-09）：
  1 只对「数据总览（/）」和「订单归档（/orders）」两页生效，其余页面（大多在填表）不自动刷；
  2 频率系统设置页可改（默认 60 秒），0 = 关闭；
  3 只改刷新频率的那次提交，不能把飞书凭证清空。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载测试包，确保测试库配置先于 app 模块

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import settings_store  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")


class AutoRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        self._saved_sec = settings.auto_refresh_sec
        self._saved_app_id = settings.feishu_app_id
        self._saved_chat_ids = list(settings.feishu_chat_ids)
        settings.feishu_app_id = "cli_test_keepme"
        settings.feishu_chat_ids = ["oc_test"]
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)

    def tearDown(self) -> None:
        settings.auto_refresh_sec = self._saved_sec
        settings.feishu_app_id = self._saved_app_id
        settings.feishu_chat_ids = self._saved_chat_ids

    def test_01_auto_refresh_only_on_home(self):
        """只有首页带刷新标记和倒计时徽章；订单页已关闭（列表长、常勾选核对）。"""
        settings.auto_refresh_sec = 60
        body = self.client.get("/").text
        self.assertIn('data-auto-refresh="60"', body, "首页要自动刷新")
        self.assertIn('id="autoRefreshChip"', body, "首页顶栏有倒计时")
        for path in ("/orders", "/finance"):
            self.assertNotIn(
                "data-auto-refresh",
                self.client.get(path).text,
                f"{path} 不该自动刷新",
            )

    def test_02_zero_disables_refresh(self):
        """设成 0 = 关闭。"""
        settings.auto_refresh_sec = 0
        self.assertNotIn("data-auto-refresh", self.client.get("/").text)

    def test_03_change_frequency_takes_effect(self):
        """设置页改成 30 秒 / 2 分钟，立刻反映到页面上。"""
        for sec in (30, 120):
            settings.auto_refresh_sec = sec
            self.assertIn(f'data-auto-refresh="{sec}"', self.client.get("/").text)

    def test_04_save_only_refresh_keeps_feishu_credential(self):
        """只提交刷新频率：频率生效，飞书凭证不能被清空。"""
        with mock.patch.object(settings_store, "is_writable", return_value=False):
            resp = self.client.post(
                "/settings/save",
                data={"auto_refresh_sec": "30"},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303, resp.text)
        self.assertEqual(settings.auto_refresh_sec, 30, "保存后立即生效")
        self.assertEqual(settings.feishu_app_id, "cli_test_keepme", "凭证不能被清掉")
        self.assertEqual(settings.feishu_chat_ids, ["oc_test"])

    def test_05_settings_page_offers_choices(self):
        """设置页能把频率改成关闭。"""
        page = self.client.get("/settings").text
        self.assertIn('name="auto_refresh_sec"', page)
        self.assertIn("关闭自动刷新", page)
        self.assertIn("页面自动刷新", page)

    def test_06_frontend_has_guard_logic(self):
        """前端脚本带三道保护：不在前台 / 正忙 / 变慢都别硬刷。"""
        for keyword in (
            "data-auto-refresh",
            "document.hidden",
            'input[name="members"]:checked',
            "MAX_GAP",
        ):
            self.assertIn(keyword, JS, f"前端要处理 {keyword}")
