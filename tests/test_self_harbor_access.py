"""学员账号（端只勾了「渔货栈」）也要能进货源广场（2026-10-11 线上报障）。

现场：liuyuhui（学员组，`portals="self"`）登录后被拦在自己的首页 /self/harbor 上，
浏览器里只有一串乱码 JSON。根因两条：
① `/self/harbor`、`/self/` 被归成「渔枢系统端」的页面 ——
   只勾了「渔货栈」的学员账号进不去（选货页本来就是两端共用：学员选货、渔货栈账号铺货）；
② `HTTPException` 的中文 detail 走 JSON 且 `Content-Type` 没带 charset，
   浏览器按本地编码解就成乱码，用户看不清为什么进不去。

本文件盯这两条：路径归属（`needed_portal` 空串 = 不限端）+ 403 的两种呈现
（浏览器给人话 HTML，接口给带 charset 的 JSON）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.hub_bridge import needed_portal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.portals import PORTAL_SELF  # noqa: E402
from app.services.users import create_user  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

PASSWORD = "xianyu2026"
SELF_ONLY = "t_harbor_self_only"   # 学员：只勾「渔货栈」端
HUB_ONLY = "t_harbor_hub_only"     # 只勾「渔枢系统」端
ALL_USERS = (SELF_ONLY, HUB_ONLY)


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(User).filter(User.username.in_(ALL_USERS)).delete(synchronize_session=False)
        s.commit()


def _make_users() -> None:
    with SessionLocal() as s:
        create_user(s, SELF_ONLY, PASSWORD, display_name="测试-学员", portals="self")
        create_user(s, HUB_ONLY, PASSWORD, display_name="测试-渔枢", portals="hub")
        s.commit()


class SelfHarborAccessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()

    def setUp(self):
        _wipe()
        _make_users()
        self.client = TestClient(app)

    def tearDown(self):
        _wipe()

    def _login(self, username: str) -> None:
        resp = self.client.post(
            "/login",
            data={"username": username, "password": PASSWORD},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, resp.text[:200])

    # ------------------------------------------------- 路径归属
    def test_10_shared_pages_are_open_to_both_portals(self):
        """选货页与首页不限端：两端账号都得进（其余 /self 页面仍只给渔货栈）。"""
        for path in ("/self", "/self/", "/self/harbor"):
            self.assertEqual(needed_portal(path), "", f"{path} 应该不限端")
        # 货源广场下的子路径（榜单 / 采购节点 / 三级类目接口）跟着首页一起放开
        self.assertEqual(needed_portal("/self/harbor/leaves"), "")
        self.assertEqual(needed_portal("/self/orders"), PORTAL_SELF, "订单页只给渔货栈")
        self.assertEqual(needed_portal("/self/shop-goods"), PORTAL_SELF)

    # ------------------------------------------------- 学员（只勾渔货栈）
    def test_20_student_self_only_can_open_harbor(self):
        """线上那条报障：只勾「渔货栈」的账号，登录后首页必须打得开。"""
        self._login(SELF_ONLY)

        page = self.client.get("/self/harbor")
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn("货源", page.text)

        # 首页只是跳转：307 到货源广场，不拦
        home = self.client.get("/self/", follow_redirects=False)
        self.assertEqual(home.status_code, 307, home.text[:200])
        self.assertEqual(home.headers["location"], "/self/harbor")

        # 自营页面照旧（「我的上架」页 2026-10-11 已下线，走的是 404）
        self.assertEqual(self.client.get("/self/selection").status_code, 404)
        self.assertEqual(self.client.get("/self/orders").status_code, 200)

    # ------------------------------------------------- 只勾渔枢系统的账号
    def test_30_hub_only_account_can_open_harbor(self):
        """只勾「渔枢系统」的账号也能进选货页（以前就是这条路）。"""
        self._login(HUB_ONLY)
        self.assertEqual(self.client.get("/self/harbor").status_code, 200)
        # 但渔货栈自己的页面（订单 / 采购）仍旧不给
        resp = self.client.get("/self/orders")
        self.assertEqual(resp.status_code, 403, resp.text[:200])

    def test_40_forbidden_json_has_charset(self):
        """接口拿到的是 JSON，且带 charset（中文提示不再被解成乱码）。"""
        self._login(HUB_ONLY)
        resp = self.client.get("/self/orders", headers={"accept": "*/*"})
        self.assertEqual(resp.status_code, 403)
        ctype = resp.headers["content-type"]
        self.assertIn("charset=utf-8", ctype.lower(), ctype)
        self.assertIn("访问权限", resp.json()["detail"])

    def test_50_forbidden_in_browser_is_html(self):
        """浏览器导航（Accept 带 text/html）拿到人话页面，而不是一坨 JSON。"""
        self._login(HUB_ONLY)
        resp = self.client.get("/self/orders", headers={"accept": "text/html,application/xhtml+xml"})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("403", resp.text)
        self.assertIn("没有访问权限", resp.text)
        self.assertIn("回到首页", resp.text)
