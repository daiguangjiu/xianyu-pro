"""角色权限（菜单级，2026-09-29 新增）。

一个账号能进哪些菜单：
  · 管理员（users.role == 'admin'）→ 全部菜单（与升级前一致）；
  · 挂了角色（users.role_id）→ 按角色勾选的菜单；
  · 没挂角色的子账号 → 默认菜单（非 admin_only 的那些，也与升级前一致）。

两个内置角色是动态的：admin 跟全部菜单走、staff 跟默认菜单走 ——
以后新增菜单，这两种账号自动就有，不会出现「加了菜单但管理员也看不到」。
数据级权限（Role.data_scope）现在只存不生效，这里只确认字段存得下。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载测试包，确保测试库配置先于 app 模块

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Role, User  # noqa: E402
from app.services import users as users_service  # noqa: E402
from app.web import permissions  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
STAFF = {"username": "xiaoyu01", "password": "xiaoyu2026"}


class RolePermissionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)

    def setUp(self) -> None:
        with SessionLocal() as s:
            # 清掉上次留下的子账号与自定义角色（内置角色留着，播种逻辑不重复建）。
            # 必须「先删账号、再删角色」并分两次提交：账号的 role_id 指着角色，
            # 同一个事务里删角色会被外键拦下。
            for user in s.scalars(select(User).where(User.username != "admin")):
                s.delete(user)
            s.commit()
            for role in s.scalars(select(Role).where(Role.is_system.is_(False))):
                s.delete(role)
            s.commit()
            permissions.ensure_builtin_roles(s)
            permissions.invalidate_cache()

    # ------------------------------------------------------------ 权限计算
    def test_10_builtin_roles_seeded_once(self):
        with SessionLocal() as s:
            self.assertEqual(permissions.ensure_builtin_roles(s), 0, "再播一次不应重复建")
            roles = {r.key: r for r in permissions.role_choices(s)}
        self.assertIn(permissions.ROLE_KEY_ADMIN, roles)
        self.assertIn(permissions.ROLE_KEY_STAFF, roles)
        self.assertTrue(roles[permissions.ROLE_KEY_ADMIN].is_system)

    def test_20_admin_sees_everything(self):
        with SessionLocal() as s:
            admin = s.scalar(select(User).where(User.role == "admin"))
            self.assertIsNone(permissions.menu_keys_for(admin), "管理员不限制菜单")
            self.assertTrue(permissions.can_access_path(admin, "/users"))

    def test_30_staff_without_role_gets_default_menus(self):
        """没挂角色的子账号 = 默认菜单：能看订单商品，看不到解析规则 / 系统设置 / 账号管理。"""
        with SessionLocal() as s:
            row = users_service.create_user(s, "xiaoyu01", "xiaoyu2026", "user", "小鱼", "self")
            keys = permissions.menu_keys_for(row)
        self.assertIsNotNone(keys)
        self.assertIn("orders", keys)
        self.assertIn("products", keys)
        for blocked in ("users", "rules", "logs", "settings", "backups"):
            self.assertNotIn(blocked, keys, f"默认菜单不该含 {blocked}")
        self.assertEqual(keys, permissions.default_menu_keys())

    def test_40_custom_role_limits_menus(self):
        with SessionLocal() as s:
            role = permissions.save_role(
                s, name="只跟进订单", description="测试用", menus=["orders", "suppliers"]
            )
            row = users_service.create_user(s, "xiaoyu01", "xiaoyu2026", "user", "小鱼", "hub")
            row.role_id = int(role.id)
            s.commit()
            permissions.invalidate_cache()
            self.assertEqual(permissions.menu_keys_for(row), frozenset({"orders", "suppliers"}))
            # 不在菜单里的页面进不去；详情页 / 接口反查不到菜单项，放行
            self.assertTrue(permissions.can_access_path(row, "/orders"))
            self.assertFalse(permissions.can_access_path(row, "/products"))
            self.assertTrue(permissions.can_access_path(row, "/orders/123"))

    def test_45_builtin_roles_follow_code(self):
        """内置角色是动态的：新增菜单时管理员自动有、普通成员按默认集合走。"""
        with SessionLocal() as s:
            for key in (permissions.ROLE_KEY_ADMIN, permissions.ROLE_KEY_STAFF):
                role = s.scalar(select(Role).where(Role.key == key))
                role.menus = []          # 故意清空：内置角色的菜单本来就不看这个字段
                s.commit()
            permissions.invalidate_cache()
            admin_role = s.scalar(
                select(Role).where(Role.key == permissions.ROLE_KEY_ADMIN)
            )
            staff_role = s.scalar(
                select(Role).where(Role.key == permissions.ROLE_KEY_STAFF)
            )
            self.assertIsNone(permissions._load_role_menus(s, int(admin_role.id)))  # noqa: SLF001
            self.assertEqual(
                permissions._load_role_menus(s, int(staff_role.id)),  # noqa: SLF001
                permissions.default_menu_keys(),
            )

    def test_50_builtin_role_cannot_be_edited_or_deleted(self):
        with SessionLocal() as s:
            staff_role = s.scalar(
                select(Role).where(Role.key == permissions.ROLE_KEY_STAFF)
            )
            with self.assertRaises(ValueError):
                permissions.save_role(s, role=staff_role, name="改名", menus=["orders"])
            with self.assertRaises(ValueError):
                permissions.delete_role(s, staff_role)

    def test_55_delete_role_puts_users_back_to_default(self):
        with SessionLocal() as s:
            role = permissions.save_role(s, name="临时角色", menus=["orders"])
            row = users_service.create_user(s, "xiaoyu01", "xiaoyu2026", "user", "小鱼", "hub")
            row.role_id = int(role.id)
            s.commit()
            permissions.delete_role(s, role)
            permissions.invalidate_cache()
            refreshed = s.get(User, int(row.id))
            self.assertIsNone(refreshed.role_id, "角色删了，账号回到默认菜单")
            self.assertEqual(permissions.menu_keys_for(refreshed), permissions.default_menu_keys())

    def test_60_data_scope_field_reserved(self):
        """数据级权限先留位：字段存得下，当前不生效（菜单权限与它无关）。"""
        with SessionLocal() as s:
            role = permissions.save_role(s, name="数据级预留", menus=["orders"])
            role.data_scope = {"suppliers": [1, 2], "own_only": True}
            s.commit()
            again = s.get(Role, int(role.id))
            self.assertEqual(again.data_scope["suppliers"], [1, 2])

    # ------------------------------------------------------------ 页面与拦截
    def test_70_roles_page_admin_only(self):
        self.client.post("/login", data=ADMIN, follow_redirects=False)
        page = self.client.get("/roles")
        self.assertEqual(page.status_code, 200)
        self.assertIn("角色权限", page.text)
        self.assertIn("普通成员", page.text)

    def test_80_staff_blocked_from_admin_pages(self):
        """子账号直接敲 /users 与 /roles 这类地址：退回首页，不给进。"""
        with SessionLocal() as s:
            users_service.create_user(s, "xiaoyu01", "xiaoyu2026", "user", "小鱼", "hub")
        client = TestClient(app)
        self.assertEqual(client.post("/login", data=STAFF, follow_redirects=False).status_code, 303)
        for path in ("/users", "/roles", "/rules", "/settings"):
            resp = client.get(path, follow_redirects=False)
            self.assertEqual(resp.status_code, 303, f"{path} 应被拦下")
            self.assertEqual(resp.headers["location"], "/")
        self.assertEqual(client.get("/orders", follow_redirects=False).status_code, 200)

    def test_90_menu_follows_role(self):
        """挂了「只有订单」的角色：侧边栏只剩订单，商品等菜单不显示。"""
        with SessionLocal() as s:
            role = permissions.save_role(s, name="只有订单", menus=["orders"])
            row = users_service.create_user(s, "xiaoyu01", "xiaoyu2026", "user", "小鱼", "hub")
            row.role_id = int(role.id)
            s.commit()
        permissions.invalidate_cache()
        client = TestClient(app)
        client.post("/login", data=STAFF, follow_redirects=False)
        home = client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertNotIn('href="/products"', home.text, "没有商品菜单权限，不该出现商品入口")
        self.assertNotIn('href="/users"', home.text)
        self.assertIn("/orders", home.text, "订单菜单要在")

    def test_95_user_page_can_pick_role(self):
        """用户管理页给出「权限角色」下拉，子账号那行也能直接换。"""
        with SessionLocal() as s:
            permissions.save_role(s, name="客服", menus=["orders", "messages"])
            users_service.create_user(s, "xiaoyu01", "xiaoyu2026", "user", "小鱼", "hub")
        permissions.invalidate_cache()
        self.client.post("/login", data=ADMIN, follow_redirects=False)
        page = self.client.get("/users")
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="role_id"', page.text)
        self.assertIn("客服", page.text)


if __name__ == "__main__":
    unittest.main()
