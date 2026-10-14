"""按需缩略图 /thumb/...（2026-10-15）。

守的是三件事，都是「图多了页面就慢」这条线上不能退的东西：
 1. 缩略图必须比原图小一个数量级 —— 列表 / 购物车只占几十像素，
    再把 2MB 原图发出去等于白做；
 2. 只能取 media 目录内的文件 —— 路径穿越必须挡住；
 3. 取不到 / 不是图就 404 或原样返回 —— 不许在页面上开天窗，也不许 500。
"""
from __future__ import annotations

import shutil
import unittest
from pathlib import Path

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402


class ThumbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.media = Path(settings.media_path)
        cls.target = cls.media / "products" / "thumb_case_big.jpg"
        cls.target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2400, 1600), (118, 150, 96)).save(cls.target, quality=95)
        init_db()
        ensure_default_admin()
        cls.client = TestClient(create_app())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.target.unlink(missing_ok=True)

    def setUp(self) -> None:
        self.client.cookies.set(COOKIE_NAME, make_session(1)[0])

    def tearDown(self) -> None:
        shutil.rmtree(self.media / "_thumbs", ignore_errors=True)

    def test_10_thumb_much_smaller_than_original(self) -> None:
        raw = self.target.stat().st_size
        res = self.client.get("/thumb/products/thumb_case_big.jpg?w=200")
        self.assertEqual(res.status_code, 200, "能拿到缩略图")
        self.assertIn("webp", res.headers.get("content-type", ""))
        self.assertLess(len(res.content), raw // 5, "缩略图要比原图小一个数量级")

    def test_20_traversal_blocked(self) -> None:
        res = self.client.get("/thumb/../../etc/passwd?w=200")
        self.assertIn(res.status_code, (404, 403, 400), "不能读到 media 目录外的文件")

    def test_30_missing_file_is_404(self) -> None:
        res = self.client.get("/thumb/products/definitely_not_here.jpg?w=200")
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
