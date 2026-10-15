"""图片地址归一化（2026-10-15 修线上「渔枢商品详情页的图全没了」）。

库里 ``products.image_path`` 存的是**站内地址** ``/media/products/xxx.jpg``，
而 ``media_url()`` 以前只认「media_path 之下的绝对路径」——前两种写法会被
``relative_to`` 抛 ValueError，退化成「只剩文件名」，于是
``/thumb/12889ec1eff5487a.jpg?w=800`` 一律 404，详情页整片图消失。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  测试库配置要在任何 app 模块之前生效

from app.config import settings  # noqa: E402
from app.utils import media_url, thumb_url  # noqa: E402


class MediaUrlTest(unittest.TestCase):
    def test_10_empty(self):
        self.assertEqual(media_url(""), "")
        self.assertEqual(media_url(None), "")

    def test_20_external_passthrough(self):
        for raw in ("https://img.alicdn.com/a.jpg", "http://x/a.jpg",
                    "//x/a.jpg", "data:image/png;base64,AAA"):
            self.assertEqual(media_url(raw), raw)
            self.assertEqual(thumb_url(raw, 800), raw)

    def test_30_in_site_path(self):
        """站内地址原样保留目录层级（以前会被吃掉 products/ 这层 → 404）。"""
        self.assertTrue(media_url("/media/products/a.jpg").startswith(
            "/media/products/a.jpg"), media_url("/media/products/a.jpg"))

    def test_40_relative_path(self):
        self.assertTrue(media_url("products/a.jpg").startswith(
            "/media/products/a.jpg"), media_url("products/a.jpg"))

    def test_50_absolute_path(self):
        raw = str(Path(settings.media_path) / "products" / "a.jpg")
        self.assertTrue(media_url(raw).startswith("/media/products/a.jpg"),
                        media_url(raw))

    def test_60_thumb_url(self):
        url = thumb_url("products/a.jpg", 800)
        self.assertTrue(url.startswith("/thumb/products/a.jpg?"), url)
        self.assertIn("w=800", url)


if __name__ == "__main__":
    unittest.main()
