"""系统设置页：飞书凭证可在线修改并写回 .env（测试结束会还原原文件）。"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: F401

from fastapi.testclient import TestClient

from app.config import settings
from app.db import init_db
from app.main import app
from app.services import settings_store as store
from app.web.auth import ensure_default_admin

ADMIN = {"username": "admin", "password": "xianyu2026"}

BACKUP = store.ENV_PATH.read_text(encoding="utf-8") if store.ENV_PATH.exists() else ""
ORIG = (settings.feishu_app_id, settings.feishu_app_secret, list(settings.feishu_chat_ids))


def _restore() -> None:
    store.ENV_PATH.write_text(BACKUP, encoding="utf-8")
    settings.feishu_app_id, settings.feishu_app_secret = ORIG[0], ORIG[1]
    settings.feishu_chat_ids = list(ORIG[2])


class EnvStoreTest(unittest.TestCase):
    def tearDown(self):
        _restore()

    def test_mask_secret(self):
        self.assertEqual(store.mask_secret(""), "")
        self.assertTrue(store.mask_secret("CukJM8bcyrKD588AaYTCmPCmGIJG6P81").startswith("CukJM8"))
        self.assertTrue(store.mask_secret("CukJM8bcyrKD588AaYTCmPCmGIJG6P81").endswith("P81"))

    def test_write_updates_keeps_other_lines(self):
        store.write_updates({"FEISHU_APP_ID": "cli_unit_test"})
        text = store.ENV_PATH.read_text(encoding="utf-8")
        self.assertIn("FEISHU_APP_ID=cli_unit_test", text)
        self.assertIn("OCR_PROVIDER=", text)          # 其它配置不受影响
        self.assertEqual(text.count("FEISHU_APP_ID="), 1)


class SettingsPageTest(unittest.TestCase):
    def tearDown(self):
        _restore()

    def test_save_via_web(self):
        init_db()
        ensure_default_admin()
        client = TestClient(app)
        # 系统设置是管理员专属：先登录
        self.assertEqual(client.post("/login", data=ADMIN, follow_redirects=False).status_code, 303)
        page = client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn("FEISHU_APP_ID", page.text)

        resp = client.post(
            "/settings/save",
            data={
                "feishu_app_id": "cli_web_test",
                "feishu_app_secret": "CukJM8bc****P81",   # 掩码 = 不修改
                "feishu_chat_ids": "oc_web_test",
            },
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))
        self.assertEqual(settings.feishu_app_id, "cli_web_test")
        self.assertEqual(settings.feishu_chat_ids, ["oc_web_test"])
        self.assertIn("FEISHU_APP_ID=cli_web_test", store.ENV_PATH.read_text(encoding="utf-8"))
        # 掩码提交不应改动密钥
        self.assertNotIn("****", store.ENV_PATH.read_text(encoding="utf-8"))


class MessageCenterRouteTest(unittest.TestCase):
    """消息中心是自己的一级菜单：地址 /message-center，不再挂在系统设置下面。"""

    KEY_RE = r'name="([A-Za-z0-9_]+__[a-z_]+)"'

    def setUp(self):
        init_db()
        ensure_default_admin()
        self.client = TestClient(app)
        self.assertEqual(
            self.client.post("/login", data=ADMIN, follow_redirects=False).status_code, 303
        )

    def tearDown(self):
        _restore()

    def test_page_is_standalone(self):
        page = self.client.get("/message-center")
        self.assertEqual(page.status_code, 200)
        self.assertIn("保存发送方式", page.text)
        # 红框里那两块（右上「返回系统设置」+ 系统设置 tab 行）都去掉了
        self.assertNotIn("返回系统设置", page.text)
        self.assertIn('action="/message-center"', page.text)

    def test_legacy_settings_path_redirects(self):
        resp = self.client.get("/settings/messages", follow_redirects=False)
        self.assertEqual(resp.status_code, 301)
        self.assertTrue(resp.headers["location"].endswith("/message-center"))
        # 老页面缓存里的表单提交也要能落地（307 保留 POST 与表单内容）
        posted = self.client.post("/settings/messages", data={}, follow_redirects=False)
        self.assertEqual(posted.status_code, 307)
        self.assertTrue(posted.headers["location"].endswith("/message-center"))

    def test_save_channels(self):
        page = self.client.get("/message-center")
        names = re.findall(self.KEY_RE, page.text)
        self.assertTrue(names, "消息中心页面上应该能解析出每条消息的发送方式勾选框")
        # 原样提交页面上的勾选：既不改变现有配置，又能覆盖保存接口
        checked = set(
            re.findall(self.KEY_RE + r"\s+value=\"1\"\s*\n?\s*checked", page.text)
        )
        resp = self.client.post(
            "/message-center",
            data={name: "1" for name in names if name in checked},
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))
        # 保存后回到消息中心自己（后面可能带一条 msg 提示，所以用包含判断）
        self.assertIn("/message-center", resp.headers["location"])
        self.assertNotIn("/settings", resp.headers["location"])


if __name__ == "__main__":
    unittest.main()
