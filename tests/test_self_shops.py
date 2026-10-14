"""渔货栈「店铺管理」与渔枢系统端审核（2026-09-28 版本）。

两端共用同一份店铺数据（渔枢系统库的 shops 表）：

1. 渔货栈账号新增的店铺记在自己名下（`owner_user_id`），状态是「待审核」；
   渔枢系统端直接在店铺管理里建档的不归属任何账号，默认「已通过」；
2. 只有渔枢系统端审核「已通过」的店铺才在渔货栈生效，驳回原因提交人能看到；
3. 渔货栈只能看 / 改 / 删自己提交的店铺：改完会回到「待审核」，别人的店一律当不存在。
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
from app.models import (  # noqa: E402
    SHOP_AUDIT_APPROVED,
    SHOP_AUDIT_PENDING,
    SHOP_AUDIT_REJECTED,
    Shop,
    User,
)
from app.services import shop as shop_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from app.web import nav  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

NEW_SHOP = {
    "name": "渔货栈小店",
    "operator": "张三",
    "category": "数码",
    "supply": "京东数码类",
    "phone": "13800000000",
}


class SelfShopAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text
        with SessionLocal() as s:
            cls.admin_id = s.scalar(select(User.id).where(User.username == "admin"))

    def setUp(self):
        with SessionLocal() as s:
            s.query(Shop).delete()
            s.commit()
        nav._badge_cache.clear()  # 菜单徽标有 30 秒缓存

    def tearDown(self):
        with SessionLocal() as s:
            s.query(Shop).delete()
            s.commit()

    # ---------------------------------------------------------------- 提交
    def test_10_submit_goes_pending(self):
        """渔货栈提交：进渔枢系统库、记在当前账号名下、状态待审核。"""
        resp = self.client.post("/self/shops", data=NEW_SHOP, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            row = s.scalar(select(Shop).where(Shop.name == "渔货栈小店"))
            self.assertIsNotNone(row, "店铺写进了渔枢系统端店铺档案")
            self.assertEqual(row.owner_user_id, self.admin_id)
            self.assertEqual(row.audit_status, SHOP_AUDIT_PENDING)
            self.assertEqual(row.supply, "京东数码类", "字段与渔枢系统端一致")
            self.assertTrue(row.is_self_submitted)

    def test_20_hub_created_is_approved(self):
        """渔枢系统端自己建档：不归属任何账号，默认已通过（不用自己审自己）。"""
        with SessionLocal() as s:
            row = shop_service.create_shop(s, {"name": "渔枢系统直接建档"})
        self.assertEqual(row.audit_status, SHOP_AUDIT_APPROVED)
        self.assertFalse(row.is_self_submitted)
        self.assertEqual(row.owner_user_id, 0)

    def test_30_pending_shows_in_hub_list(self):
        """待审核的店铺出现在渔枢系统端列表里，菜单徽标 +1。"""
        self.client.post("/self/shops", data=NEW_SHOP, follow_redirects=False)
        page = self.client.get("/shops")
        self.assertIn("渔货栈小店", page.text)
        self.assertIn("待审核", page.text)
        counts = nav.badge_counts()
        self.assertEqual(counts.get("shops_audit"), 1, "店铺管理菜单显示待审核数量")

    # ---------------------------------------------------------------- 审核
    def test_40_approve_makes_it_effective(self):
        """渔枢系统端审核通过：渔货栈显示已通过，生效数 +1。"""
        with SessionLocal() as s:
            row = shop_service.create_shop(
                s, dict(NEW_SHOP), owner_user_id=self.admin_id,
                audit_status=SHOP_AUDIT_PENDING,
            )
            shop_id = row.id
        resp = self.client.post(
            f"/shops/{shop_id}/audit", data={"audit_status": SHOP_AUDIT_APPROVED},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        page = self.client.get("/self/shops")
        self.assertIn("已通过", page.text)
        self.assertIn("关联店铺 1 家", page.text, "顶栏显示生效店铺数")

    def test_50_reject_shows_reason(self):
        """驳回：渔货栈看不到「已通过」，能看到驳回原因。"""
        with SessionLocal() as s:
            row = shop_service.create_shop(
                s, dict(NEW_SHOP), owner_user_id=self.admin_id,
                audit_status=SHOP_AUDIT_PENDING,
            )
            shop_id = row.id
        self.client.post(
            f"/shops/{shop_id}/audit",
            data={"audit_status": SHOP_AUDIT_REJECTED, "audit_note": "账号名称与出货闲鱼号不一致"},
            follow_redirects=False,
        )
        page = self.client.get("/self/shops")
        self.assertIn("已驳回", page.text)
        self.assertIn("账号名称与出货闲鱼号不一致", page.text)
        self.assertIn("关联店铺 0 家", page.text, "驳回的不算生效")
        # 重新通过时清掉上一次的驳回原因
        with SessionLocal() as s:
            shop_service.set_audit(s, shop_id, SHOP_AUDIT_APPROVED)
            row = s.get(Shop, shop_id)
            self.assertEqual(row.audit_note, "")

    # ---------------------------------------------------------------- 归属
    def test_60_edit_returns_to_pending(self):
        """渔货栈改自己的店：改完回到待审核，等渔枢系统端重新审。"""
        with SessionLocal() as s:
            row = shop_service.create_shop(
                s, dict(NEW_SHOP), owner_user_id=self.admin_id,
                audit_status=SHOP_AUDIT_APPROVED,
            )
            shop_id = row.id
        self.client.post(
            f"/self/shops/{shop_id}/edit",
            data={**NEW_SHOP, "category": "家电"}, follow_redirects=False,
        )
        with SessionLocal() as s:
            row = s.get(Shop, shop_id)
            self.assertEqual(row.category, "家电")
            self.assertEqual(row.audit_status, SHOP_AUDIT_PENDING, "改动要重新审核")

    def test_70_other_peoples_shop_invisible(self):
        """别人的店（渔枢系统端建档 / 其它账号提交）：渔货栈看不到，也改不了删不了。"""
        with SessionLocal() as s:
            other = shop_service.create_shop(s, {"name": "别人的店"})
            other_id = other.id
        page = self.client.get("/self/shops")
        self.assertNotIn("别人的店", page.text)
        # 硬改别人的店：接口拒绝，数据不变
        resp = self.client.post(
            f"/self/shops/{other_id}/edit", data={"name": "被改了"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(Shop, other_id).name, "别人的店")
        self.client.post(f"/self/shops/{other_id}/delete", follow_redirects=False)
        with SessionLocal() as s:
            self.assertIsNotNone(s.get(Shop, other_id), "别人的店删不掉")

    def test_80_delete_mine(self):
        """删自己的店：渔枢系统库里的档案一起删掉。"""
        with SessionLocal() as s:
            row = shop_service.create_shop(
                s, dict(NEW_SHOP), owner_user_id=self.admin_id,
                audit_status=SHOP_AUDIT_PENDING,
            )
            shop_id = row.id
        self.client.post(f"/self/shops/{shop_id}/delete", follow_redirects=False)
        with SessionLocal() as s:
            self.assertIsNone(s.get(Shop, shop_id))

    # ---------------------------------------------------------------- 页面
    def test_90_pages_render(self):
        """新页面能打开：渔货栈列表 + 渔枢系统端详情页带审核区。"""
        self.assertEqual(self.client.get("/self/shops").status_code, 200)
        with SessionLocal() as s:
            row = shop_service.create_shop(
                s, dict(NEW_SHOP), owner_user_id=self.admin_id,
                audit_status=SHOP_AUDIT_PENDING,
            )
            shop_id = row.id
        page = self.client.get(f"/shops/{shop_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("审核", page.text)
        # 侧边栏去掉的菜单项不再出现
        sidebar = self.client.get("/self/shops").text
        for gone in ("商家管理", "1688 授权", "待开发", "API 文档"):
            self.assertNotIn(gone, sidebar)


    def test_95_list_and_form_pages(self):
        """列表页只管列表，「新建店铺 / 编辑店铺」各自独立成页（2026-10-03）。"""
        with SessionLocal() as s:
            row = shop_service.create_shop(
                s, dict(NEW_SHOP), owner_user_id=self.admin_id,
                audit_status=SHOP_AUDIT_PENDING,
            )
            shop_id = row.id

        listing = self.client.get("/self/shops")
        self.assertEqual(listing.status_code, 200)
        self.assertIn("新建店铺", listing.text)
        self.assertIn(f"/self/shops/{shop_id}/edit", listing.text)
        self.assertNotIn('name="shop_type"', listing.text, "列表页不再塞新建表单")

        # 新建页（2026-10-11 改版）：只问这 6 项，分 3 组，
        # 店铺类型也只有「个人店铺 / 鱼小铺」（别的一概不问，别拦着人开店）
        new_page = self.client.get("/self/shops/new")
        self.assertEqual(new_page.status_code, 200)
        for group in ("店铺与账号", "运营与人员", "店铺类型"):
            self.assertIn(group, new_page.text, f"新建页应有「{group}」分组")
        for field in ("name", "member_name", "phone", "operator", "contact_phone", "shop_type"):
            self.assertIn(f'name="{field}"', new_page.text, f"新建页要有「{field}」")
        self.assertIn("店铺名称（闲鱼昵称）", new_page.text)
        self.assertIn("此手机为闲鱼App的我的-设置-账号与安全的手机号", new_page.text)
        self.assertIn("个人店铺", new_page.text)
        self.assertIn("鱼小铺", new_page.text)
        # 完整档案那几组（设备 / 支付宝 / 信用分…）不在新建页
        self.assertNotIn("资金与回款", new_page.text)
        self.assertNotIn('name="service_fee_rate"', new_page.text)

        # 编辑页：同一份分组表单，预填这条店铺
        edit_page = self.client.get(f"/self/shops/{shop_id}/edit")
        self.assertEqual(edit_page.status_code, 200)
        self.assertIn("渔货栈小店", edit_page.text)
        self.assertIn(f'action="/self/shops/{shop_id}/edit"', edit_page.text)

        # 老链接（列表页带 ?edit=N）301 到编辑页
        legacy = self.client.get(f"/self/shops?edit={shop_id}", follow_redirects=False)
        self.assertEqual(legacy.status_code, 301)
        self.assertEqual(legacy.headers.get("location"), f"/self/shops/{shop_id}/edit")

        # 不存在 / 别人的店：编辑页退回列表并提示
        missing = self.client.get("/self/shops/999999/edit", follow_redirects=False)
        self.assertEqual(missing.status_code, 303)


if __name__ == "__main__":
    unittest.main()
