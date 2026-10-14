"""学员进渔货栈要弹新手引导 + 「我的」弹窗显示显示名（2026-10-11 用户提）。

现场两条：

① 线上账号 liuyuhui（渔枢角色「学员组」）进来**没看到引导弹窗** ——
   判定只看账号身份字段（identity，线上 10 个账号全是建列时的默认值 internal，
   没人去改那个下拉），而学员在渔枢是按角色分组建的；
   现在放宽成「身份=外部学员 **或** 角色名带『学员』」。
② 「我的」弹窗第一行显示的是登录名 liuyuhui，要显示**显示名**（刘宇辉）。
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
from app.main import app  # noqa: E402
from app.models import IDENTITY_STUDENT, Role, User  # noqa: E402
from app.services.users import create_user  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.models import GuidePref  # noqa: E402
from src.self_purchase.guide import guide_of, is_hidden, is_student  # noqa: E402

PASSWORD = "xianyu2026"
ROLE_NAME = "测试学员组"
ROLE_USER = "t_guide_role"      # 挂「学员组」角色，身份仍是默认的内部人员
IDENT_USER = "t_guide_ident"    # 身份直接设成「外部学员」
INNER_USER = "t_guide_inner"    # 内部人员（既不挂学员角色，身份也是默认值）
ALL_USERS = (ROLE_USER, IDENT_USER, INNER_USER)


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(User).filter(User.username.in_(ALL_USERS)).delete(synchronize_session=False)
        s.query(Role).filter(Role.name == ROLE_NAME).delete(synchronize_session=False)
        # 「以后不再弹出」的偏好：跟着测试账号一起清，免得上一个用例的勾选影响下一个
        s.query(GuidePref).delete(synchronize_session=False)
        s.commit()


def _make_users() -> None:
    with SessionLocal() as s:
        role = Role(key="t_student_role", name=ROLE_NAME, description="测试用")
        s.add(role)
        s.flush()
        create_user(s, ROLE_USER, PASSWORD, display_name="测试-角色学员", portals="self")
        create_user(
            s, IDENT_USER, PASSWORD, display_name="测试-身份学员", portals="self",
            identity=IDENTITY_STUDENT,
        )
        create_user(s, INNER_USER, PASSWORD, display_name="测试-内部人员", portals="self")
        s.query(User).filter(User.username == ROLE_USER).one().role_id = role.id
        s.commit()


class SelfGuideStudentTest(unittest.TestCase):
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

    def _uid(self, username: str) -> int:
        with SessionLocal() as s:
            return int(s.query(User).filter(User.username == username).one().id)

    # ------------------------------------------------- 判定口径
    def test_10_student_judgement_accepts_role_name(self):
        """两条任一命中就算学员：身份字段（最精确）/ 角色名带「学员」（线上口径）。"""
        self.assertTrue(is_student(self._uid(ROLE_USER)), "角色名带「学员」要算学员")
        self.assertTrue(is_student(self._uid(IDENT_USER)), "身份=外部学员要算学员")
        self.assertFalse(is_student(self._uid(INNER_USER)), "内部人员不该算学员")
        self.assertFalse(is_student(0), "没登录不算学员")

    # ------------------------------------------------- 弹窗
    def test_20_student_role_sees_guide_modal(self):
        """挂「学员组」的账号进来就弹引导（线上 liuyuhui 这条）。"""
        self._login(ROLE_USER)
        html = self.client.get("/self/harbor").text
        self.assertIn('id="guideMask"', html, "该弹新手指引")
        self.assertIn("先把这 4 件事做一遍", html)
        for step in ("新建店铺", "选品铺货到店铺", "下载素材上架店铺", "完成第一单下单"):
            self.assertIn(step, html, f"引导里该有「{step}」这一步")
        self.assertIn("selfGuideSkip", html, "关掉这一次的开关要留着")

    def test_30_student_identity_sees_guide_modal(self):
        """身份设成「外部学员」的账号同样弹。"""
        self._login(IDENT_USER)
        self.assertIn('id="guideMask"', self.client.get("/self/harbor").text)

    def test_40_inner_user_has_no_guide(self):
        """内部人员不弹（别打扰内部运营）。"""
        self._login(INNER_USER)
        html = self.client.get("/self/harbor").text
        self.assertNotIn('id="guideMask"', html)
        with SessionLocal() as s:
            self.assertIsNone(guide_of(s, self._uid(INNER_USER)))

    # ------------------------------------------------- 「以后不再弹出」
    def test_44_hide_makes_the_guide_never_come_back(self):
        """勾了「以后不再弹出」：记在服务端，换浏览器 / 换设备都不再弹（用户 2026-10-11 拍板）。"""
        self._login(IDENT_USER)
        uid = self._uid(IDENT_USER)
        self.assertIn('id="guideMask"', self.client.get("/self/harbor").text, "先确认本来会弹")

        resp = self.client.post("/self/guide/hide")
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertTrue(resp.json()["ok"])

        # 服务端记着：同一个账号再进（哪怕换了会话）都不弹
        with SessionLocal() as s:
            self.assertTrue(is_hidden(s, uid))
            self.assertIsNone(guide_of(s, uid))
        self.assertNotIn('id="guideMask"', self.client.get("/self/harbor").text)

    def test_46_closing_without_the_checkbox_still_asks_next_time(self):
        """没勾那个框：只是关掉这一次（sessionStorage 标记），下次登录照旧提醒。"""
        self._login(IDENT_USER)
        uid = self._uid(IDENT_USER)
        html = self.client.get("/self/harbor").text
        self.assertIn("selfGuideSkip", html, "关掉这一次走 sessionStorage 标记")
        with SessionLocal() as s:
            self.assertFalse(is_hidden(s, uid), "没勾就不该往服务端写")
            self.assertIsNotNone(guide_of(s, uid))

    # ------------------------------------------------- 显示名
    def test_50_me_popup_shows_display_name(self):
        """「我的」弹窗第一行显示显示名（不是登录名）。"""
        self._login(IDENT_USER)
        html = self.client.get("/self/harbor").text
        start = html.find('class="sy-me-pop"')
        self.assertGreater(start, -1, "「我的」弹窗要在页面上")
        pop = html[start : html.find("</details>", start)]
        self.assertIn("测试-身份学员", pop, "弹窗里看显示名")
        self.assertNotIn(f">{IDENT_USER}<", pop, "不该显示登录名")


if __name__ == "__main__":
    unittest.main()
