"""渔货栈 · 全站 UI 统一规范（2026-10-12）。

用户反馈：「我要下单页顶部还是有点乱，全网页的组件对齐也不统一，字体大小统一合适的」。
根因两类：
1) 页面用了 base.html 里**没定义**的类（.sec-title 是从渔枢系统那边照抄的、.field-row.auto
   从来没定义过），浏览器按裸元素渲染 → 排版乱；
2) 各页自己写字号（12.5 / 13.5 / 14.5 / 11.5 / 10px）与内边距，组件之间对不齐。

所以 base.html 里落了一套唯一的字号阶梯与组件口径，这里把「不许再退化」钉住：
- base 必须定义阶梯 token 与 .sec-title / .field-row.auto；
- 模板里不许再出现小数号；
- 引用的 CSS 变量必须在 base 或本页声明过（变量没定义 = 整条样式失效）。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

TPL = ROOT / "templates"


def _templates() -> list[Path]:
    return sorted(list(TPL.glob("*.html")) + list((TPL / "merchant").glob("*.html")))


class UiSpecTest(unittest.TestCase):
    """base.html 里的统一口径要在（改坏了页面会立刻退回裸样式）。"""

    def setUp(self) -> None:
        self.base = (TPL / "base.html").read_text(encoding="utf-8")

    def test_10_font_ladder_defined(self):
        """字号阶梯：六个 token + 控件高 + 区块内边距。"""
        for token in (
            "--fs-title", "--fs-block", "--fs-body",
            "--fs-label", "--fs-help", "--fs-micro",
            "--ctl-h", "--ctl-h-sm", "--pad-hd", "--pad-bd",
        ):
            self.assertRegex(self.base, r"%s\s*:" % re.escape(token), f"缺少字号 token {token}")

    def test_20_shared_classes_defined(self):
        """.sec-title / .field-row.auto / .page-actions / .blk 都要在 base 里有定义。"""
        for cls in (".sec-title", ".field-row.auto", ".page-actions", ".blk, .on-block"):
            self.assertIn(cls, self.base, f"{cls} 没定义（页面用了就是裸样式）")

    def test_30_button_and_input_same_height(self):
        """按钮与输入框同高：并排时不再差 2px。"""
        self.assertRegex(self.base, r"\.btn\s*\{[^}]*min-height:\s*var\(--ctl-h\)")
        self.assertRegex(self.base, r"--ctl-h:\s*38px")
        self.assertRegex(self.base, r"--ctl-h-sm:\s*32px")

    def test_40_no_decimal_font_sizes(self):
        """模板里不许再出现 12.5 / 13.5 / 14.5 / 11.5 这类小数号。"""
        bad: list[str] = []
        for f in _templates():
            for m in re.finditer(r"font-size:\s*(\d+\.\d+)px", f.read_text(encoding="utf-8")):
                bad.append(f"{f.name}:{m.group(1)}px")
        self.assertEqual([], bad, f"字号要落回阶梯（24/14/13/12/11），这些是小数字号: {bad}")

    def test_50_css_variables_are_declared(self):
        """引用的 CSS 变量必须声明过：没声明 = 整条样式失效（边框会变黑）。"""
        base_def = set(re.findall(r"--([\w-]+)\s*:", self.base))
        missing: list[str] = []
        for f in _templates():
            text = f.read_text(encoding="utf-8")
            own = set(re.findall(r"--([\w-]+)\s*:", text))
            for name in set(re.findall(r"var\(--([\w-]+)", text)):
                if name not in base_def and name not in own:
                    missing.append(f"{f.name} -> {name}")
        self.assertEqual([], missing, f"这些变量没声明: {missing}")


class OrderNewHeaderTest(unittest.TestCase):
    """「我要下单」页顶部：走全站页头，不再用没定义的 .sec-title。"""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post(
            "/login", data={"username": "admin", "password": "xianyu2026"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text[:200]

    def test_10_page_uses_shared_header(self):
        page = self.client.get("/self/orders/new")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn('<div class="page-header">', html)
        self.assertIn("<h1>我要下单</h1>", html)
        self.assertIn("page-actions", html)
        # 正文里不能再出现没定义的 .sec-title（base 的 CSS 里有这个词，只查正文）
        body = html.split("<main", 1)[-1]
        self.assertNotIn('class="sec-title"', body)

    def test_20_other_pages_share_the_header(self):
        """其余页面同样走 .page-header（页头骨架全站一份）。"""
        for path in ("/self/orders", "/self/purchases", "/self/cart",
                     "/self/shop-goods", "/self/shops", "/self/addresses"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertIn('<div class="page-header">', page.text, path)


if __name__ == "__main__":
    unittest.main()
