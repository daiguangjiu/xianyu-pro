"""渔货栈「店铺带入 / 同步」与按账号组区分的建档表单（2026-10-05）。

背景：学员在渔货栈新建店铺时，这条店铺在渔枢系统里往往已经建档过了 ——
以前只会冷冰冰地报「店铺已存在」，现在改成按需带入：

1. 渔枢系统端建档、还没归属任何渔货栈账号的（owner_user_id = 0）：直接认领过来；
2. 挂在别人名下、但那人就是学员填的「实际运营人」且属于渔枢系统后台自营账号组的：
   同步过来（只记关联，不改归属，渔枢系统那边照样看得到）；
3. 填的运营人姓名对上自营账号组的人时，他那边的店铺一次性同步过来，不用再填一遍；
4. 建档表单按账号组区分：学员只填 8 项，自营账号组（和管理员）填完整档案。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Role, SELF_RUN_ROLE_NAME, Shop, User  # noqa: E402
from app.services import shop as shop_service  # noqa: E402
from app.services import users as users_service  # noqa: E402
from app.services.users import hash_password  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
PWD = "Test@12345"

# 学员填的那 8 项
STUDENT_SHOP = {
    "name": "学员的小店",
    "member_name": "xy_member_001",
    "phone": "13900000001",
    "real_name_verified": "1",
    "operator": "王五",
    "contact_phone": "13900000002",
    "wechat": "wangwu_wx",
    "category": "家电",
}


class SelfShopLinkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        with SessionLocal() as s:
            cls.admin_id = s.scalar(select(User.id).where(User.username == "admin"))
            # 渔枢系统后台的两个账号组：自营账号组（运营）+ 学员
            cls.role_self_run_id = cls._ensure_role(s, "test_self_run", SELF_RUN_ROLE_NAME).id
            cls.role_student_id = cls._ensure_role(s, "test_student", "学员").id
            cls.member_id = cls._ensure_user(
                s, "test_member", "李四", cls.role_self_run_id, "hub,self"
            ).id
            cls.student_id = cls._ensure_user(
                s, "test_student01", "赵六", cls.role_student_id, "hub,self"
            ).id

    @classmethod
    def tearDownClass(cls):
        with SessionLocal() as s:
            s.query(Shop).delete()
            s.commit()
            for name in ("test_member", "test_student01"):
                row = s.scalar(select(User).where(User.username == name))
                if row is not None:
                    users_service.delete_user(s, row.id)  # 顺带解绑渔货栈那几张引用表
            for key in ("test_self_run", "test_student"):
                row = s.scalar(select(Role).where(Role.key == key))
                if row is not None:
                    s.delete(row)
            s.commit()

    @staticmethod
    def _ensure_role(s, key: str, name: str) -> Role:
        row = s.scalar(select(Role).where(Role.key == key))
        if row is None:
            row = Role(key=key, name=name, menus=["orders"], description="测试用账号组")
            s.add(row)
            s.flush()
        row.name = name
        s.commit()
        return row

    @staticmethod
    def _ensure_user(s, username: str, display_name: str, role_id: int, portals: str) -> User:
        row = s.scalar(select(User).where(User.username == username))
        if row is None:
            row = User(
                username=username,
                display_name=display_name,
                password_hash=hash_password(PWD),
                role="user",
                portals=portals,
                role_id=role_id,
                status="active",
            )
            s.add(row)
            s.flush()
        row.role_id = role_id
        row.portals = portals
        s.commit()
        return row

    def setUp(self):
        with SessionLocal() as s:
            s.query(Shop).delete()
            s.commit()
        self._login("test_student01")  # 默认以学员身份进来

    def tearDown(self):
        with SessionLocal() as s:
            s.query(Shop).delete()
            s.commit()

    # ---------------------------------------------------------------- 工具
    def _login(self, username: str, password: str = PWD) -> None:
        resp = self.client.post(
            "/login", data={"username": username, "password": password}, follow_redirects=False
        )
        assert resp.status_code in (302, 303), resp.text

    def _flash(self, resp) -> str:
        """重定向里的提示语（msg 是 URL 编码过的）。"""
        return unquote(resp.headers.get("location", "") or "")

    def _shop(self, name: str) -> Shop | None:
        with SessionLocal() as s:
            return s.scalar(select(Shop).where(Shop.name == name))

    # ------------------------------------------------- 表单按账号组区分
    def test_10_student_form_only_student_fields(self):
        """新建页（2026-10-11 新口径）：一律只摆那 6 个字段，完整档案的字段不出现。"""
        page = self.client.get("/self/shops/new")
        self.assertEqual(page.status_code, 200)
        for label in ("店铺名称（闲鱼昵称）", "会员名", "闲鱼手机号", "姓名", "手机号码", "店铺类型"):
            self.assertIn(label, page.text, f"新建页要有「{label}」")
        self.assertIn("此手机为闲鱼App的我的-设置-账号与安全的手机号", page.text)
        self.assertIn("个人店铺", page.text)
        self.assertIn("鱼小铺", page.text)
        for label in ("芝麻信用分", "支付宝绑定账号", "闲气值", "平台技术服务费"):
            self.assertNotIn(label, page.text, "新建页不该问完整档案的字段")

    def test_15_self_run_form_keeps_full_fields(self):
        """编辑页：还是那份完整档案（新建页是简版，别把编辑页也砍了）。"""
        self._login("test_member")
        resp = self.client.post("/self/shops", data=STUDENT_SHOP, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        row = self._shop("学员的小店")
        self.assertIsNotNone(row, "先建一店铺才能打开编辑页")
        page = self.client.get(f"/self/shops/{row.id}/edit")
        self.assertEqual(page.status_code, 200)
        self.assertIn("芝麻信用分", page.text)
        self.assertIn("平台技术服务费", page.text)

    def test_18_student_submit_writes_student_fields(self):
        """学员提交：会员名 / 联系电话 / 实名认证是否完成都写进档案，状态待审核。"""
        resp = self.client.post("/self/shops", data=STUDENT_SHOP, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        row = self._shop("学员的小店")
        self.assertIsNotNone(row)
        self.assertEqual(row.owner_user_id, self.student_id)
        self.assertEqual(row.member_name, "xy_member_001")
        self.assertEqual(row.contact_phone, "13900000002")
        self.assertTrue(row.real_name_verified, "勾了「实名认证是否完成」")
        self.assertEqual(row.member_name, "xy_member_001")

    # ------------------------------------------------- 渔枢系统已存在 → 带入
    def test_20_claim_hub_existing_shop(self):
        """渔枢系统端建档、还没归属任何账号的店铺：提交同名直接认领过来。"""
        with SessionLocal() as s:
            row = shop_service.create_shop(s, {"name": "渔枢系统已建档的店", "operator": "钱七"})
            shop_id = row.id
            self.assertEqual(row.owner_user_id, 0)

        resp = self.client.post(
            "/self/shops",
            data={**STUDENT_SHOP, "name": "渔枢系统已建档的店"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            row = s.get(Shop, shop_id)
            self.assertEqual(row.owner_user_id, self.student_id, "归属转到学员名下")
            self.assertEqual(row.self_linked_user_id, self.student_id, "记「渔货栈店铺已关联」")
            self.assertIsNotNone(row.self_linked_at)
        listing = self.client.get("/self/shops")
        self.assertIn("渔枢系统已建档的店", listing.text, "学员的店铺列表里能看到它")
        with SessionLocal() as s:
            self.assertEqual(
                shop_service.count_shops(s, owner_user_id=self.student_id), 1, "算进我的店铺数"
            )

    def test_25_duplicate_claim_is_rejected(self):
        """已经在我名下的店铺：再提交一次只提示，不会重复建档。"""
        self.client.post("/self/shops", data=STUDENT_SHOP, follow_redirects=False)
        resp = self.client.post("/self/shops", data=STUDENT_SHOP, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("已经在你的店铺列表里", self._flash(resp))
        with SessionLocal() as s:
            self.assertEqual(shop_service.count_shops(s), 1, "没有多出一条")

    def test_30_taken_by_other_is_rejected(self):
        """挂在别人名下、对不上自营账号组运营人的：拒绝带入，档案不动。"""
        with SessionLocal() as s:
            row = shop_service.create_shop(s, {"name": "别人的店", "operator": "路人甲"})
            shop_id = row.id
            row.owner_user_id = self.admin_id
            s.commit()

        resp = self.client.post(
            "/self/shops", data={**STUDENT_SHOP, "name": "别人的店"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("已被其它账号占用", self._flash(resp))
        with SessionLocal() as s:
            row = s.get(Shop, shop_id)
            self.assertEqual(row.owner_user_id, self.admin_id, "归属没被改走")
            self.assertEqual(row.self_linked_user_id, 0)

    # ------------------------------------------------- 姓名对上自营账号组 → 同步
    def test_40_link_by_self_run_member_name(self):
        """同名 + 运营人就是自营账号组的人：同步过来（只记关联，不改归属）。"""
        with SessionLocal() as s:
            row = shop_service.create_shop(s, {"name": "自营运营的店", "operator": "李四"})
            shop_id = row.id
            row.owner_user_id = self.member_id
            s.commit()

        resp = self.client.post(
            "/self/shops",
            data={**STUDENT_SHOP, "name": "自营运营的店", "operator": "李四"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            row = s.get(Shop, shop_id)
            self.assertEqual(row.self_linked_user_id, self.student_id, "已关联到学员")
            self.assertEqual(row.owner_user_id, self.member_id, "档案仍归运营，改的是关联")
        listing = self.client.get("/self/shops")
        self.assertIn("自营运营的店", listing.text)
        self.assertIn("渔枢带入", listing.text)

    def test_50_sync_member_shops_by_operator(self):
        """名字没撞上，但填的运营人是自营账号组的人：把他名下的店铺同步过来。"""
        with SessionLocal() as s:
            row = shop_service.create_shop(s, {"name": "李四的店甲", "operator": "李四"})
            shop_id = row.id
            row.owner_user_id = self.member_id
            s.commit()

        resp = self.client.post(
            "/self/shops", data={**STUDENT_SHOP, "operator": "李四"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            row = s.get(Shop, shop_id)
            self.assertEqual(row.self_linked_user_id, self.student_id)
            self.assertIsNone(
                s.scalar(select(Shop).where(Shop.name == "学员的小店")),
                "同步过来就不再重复新建一条",
            )

    def test_55_unknown_operator_creates_shop(self):
        """运营人对不上任何人：正常新建，走渔枢系统审核。"""
        self.client.post("/self/shops", data=STUDENT_SHOP, follow_redirects=False)
        row = self._shop("学员的小店")
        self.assertIsNotNone(row)
        self.assertEqual(row.owner_user_id, self.student_id)
        self.assertEqual(row.self_linked_user_id, 0)

    # ------------------------------------------------- 解除关联
    def test_60_unlink_keeps_record(self):
        """删掉只是同步过来（没改归属）的店铺：只解除关联，渔枢系统档案留着。"""
        with SessionLocal() as s:
            row = shop_service.create_shop(s, {"name": "要解除的店", "operator": "李四"})
            shop_id = row.id
            row.owner_user_id = self.member_id
            s.commit()
            shop_service.link_self_shop(s, row, self.student_id)

        resp = self.client.post(f"/self/shops/{shop_id}/delete", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            row = s.get(Shop, shop_id)
            self.assertIsNotNone(row, "档案还在")
            self.assertEqual(row.owner_user_id, self.member_id)
            self.assertEqual(row.self_linked_user_id, 0, "关联已解除")
            self.assertEqual(shop_service.count_shops(s, owner_user_id=self.student_id), 0)

    def test_65_delete_own_shop(self):
        """自己提交的店铺：删掉整条档案（老行为不变）。"""
        self.client.post("/self/shops", data=STUDENT_SHOP, follow_redirects=False)
        row = self._shop("学员的小店")
        resp = self.client.post(f"/self/shops/{row.id}/delete", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIsNone(self._shop("学员的小店"))

    # ------------------------------------------------- 渔枢系统端看得到关联
    def test_70_hub_list_shows_linked(self):
        """渔枢系统端店铺列表：显示「渔货栈店铺已关联」。"""
        with SessionLocal() as s:
            row = shop_service.create_shop(s, {"name": "被关联的店", "operator": "李四"})
            shop_id = row.id
            row.owner_user_id = self.member_id
            s.commit()
            shop_service.link_self_shop(s, row, self.student_id)

        self._login("admin", ADMIN["password"])
        listing = self.client.get("/shops")
        self.assertIn("被关联的店", listing.text)
        self.assertIn("已关联·", listing.text, "列表里标出渔货栈店铺已关联")
        detail = self.client.get(f"/shops/{shop_id}")
        self.assertIn("渔货栈店铺已关联", detail.text)
        self.assertIn("已关联", detail.text)


if __name__ == "__main__":
    unittest.main()
