"""后台账号体系：登录、角色权限、子账号管理、操作日志。

业务口径：
  1 admin 是默认管理员（默认密码 xianyu2026），管理员可建子账号；
  2 解析规则、系统设置、操作日志、用户管理 仅管理员可见；
  3 后台写操作自动记操作日志，密码类字段不落日志。
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
from app.models import OperationLog, Supplier, User  # noqa: E402
from app.services import users as users_service  # noqa: E402
from app.web.auth import ensure_default_admin, verify_password  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
SUB = {"username": "xiaoyu01", "password": "xianyu2026", "display_name": "小鱼"}
# 仅管理员可见的页面
ADMIN_ONLY = ("/rules", "/settings", "/logs", "/users")


class AuthFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        self.admin = ensure_default_admin()
        with SessionLocal() as s:
            s.query(OperationLog).delete()
            s.query(Supplier).delete()
            # 清掉上一轮建的子账号，只保留管理员
            s.query(User).filter(User.username != self.admin.username).delete()
            s.commit()
        self.client = TestClient(app)

    def _login(self, data=None) -> TestClient:
        resp = self.client.post("/login", data=data or ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)
        return self.client

    def test_01_default_admin_seeded(self):
        with SessionLocal() as s:
            user = s.scalar(select(User).where(User.username == "admin"))
            self.assertIsNotNone(user)
            self.assertEqual(user.role, "admin")
            self.assertEqual(user.status, "active")
            self.assertTrue(verify_password("xianyu2026", user.password_hash))
            self.assertFalse(verify_password("wrong-pass", user.password_hash))

    def test_02_login_required(self):
        resp = self.client.get("/orders", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(resp.headers["location"].startswith("/login"))

        # 密码错误：回到登录页并提示
        page = self.client.post("/login", data={"username": "admin", "password": "bad"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("账号或密码错误", page.text)
        with SessionLocal() as s:
            self.assertEqual(
                s.scalar(select(OperationLog.id).where(OperationLog.action == "登录失败")),
                1,
            )

    def test_03_login_success_and_logout(self):
        self._login()
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        # 顶栏显示当前账号与角色
        self.assertIn("管理员", page.text)
        self.assertIn("退出", page.text)

        resp = self.client.post("/logout", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        # 登出后再访问会被踢回登录页
        self.assertEqual(self.client.get("/orders", follow_redirects=False).status_code, 303)

        with SessionLocal() as s:
            actions = [
                r[0]
                for r in s.execute(select(OperationLog.action)).all()
            ]
            self.assertIn("登录成功", actions)
            self.assertIn("退出登录", actions)

    def test_04_admin_only_pages_for_sub_account(self):
        self._login()
        resp = self.client.post(
            "/users", data={**SUB, "role": "user"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            sub = s.scalar(select(User).where(User.username == SUB["username"]))
            self.assertEqual(sub.role, "user")
            self.assertTrue(verify_password(SUB["password"], sub.password_hash))
            # 建号动作已记日志，且密码不落库日志
            log = s.scalar(
                select(OperationLog).where(OperationLog.action == "新增账号")
            )
            self.assertIsNotNone(log)
            self.assertNotIn(SUB["password"], log.detail or "")

        # 换成子账号登录
        self.client.post("/logout", follow_redirects=False)
        self._login({"username": SUB["username"], "password": SUB["password"]})

        for path in ADMIN_ONLY:
            resp = self.client.get(path, follow_redirects=False)
            self.assertEqual(resp.status_code, 303, path)
            self.assertEqual(resp.headers["location"].split("?")[0], "/", path)

        # 业务页面子账号照常可见，但导航里不出现管理员菜单
        page = self.client.get("/orders")
        self.assertEqual(page.status_code, 200)
        self.assertIn("订单管理", page.text)
        self.assertNotIn("用户管理", page.text)

        # 越权写操作同样被挡住
        resp = self.client.post("/rules", data={"yaml_text": "x: 1"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        resp = self.client.post(
            "/users/1/delete", data={}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertIsNotNone(s.get(User, 1))

    def test_05_account_rules_and_actions(self):
        self._login()
        # 账号格式：字母开头、3-20 位；密码 8-32 位且含字母和数字
        for bad in (
            {"username": "ab", "password": "xianyu2026"},              # 太短
            {"username": "1abc", "password": "xianyu2026"},            # 数字开头
            {"username": "a-b", "password": "xianyu2026"},             # 非法字符
            {"username": "xiaoyu01", "password": "1234567"},           # 密码太短
            {"username": "xiaoyu01", "password": "abcdefgh"},          # 密码无数字
            {"username": "xiaoyu01", "password": "12345678"},          # 密码无字母
        ):
            resp = self.client.post(
                "/users", data={**bad, "role": "user"}, follow_redirects=False
            )
            self.assertEqual(resp.status_code, 303)
            with SessionLocal() as s:
                self.assertIsNone(
                    s.scalar(select(User).where(User.username == "xiaoyu01"))
                )

        resp = self.client.post("/users", data={**SUB, "role": "user"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            sub = s.scalar(select(User).where(User.username == SUB["username"]))
            sub_id = sub.id

        # 停用后不能登录
        resp = self.client.post(f"/users/{sub_id}/status", data={"status": "disabled"},
                                follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        bad = self.client.post(
            "/login", data={"username": SUB["username"], "password": SUB["password"]}
        )
        self.assertIn("账号或密码错误", bad.text)

        # 重新启用 + 改密码后可登录
        self.client.post(f"/users/{sub_id}/status", data={"status": "active"}, follow_redirects=False)
        self.client.post(
            f"/users/{sub_id}/password", data={"password": "Newpass2026"}, follow_redirects=False
        )
        self.client.post("/logout", follow_redirects=False)
        self._login({"username": SUB["username"], "password": "Newpass2026"})

        # 管理员不能把自己删掉或停用（至少留一个管理员）
        self.client.post("/logout", follow_redirects=False)
        self._login()
        resp = self.client.post(f"/users/{self.admin.id}/delete", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertIsNotNone(s.get(User, self.admin.id))

    def test_06_password_format_helpers(self):
        with self.assertRaises(ValueError):
            users_service.validate_username("1bad")
        with self.assertRaises(ValueError):
            users_service.validate_password("short1")
        with self.assertRaises(ValueError):
            users_service.validate_password("allletters")
        self.assertEqual(users_service.validate_username("xiaoyu_01"), "xiaoyu_01")

    def test_07_write_actions_are_logged(self):
        self._login()
        resp = self.client.post(
            "/suppliers",
            data={"name": "宁波家纺", "city": "宁波", "has_group": "1", "group_name": "宁波群"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            log = s.scalar(select(OperationLog).where(OperationLog.action == "新增供应商"))
            self.assertIsNotNone(log)
            self.assertEqual(log.username, "admin")
            self.assertEqual(log.role, "admin")
            self.assertIn("name=宁波家纺", log.detail)
            self.assertIn("group_name=宁波群", log.detail)

        page = self.client.get("/logs")
        self.assertEqual(page.status_code, 200)
        self.assertIn("新增供应商", page.text)
        self.assertIn("admin", page.text)


if __name__ == "__main__":
    unittest.main()
