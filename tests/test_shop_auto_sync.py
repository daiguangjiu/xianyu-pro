"""渔货栈账号 ↔ 渔枢店铺的**自动关联与双向同步**（2026-10-13）。

背景：内部人员（运营 / 带教 / 学员）在渔枢系统里已经有店铺档案，进渔货栈还要
再手填一遍 —— 同一家店填两遍，两边还容易不一致。所以：

1. **第一次进渔货栈就自动带过来**：按手机号（渔枢账号的手机号 / 登录名）或姓名
   （显示名 vs 店铺的实际运营人 / 闲鱼会员名）匹配，无主的认领、有主的只关联
   不改归属（见 `src/web/shop_sync.py`）；
2. **之后自动增量补**：按 `SYNC_INTERVAL_SECONDS` 节流，渔枢那边新建的店铺自动跟上，
   页面另有「立即同步」按钮；
3. **渔枢端建档也反向同步**：那边建了店 / 改了运营人，这边立刻给对应账号建关联
   （`app/services/shop.py` 的 `_notify_self_sync`），他不用再进一次渔货栈；
4. **解除关联过的不再自己回来**：台账 `shop_user_links.unlinked_at` 记着。

店铺数据本身不复制 —— 渔货栈「店铺管理」读写的就是渔枢那张 `shops` 表。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import unquote_plus

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
from src.deps import _get_engine  # noqa: E402  顺带把渔货栈那两张台账表建出来
from src.models.shop_link import (  # noqa: E402
    LINK_SOURCE_AUTO_NAME,
    LINK_SOURCE_AUTO_PHONE,
    LINK_SOURCE_MANUAL,
    ShopSyncMark,
    ShopUserLink,
)

PWD = "Test@12345"
STUDENT_PHONE = "13900000101"
MEMBER_PHONE = "13900000102"

# 学员在渔货栈手动新建店铺时填的那几项（走精简表单）
STUDENT_SHOP = {
    "name": "手动新建的店",
    "member_name": "xy_sync_001",
    "phone": "13900000111",
    "real_name_verified": "1",
    "operator": "孙七",
    "contact_phone": "13900000112",
    "wechat": "sunqi_wx",
    "category": "家电",
}


class ShopAutoSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        _get_engine()  # 渔货栈那两张台账表（shop_user_links / shop_sync_marks）在这建
        ensure_default_admin()
        cls.client = TestClient(app)
        with SessionLocal() as s:
            cls.role_self_run_id = cls._ensure_role(s, "test_sync_self_run", SELF_RUN_ROLE_NAME).id
            cls.role_student_id = cls._ensure_role(s, "test_sync_student", "学员").id
            cls.student_id = cls._ensure_user(
                s, "test_sync_stu", "赵六", cls.role_student_id, "hub,self", STUDENT_PHONE
            ).id
            cls.member_id = cls._ensure_user(
                s, "test_sync_member", "李四", cls.role_self_run_id, "hub,self", MEMBER_PHONE
            ).id

    @classmethod
    def tearDownClass(cls):
        with SessionLocal() as s:
            s.query(ShopUserLink).delete()
            s.query(ShopSyncMark).delete()
            s.query(Shop).delete()
            s.commit()
            for name in ("test_sync_stu", "test_sync_member"):
                row = s.scalar(select(User).where(User.username == name))
                if row is not None:
                    users_service.delete_user(s, row.id)
            for key in ("test_sync_self_run", "test_sync_student"):
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
    def _ensure_user(s, username: str, display_name: str, role_id: int, portals: str,
                     phone: str = "") -> User:
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
                phone=phone or None,
            )
            s.add(row)
            s.flush()
        row.role_id = role_id
        row.portals = portals
        row.phone = phone or None
        s.commit()
        return row

    def setUp(self):
        with SessionLocal() as s:
            s.query(ShopUserLink).delete()
            s.query(ShopSyncMark).delete()
            s.query(Shop).delete()
            s.commit()
        self._login("test_sync_stu")  # 默认以学员「赵六」的身份进渔货栈

    def tearDown(self):
        with SessionLocal() as s:
            s.query(ShopUserLink).delete()
            s.query(ShopSyncMark).delete()
            s.query(Shop).delete()
            s.commit()

    # ---------------------------------------------------------------- 工具
    def _login(self, username: str, password: str = PWD) -> None:
        resp = self.client.post(
            "/login", data={"username": username, "password": password}, follow_redirects=False
        )
        self.assertIn(resp.status_code, (302, 303))

    @staticmethod
    def _make_shop(name: str, **kw) -> int:
        """直接落一条店铺档案（造数据用）：绕过 create_shop，免得触发反向同步。"""
        with SessionLocal() as s:
            row = Shop(name=name, **kw)
            s.add(row)
            s.commit()
            s.refresh(row)
            return int(row.id or 0)

    def _link_row(self, s, shop_id: int):
        return s.scalar(
            select(ShopUserLink).where(
                ShopUserLink.user_id == self.student_id, ShopUserLink.shop_id == shop_id
            )
        )

    def _shop_of(self, name: str):
        with SessionLocal() as s:
            return s.scalar(select(Shop).where(Shop.name == name))

    # ---------------------------------------------------------------- 用例
    def test_10_first_login_claims_by_name(self):
        """第一次进渔货栈：渔枢里「实际运营人 = 我」的无主店铺自动认领过来。"""
        self._make_shop("自动同步店A", operator="赵六")

        resp = self.client.get("/self/shops")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("自动同步店A", resp.text)

        with SessionLocal() as s:
            row = self._shop_of("自动同步店A")
            # 无主（owner_user_id = 0）→ 认领过来，归属也转过来
            self.assertEqual(int(row.owner_user_id or 0), self.student_id)
            link = self._link_row(s, int(row.id or 0))
            self.assertIsNotNone(link)
            self.assertEqual(link.source, LINK_SOURCE_AUTO_NAME)
            self.assertTrue(link.claimed)

    def test_15_first_login_links_by_phone(self):
        """手机号对上的也算：渔枢账号的手机号 = 店铺的联系 / 绑定手机号。"""
        self._make_shop("自动同步店B", contact_phone=STUDENT_PHONE)

        self.client.get("/self/shops")

        with SessionLocal() as s:
            row = self._shop_of("自动同步店B")
            link = self._link_row(s, int(row.id or 0))
            self.assertIsNotNone(link)
            self.assertEqual(link.source, LINK_SOURCE_AUTO_PHONE)

    def test_20_throttle_then_manual_sync(self):
        """平时节流（5 分钟内不重复扫库），「立即同步」按钮才无视节流补齐。"""
        self._make_shop("首登带过来的店", operator="赵六")
        self.client.get("/self/shops")  # 首登：全量同步一次

        self._make_shop("节流后新建的店", operator="赵六")
        self.client.get("/self/shops")  # 距上次不足 5 分钟 → 跳过
        row = self._shop_of("节流后新建的店")
        self.assertEqual(int(row.owner_user_id or 0), 0, "节流期内不该自动带过来")

        resp = self.client.post("/self/shops/sync", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        # 提示挂在查询串上（urlencode 把空格编成 +，所以要用 unquote_plus 解）
        self.assertIn("本次新增 1 家", unquote_plus(resp.headers.get("location", "")))
        self.assertIn("节流后新建的店", self.client.get("/self/shops").text)

    def test_25_linked_shop_keeps_owner(self):
        """挂在别人名下的店：只关联不改归属（渔枢那边照样看得到）。"""
        shop_id = self._make_shop("李四名下的店", operator="赵六", owner_user_id=self.member_id)

        page = self.client.get("/self/shops").text
        self.assertIn("李四名下的店", page)

        with SessionLocal() as s:
            row = s.get(Shop, shop_id)
            self.assertEqual(int(row.owner_user_id or 0), self.member_id)  # 归属没动
            self.assertEqual(int(row.self_linked_user_id or 0), self.student_id)  # 只记关联
            link = self._link_row(s, shop_id)
            self.assertIsNotNone(link)
            self.assertFalse(link.claimed)

    def test_30_unlinked_shop_not_coming_back(self):
        """解除关联过的店铺，同步时不会再自己关联回来（台账上记着 unlinked_at）。"""
        shop_id = self._make_shop("要解绑的店", operator="赵六", owner_user_id=self.member_id)
        self.client.get("/self/shops")  # 首登：按运营人关联上

        resp = self.client.post(f"/self/shops/{shop_id}/delete", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            row = s.get(Shop, shop_id)
            self.assertEqual(int(row.self_linked_user_id or 0), 0, "关联已解除")
            self.assertIsNotNone(self._link_row(s, shop_id).unlinked_at)

        self.client.post("/self/shops/sync")  # 再同步也不该回来
        with SessionLocal() as s:
            row = s.get(Shop, shop_id)
            self.assertEqual(int(row.self_linked_user_id or 0), 0)

    def test_40_hub_side_create_triggers_sync(self):
        """渔枢端建档即反向同步：对应的人不用等他进渔货栈，这边直接把关联建好。"""
        with SessionLocal() as s:
            row = shop_service.create_shop(
                s, {"name": "渔枢新建店", "operator": "赵六", "contact_phone": STUDENT_PHONE}
            )
            shop_id = int(row.id or 0)

        with SessionLocal() as s:
            link = self._link_row(s, shop_id)
            self.assertIsNotNone(link, "建档后应立刻给对应账号建关联")
            row = s.get(Shop, shop_id)
            self.assertEqual(int(row.owner_user_id or 0), self.student_id)

    def test_45_hub_side_update_operator_triggers_sync(self):
        """改了运营人才重新认一遍归属（改别的字段不打扰）。"""
        shop_id = self._make_shop("改运营人的店", operator="钱八")
        with SessionLocal() as s:
            shop_service.update_shop(s, shop_id, {"name": "改运营人的店", "operator": "赵六"})

        with SessionLocal() as s:
            link = self._link_row(s, shop_id)
            self.assertIsNotNone(link)

    def test_50_page_shows_sync_bar(self):
        """店铺列表页给同步状态与「立即同步」入口。"""
        self._make_shop("状态条店", operator="赵六")

        page = self.client.get("/self/shops").text
        self.assertIn('class="sync-bar"', page)
        self.assertIn("立即同步", page)
        self.assertIn("/shops/sync", page)
        self.assertIn("自动带入 1 家", page)

    def test_60_manual_create_notes_manual_source(self):
        """自己手动添加 / 认领的店铺，台账上记「手动添加」，不被自动来源改写。"""
        resp = self.client.post("/self/shops", data=STUDENT_SHOP, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

        row = self._shop_of("手动新建的店")
        self.assertIsNotNone(row)
        self.assertEqual(int(row.owner_user_id or 0), self.student_id)
        with SessionLocal() as s:
            link = self._link_row(s, int(row.id or 0))
            self.assertIsNotNone(link)
            self.assertEqual(link.source, LINK_SOURCE_MANUAL)


if __name__ == "__main__":
    unittest.main()
