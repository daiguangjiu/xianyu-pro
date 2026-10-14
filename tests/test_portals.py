"""端（Portal）授权：管理员默认全端，子账号按勾选。

业务口径：
  1 端分两种：渔枢系统（hub，订单中台 + 货源上架）/ 渔货栈（self，供应链运营）；
  2 管理员一律全端 —— 不因为漏配就进不去某个端；
  3 子账号由管理员在「用户管理」里勾选，至少勾一个端；
  4 老账号没有 portals 值时按「渔枢系统」处理，升级不影响登录。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.portals import (  # noqa: E402
    ALL_PORTALS,
    PORTAL_HUB,
    PORTAL_KEYS,
    PORTAL_SELF,
    PORTAL_WMS,
    join_portals,
    normalize_portals,
    portal_names,
)
from app.services import users as users_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class PortalValueTest(unittest.TestCase):
    """端字符串的解析：库里存 `hub,self`，读出来永远是有序、合法、非空的元组。"""

    def test_empty_falls_back_to_hub(self) -> None:
        # 空值按默认端（渔枢系统）处理，任何账号至少能进一个端
        self.assertEqual(normalize_portals(""), (PORTAL_HUB,))
        self.assertEqual(normalize_portals(None), (PORTAL_HUB,))

    def test_order_and_dedup(self) -> None:
        # 无论怎么勾、顺序怎么乱，都按端定义顺序去重
        self.assertEqual(normalize_portals("hub,self"), (PORTAL_HUB, PORTAL_SELF))
        self.assertEqual(normalize_portals("self,self,hub"), (PORTAL_HUB, PORTAL_SELF))

    def test_invalid_keys_dropped(self) -> None:
        self.assertEqual(normalize_portals("self,xxx"), (PORTAL_SELF,))
        self.assertEqual(normalize_portals("xxx"), (PORTAL_HUB,))

    def test_chinese_comma(self) -> None:
        self.assertEqual(normalize_portals("self，hub"), (PORTAL_HUB, PORTAL_SELF))

    def test_join_portals(self) -> None:
        # 全子系统 = 依次写下三个键（顺序按 app/portals.py 的定义，不按传进来的顺序）
        self.assertEqual(join_portals(["wms", "hub", PORTAL_SELF]), ALL_PORTALS)
        self.assertEqual(join_portals(list(reversed(PORTAL_KEYS))), ALL_PORTALS)
        self.assertEqual(join_portals([]), PORTAL_HUB)

    def test_portal_names(self) -> None:
        self.assertEqual(portal_names(ALL_PORTALS), "渔枢系统 · 渔货栈 · 渔集")


class UserPortalTest(unittest.TestCase):
    """账号维度的端授权：管理员全端，子账号按勾选。"""

    def setUp(self) -> None:
        init_db()
        self.admin = ensure_default_admin()
        with SessionLocal() as s:
            s.query(User).filter(User.username != self.admin.username).delete()
            s.commit()

    def _get(self, username: str) -> User:
        with SessionLocal() as s:
            return s.scalar(select(User).where(User.username == username))

    def test_admin_has_all_portals(self) -> None:
        admin = self._get("admin")
        self.assertEqual(admin.portal_keys, (PORTAL_HUB, PORTAL_SELF, PORTAL_WMS))
        self.assertTrue(admin.has_portal(PORTAL_HUB))
        # 2026-10-04 起检测仓升级为平台子系统之一，管理员默认也能进
        self.assertTrue(admin.has_portal(PORTAL_WMS))

    def test_sub_account_defaults_to_hub(self) -> None:
        users_service.create_user(SessionLocal(), "sub01", "xianyu2026")
        sub = self._get("sub01")
        self.assertEqual(sub.portal_keys, (PORTAL_HUB,))
        self.assertFalse(sub.has_portal(PORTAL_SELF))

    def test_create_sub_with_both_portals(self) -> None:
        users_service.create_user(
            SessionLocal(), "sub02", "xianyu2026", portals=f"{PORTAL_HUB},{PORTAL_SELF}"
        )
        self.assertEqual(self._get("sub02").portal_keys, (PORTAL_HUB, PORTAL_SELF))

    def test_create_admin_always_all_portals(self) -> None:
        # 建号时就算没勾端，管理员也应该拿到全部端
        users_service.create_user(SessionLocal(), "sub03", "xianyu2026", role="admin")
        self.assertEqual(self._get("sub03").portal_keys, (PORTAL_HUB, PORTAL_SELF, PORTAL_WMS))

    def test_legacy_user_without_portals_value(self) -> None:
        """老账号升级上来没有 portals 列值：按渔枢系统处理，不会出现「哪个端都进不去」。"""
        with SessionLocal() as s:
            s.add(User(username="legacy", password_hash="x", role="user", portals=""))
            s.commit()
        self.assertEqual(self._get("legacy").portal_keys, (PORTAL_HUB,))

    def test_set_portals(self) -> None:
        users_service.create_user(SessionLocal(), "sub04", "xianyu2026")
        sub = self._get("sub04")
        row = users_service.set_portals(SessionLocal(), sub.id, [PORTAL_SELF])
        self.assertEqual(row.portals, PORTAL_SELF)
        self.assertTrue(row.has_portal(PORTAL_SELF))
        self.assertFalse(row.has_portal(PORTAL_HUB))

    def test_set_portals_rejects_empty(self) -> None:
        users_service.create_user(SessionLocal(), "sub05", "xianyu2026")
        sub = self._get("sub05")
        with self.assertRaises(ValueError):
            users_service.set_portals(SessionLocal(), sub.id, [])

    def test_set_portals_rejects_admin(self) -> None:
        with self.assertRaises(ValueError):
            users_service.set_portals(SessionLocal(), self.admin.id, [PORTAL_SELF])

    def test_role_change_syncs_portals(self) -> None:
        users_service.create_user(SessionLocal(), "sub06", "xianyu2026")
        sub = self._get("sub06")
        users_service.set_role(SessionLocal(), sub.id, "admin")
        self.assertEqual(self._get("sub06").portal_keys, (PORTAL_HUB, PORTAL_SELF, PORTAL_WMS))
        # 降回子账号时把端收回来，避免留着全端记录变相越权
        users_service.set_role(SessionLocal(), sub.id, "user")
        self.assertEqual(self._get("sub06").portal_keys, (PORTAL_HUB,))


class UsersPagePortalsTest(unittest.TestCase):
    """用户管理页：建号 / 改端都走页面，管理员行只显示「全部端」。"""

    def setUp(self) -> None:
        init_db()
        self.admin = ensure_default_admin()
        with SessionLocal() as s:
            s.query(User).filter(User.username != self.admin.username).delete()
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def _get(self, username: str) -> User:
        with SessionLocal() as s:
            return s.scalar(select(User).where(User.username == username))

    def test_page_shows_portal_options(self) -> None:
        page = self.client.get("/users")
        self.assertEqual(page.status_code, 200)
        self.assertIn("渔枢系统", page.text)
        self.assertIn("渔货栈", page.text)
        self.assertIn("渔集", page.text)   # 2026-10-04 起检测仓对外叫渔集
        # 管理员行：显示「全部端」而不是勾选框
        self.assertIn("全部子系统", page.text)

    def test_create_account_with_portals(self) -> None:
        resp = self.client.post(
            "/users",
            data={
                "username": "hubuser",
                "password": "xianyu2026",
                "role": "user",
                "display_name": "运营小张",
                "portals": [PORTAL_SELF, PORTAL_HUB],
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self._get("hubuser").portal_keys, (PORTAL_HUB, PORTAL_SELF))

    def test_update_portals_from_page(self) -> None:
        self.client.post(
            "/users",
            data={"username": "sub07", "password": "xianyu2026", "role": "user", "portals": [PORTAL_HUB]},
            follow_redirects=False,
        )
        sub = self._get("sub07")
        resp = self.client.post(
            f"/users/{sub.id}/portals",
            data={"portals": [PORTAL_SELF]},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self._get("sub07").portal_keys, (PORTAL_SELF,))

    def test_admin_portals_not_editable(self) -> None:
        page = self.client.post(
            f"/users/{self.admin.id}/portals",
            data={"portals": [PORTAL_HUB]},
            follow_redirects=True,
        )
        self.assertIn("管理员默认拥有全部子系统", page.text)


class SubsystemSwitchTest(unittest.TestCase):
    """子系统切换（2026-10-04 用户拍板）：

    1. 检测仓升级为平台子系统之一，与渔枢系统 / 渔货栈平级，入口在左上角切换面板；
    2. 平台侧边栏不再有「其它系统 / 货品检测仓」那一栏；
    3. 渔货栈里不放切换面板（学员与自营团队不需要切回渔枢系统）。
    """

    def setUp(self) -> None:
        init_db()
        self.admin = ensure_default_admin()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def test_hub_switch_panel_lists_three_subsystems(self) -> None:
        html = self.client.get("/").text
        self.assertIn("切换子系统", html)
        self.assertIn("portal-panel", html)
        for item in PORTAL_KEYS:
            self.assertIn(f'href="/portal/{item}"', html, f"面板里应有 {item} 的入口")
        for name in ("渔枢系统", "渔货栈", "渔集"):
            self.assertIn(name, html)
        # 当前所在的那个打「当前」标
        self.assertIn("当前", html)

    def test_hub_sidebar_has_no_wms_entry(self) -> None:
        html = self.client.get("/").text
        self.assertNotIn("其它系统", html)
        self.assertNotIn("渔集（独立系统）", html)
        from app.web.nav import NAV

        def _urls(items):
            for it in items:
                if it.url:
                    yield it.url
                yield from _urls(it.children)

        urls = list(_urls([it for sec in NAV for it in sec.items]))
        self.assertEqual([u for u in urls if u.startswith("/wms")], [])

    def test_self_header_has_no_switch(self) -> None:
        page = self.client.get("/self/harbor")
        self.assertEqual(page.status_code, 200, page.text[:200])
        # 断标记不能搜裸词：样式表里就有 .portal-panel，要搜标签上的 class
        self.assertNotIn('class="portal-panel"', page.text, "渔货栈顶栏不该有子系统切换")
        self.assertNotIn("切换子系统", page.text, "渔货栈里不该出现子系统切换面板")
        self.assertNotIn('href="/portal/', page.text, "渔货栈里不该能直接切回渔枢系统")

    def test_current_subsystem_always_listed(self) -> None:
        """人在哪个子系统，那个就必须在列表里（检测仓自管权限，账号可能没勾 wms）。"""
        from app.portals import switch_options

        with SessionLocal() as s:
            sub = User(username="wmsonly", password_hash="x", role="user", portals=PORTAL_HUB)
            s.add(sub)
            s.commit()
            keys = [o["key"] for o in switch_options(sub, "/wms")]
        self.assertIn(PORTAL_WMS, keys)
        self.assertIn(PORTAL_HUB, keys)
        self.assertNotIn(PORTAL_SELF, keys, "没勾渔货栈就不该出现在面板里")
        # 顺序按 app/portals.py 的定义，不按拼出来的先后
        self.assertEqual(keys, [PORTAL_HUB, PORTAL_WMS])

    def test_switch_to_wms_lands_on_wms(self) -> None:
        resp = self.client.get("/portal/wms", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/wms")


if __name__ == "__main__":
    unittest.main()
