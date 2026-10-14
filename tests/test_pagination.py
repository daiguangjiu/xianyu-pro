"""分页体验（2026-09）：窗口式页码 + 上一页 / 下一页 + 跳页，页码越界自动夹取。

背景：日志、消息这类列表动辄几百页，老实现把 1..236 全列出来，整屏都是页码还找不到位置；
现在统一走 templates/_pager.html 的 `pager` 宏：首 / 末页 + 当前页前后各 2 页 + 省略号，
并保留筛选条件；后端 page 越界（0 / 负数 / 太大）一律夹到有效范围，不出现空列表。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import OperationLog  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from app.web.pages import templates  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
KEY = "分页用例"  # 唯一关键词：把用例数据与库里的其它日志隔开


class PagerMacroTest(unittest.TestCase):
    """pager 宏本身的渲染：窗口、省略号、首末页、禁用态、跳页。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.pager = templates.env.get_template("_pager.html").module.pager

    def test_01_middle_page_is_windowed(self):
        html = self.pager(120, 236, "/logs", {"q": "abc", "action": ""}, 7060)
        self.assertIn("第 120 / 236 页", html)
        self.assertIn("共 7060 条", html)
        # 当前页前后各 2 页（当前页是文字高亮，不带链接）
        for p in (118, 119, 121, 122):
            self.assertIn(f"page={p}", html)
        self.assertIn('aria-current="page">120<', html)
        self.assertNotIn('page=117"', html, "窗口外的页码不该出现")
        self.assertNotIn('page=123"', html)
        # 首末页 + 省略号
        self.assertIn('page=1"', html)
        self.assertIn('page=236"', html)
        self.assertEqual(html.count("…"), 2)
        # 上一页 / 下一页与跳页，并保留筛选条件
        self.assertIn('rel="prev"', html)
        self.assertIn('rel="next"', html)
        self.assertIn('name="page"', html)
        self.assertIn("q=abc", html)

    def test_02_edges_and_small_page_count(self):
        first = self.pager(1, 236, "/logs", {})
        self.assertNotIn('rel="prev"', first, "第一页没有上一页")
        self.assertIn('rel="next"', first)
        self.assertIn('aria-current="page">1<', first)

        last = self.pager(236, 236, "/logs", {})
        self.assertNotIn('rel="next"', last, "最后一页没有下一页")
        self.assertIn('rel="prev"', last)
        self.assertIn('aria-current="page">236<', last)

        small = self.pager(2, 5, "/orders", {})
        self.assertNotIn("…", small, "页数不多时全部列出，不要省略号")
        self.assertNotIn('name="page"', small, "页数不多时不显示跳页框")
        for p in (1, 3, 4, 5):
            self.assertIn(f"page={p}", small)
        self.assertIn('aria-current="page">2<', small)

        # 页码越界：夹到最后一页
        self.assertIn("第 236 / 236 页", self.pager(999, 236, "/logs", {}))
        self.assertIn("第 1 / 236 页", self.pager(0, 236, "/logs", {}))

    def test_03_single_page_shows_count_only(self):
        """只有一页：不给页码 / 跳页框，但条数信息照常显示（列表底部不至于空一块）。"""
        html = self.pager(1, 1, "/logs", {}, 3)
        self.assertIn("共 3 条", html)
        self.assertNotIn("page=", html)
        # 连总条数都没给就什么都不渲染
        self.assertEqual(self.pager(1, 1, "/logs", {}).strip(), "")

    def test_04_layout_styles_exist(self):
        """外壳与「每页 N 条」下拉的样式必须在。

        全局 `select` 是满宽（style.css 里 `select { width: 100% }`），组件里的 form 又是块级，
        少了 .pager-wrap / .pager-size select 这两条，列表页底部的分页条会被撑成上下三行。
        """
        css = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
        self.assertIn(".pager-wrap {", css, "分页条外壳要有 flex 布局")
        self.assertIn(".pager-size {", css)
        size_block = css.split(".pager-size select")[1].split("}")[0]
        self.assertIn("width: auto", size_block, "下拉必须收回自适应宽度，别跟着全局 select 满宽")
        # 只有一页时右侧的条数信息也要靠右，和页码位置对齐
        self.assertIn(".pager-wrap > .pager-info", css)


class PagerPageTest(unittest.TestCase):
    """/logs 等页面：多页时分页条可用，页码越界不报错、不出现空列表。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(OperationLog).filter(OperationLog.detail == KEY).delete()
            # 70 条 → 每页 30 条 → 3 页
            for i in range(70):
                s.add(OperationLog(username="admin", role="admin", action="分页测试", detail=KEY))
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(OperationLog).filter(OperationLog.detail == KEY).delete()
            s.commit()

    def test_01_logs_pagination(self):
        page = self.client.get(f"/logs?q={KEY}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("第 1 / 3 页", page.text)
        self.assertIn("共 70 条", page.text)

        page2 = self.client.get(f"/logs?q={KEY}&page=2")
        self.assertIn("第 2 / 3 页", page2.text)
        # 翻页链接要带上原来的筛选条件（中文会 URL 编码）
        self.assertIn(f"q={quote(KEY)}", page2.text, "翻页要保留关键词筛选")

    def test_02_logs_page_out_of_range(self):
        """地址栏手改页码：夹到有效范围，页面照常渲染（不是空列表）。"""
        high = self.client.get(f"/logs?q={KEY}&page=9999")
        self.assertEqual(high.status_code, 200)
        self.assertIn("第 3 / 3 页", high.text)

        low = self.client.get(f"/logs?q={KEY}&page=0")
        self.assertIn("第 1 / 3 页", low.text)
        negative = self.client.get(f"/logs?q={KEY}&page=-7")
        self.assertIn("第 1 / 3 页", negative.text)

    def test_03_other_lists_render_pager(self):
        """订单 / 消息 / 资金三个列表也要能正常渲染（分页条参数各不一样）。"""
        for path in ("/orders?page=9999", "/messages?page=9999", "/finance?granularity=order&page=2"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, f"{path} 应该能正常打开")
