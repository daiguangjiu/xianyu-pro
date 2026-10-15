"""静态资源 Cache-Control（2026-10-15 提速）。

浏览器缓存由 ``app/web/auth.py`` 的静态缓存中间件统一管：
带 ``?v=`` 版本号的 /static 与 /media（商品图引用都带 mtime 版本号）→ 30 天 immutable；
不带版本号的 → 1 小时；HTML 页面照旧 no-store。以前 StaticFiles 只发 ETag，
浏览器每次渲染都要回源问一遍 304 —— 详情页几十张图就是几十次空往返。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401

from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class StaticCacheTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_10_static_with_version_immutable(self):
        resp = self.client.get("/static/http.js?v=test")
        self.assertEqual(resp.status_code, 200)
        cc = resp.headers.get("cache-control", "")
        self.assertIn("max-age=2592000", cc)
        self.assertIn("immutable", cc)

    def test_20_media_without_version_one_hour(self):
        from app.config import settings

        p = settings.media_path / "cards"
        p.mkdir(parents=True, exist_ok=True)
        f = p / "cache-check-test.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        try:
            resp = self.client.get("/media/cards/cache-check-test.png")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("max-age=3600", resp.headers.get("cache-control", ""))
        finally:
            f.unlink(missing_ok=True)

    def test_30_pages_still_no_store(self):
        """页面缓存策略不变：登录页照旧 no-store。"""
        resp = self.client.get("/login", follow_redirects=False)
        self.assertIn("no-store", resp.headers.get("cache-control", ""))


if __name__ == "__main__":
    unittest.main()
