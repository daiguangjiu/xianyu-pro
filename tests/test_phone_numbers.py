"""公司资源 · 手机号管理（渔枢 v2.51，2026-10-06）。

一个手机号是一条资源：谁实名、什么性质、主卡副卡、套餐怎么付、开出了哪些平台账号，
以前散在 Excel 与聊天记录里，号停用 / 换实名之后对不上账。现在落表
（app/models.py::PhoneNumber）+ 页面「系统 · 公司资源 · 手机号管理」。

本文件覆盖：菜单与页面、新增（号码归一化 / 重复与非法拦截）、顶部统计口径、
行内改「平台账号注册」、筛选与统计一致、停用与删除、管理员专属。
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
from app.models import PhoneNumber  # noqa: E402
from app.services import phone as phone_service  # noqa: E402
from app.web import nav, permissions  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _reset() -> None:
    with SessionLocal() as s:
        s.query(PhoneNumber).delete()
        s.commit()


def _add(number: str, owner: str, **extra) -> PhoneNumber:
    """直接落一条记录（不走页面，用来造统计用的数据）。"""
    with SessionLocal() as s:
        row, err = phone_service.create_phone(s, dict(number=number, owner_name=owner, **extra))
        assert err == "", err
        return row


class PhoneAssets(unittest.TestCase):
    def setUp(self):
        init_db()
        _reset()
        ensure_default_admin()
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)

    def tearDown(self):
        _reset()

    # ---------------------------------------------------------------- 菜单与页面
    def test_10_menu_and_page(self):
        """系统板块下挂「公司资源 → 手机号管理」，列表页能打开。"""
        keys = [(item.key, item.label) for section in nav.NAV for item in section.items]
        self.assertIn(("company_assets", "公司资源"), keys)
        parent = [i for section in nav.NAV for i in section.items if i.key == "company_assets"][0]
        self.assertEqual([c.label for c in parent.children], ["手机号管理"])

        html = self.client.get("/phone-numbers").text
        self.assertIn("手机号管理", html)
        self.assertIn("资源概览", html, "顶部要有统计")
        self.assertIn("新增手机号资源", html, "要有新增入口")
        # 面包屑：系统 / 公司资源 / 手机号管理
        for word in ("公司资源", "手机号管理"):
            self.assertIn(word, html)

    def test_15_new_page_grouped(self):
        """新建页字段按板块分组：号码信息 / 归属 / 套餐 / 平台账号注册情况。"""
        html = self.client.get("/phone-numbers/new").text
        for block in ("号码信息", "归属", "套餐", "平台账号注册情况"):
            self.assertIn(block, html, f"新建页要有「{block}」板块")
        for field in ("number", "owner_name", "nature", "card_type", "plan", "xianyu", "alipay", "wechat", "xiaohongshu"):
            self.assertIn(f'name="{field}"', html, f"新建页要有 {field} 字段")

    # ---------------------------------------------------------------- 新增
    def test_20_create_normalizes_number(self):
        """号码写法随便（空格 / 横线 / +86），存进去都是 11 位纯数字。"""
        for raw, want in (
            ("138 0000 0001", "13800000001"),
            ("138-0000-0002", "13800000002"),
            ("+8613800000003", "13800000003"),
        ):
            resp = self.client.post("/phone-numbers/create", data={
                "number": raw, "owner_name": "张三", "nature": "personal",
                "card_type": "main", "plan": "postpaid", "monthly_fee": "19",
                "xianyu": "1", "wechat": "1",
            }, follow_redirects=True)
            self.assertIn("已新增手机号资源", resp.text)
        with SessionLocal() as s:
            numbers = sorted(n.number for n in s.scalars(select(PhoneNumber)))
        self.assertEqual(numbers, ["13800000001", "13800000002", "13800000003"])
        with SessionLocal() as s:
            row = s.scalars(select(PhoneNumber).where(PhoneNumber.number == "13800000001")).first()
        self.assertTrue(row.xianyu and row.wechat)
        self.assertFalse(row.alipay)
        self.assertEqual(row.monthly_fee, 19.0)

    def test_30_duplicate_rejected(self):
        """同一个号不能录两遍（表上有唯一约束，页面上也要先拦一道）。"""
        self.client.post("/phone-numbers/create", data={"number": "13800000001", "owner_name": "张三"})
        resp = self.client.post("/phone-numbers/create", data={
            "number": "138 0000 0001", "owner_name": "李四",
        }, follow_redirects=True)
        self.assertIn("已经在库里了", resp.text)
        with SessionLocal() as s:
            self.assertEqual(
                len(list(s.scalars(select(PhoneNumber).where(PhoneNumber.number == "13800000001")))), 1
            )

    def test_40_bad_input_rejected(self):
        """号码不是 11 位、所属人留空都要拦下来，不能把脏数据写进台账。"""
        resp = self.client.post("/phone-numbers/create", data={
            "number": "12345", "owner_name": "张三",
        }, follow_redirects=True)
        self.assertIn("手机号要填 11 位", resp.text)

        resp = self.client.post("/phone-numbers/create", data={
            "number": "13800000009", "owner_name": "  ",
        }, follow_redirects=True)
        self.assertIn("所属人（实名人）不能为空", resp.text)
        with SessionLocal() as s:
            self.assertIsNone(s.scalars(
                select(PhoneNumber).where(PhoneNumber.number == "13800000009")).first())

    # ---------------------------------------------------------------- 统计
    def test_50_stats_match_the_list(self):
        """顶部统计与列表同一个口径：性质 / 卡类型 / 套餐 / 平台 / 月度支出 / 未开账号。"""
        _add("13800000001", "张三", nature="personal", card_type="main",
             plan="postpaid", monthly_fee=19, xianyu="1", alipay="1")
        _add("13800000002", "李四", nature="personal", card_type="sub",
             parent_number="13800000001", plan="prepaid", monthly_fee=29, balance=100,
             xianyu="1", wechat="1")
        _add("13800000003", "某公司", nature="enterprise", card_type="main",
             plan="postpaid", monthly_fee=39)

        with SessionLocal() as s:
            st = phone_service.stats(s)
        self.assertEqual(st["total"], 3)
        self.assertEqual((st["personal"], st["enterprise"]), (2, 1))
        self.assertEqual((st["main"], st["sub"]), (2, 1))
        self.assertEqual((st["postpaid"], st["prepaid"]), (2, 1))
        self.assertEqual(st["platforms"]["xianyu"], 2)
        self.assertEqual(st["platforms"]["alipay"], 1)
        self.assertEqual(st["platforms"]["wechat"], 1)
        self.assertEqual(st["platforms"]["xiaohongshu"], 0)
        self.assertEqual(st["monthly_cost"], 87.0, "月度支出 = 三个启用号的月租之和")
        self.assertEqual(st["balance"], 100.0)
        self.assertEqual(st["no_platform"], 1, "企业那个号一个平台都没开")

        html = self.client.get("/phone-numbers").text
        self.assertIn("共 3 个号", html)
        self.assertIn("¥87.00", html)
        self.assertIn("¥100.00", html)

    def test_55_stats_exclude_disabled(self):
        """停用的号不再花钱：月度支出与余额只算启用中的。"""
        row = _add("13800000001", "张三", plan="postpaid", monthly_fee=50, balance=0)
        _add("13800000002", "李四", plan="prepaid", monthly_fee=30, balance=80)
        with SessionLocal() as s:
            s.get(PhoneNumber, row.id).enabled = False
            s.commit()
            st = phone_service.stats(s)
        self.assertEqual(st["monthly_cost"], 30.0)
        self.assertEqual(st["balance"], 80.0)
        self.assertEqual(st["disabled"], 1)

    def test_60_stats_follow_the_filter(self):
        """筛了之后顶部数字跟着变（不会出现「顶部 3 个、列表 1 条」）。"""
        _add("13800000001", "张三", nature="personal")
        _add("13800000002", "某公司", nature="enterprise")
        with SessionLocal() as s:
            st = phone_service.stats(s, nature="enterprise")
        self.assertEqual(st["total"], 1)
        html = self.client.get("/phone-numbers?nature=enterprise").text
        self.assertIn("当前筛选结果", html)
        self.assertIn("共 1 个号", html)

    # ---------------------------------------------------------------- 行内改注册情况
    def test_70_inline_platform_edit(self):
        """列表页直接勾：勾上变已注册，取消勾能改回「没注册」。"""
        row = _add("13800000001", "张三", xianyu="1")
        self.client.post(f"/phone-numbers/{row.id}/edit", data={
            "xianyu": "1", "alipay": "1", "wechat": "0", "xiaohongshu": "0",
        })
        with SessionLocal() as s:
            got = s.get(PhoneNumber, row.id)
        self.assertTrue(got.xianyu and got.alipay)
        self.assertFalse(got.wechat)

        # 取消勾选：表单里带回来的就是 0（不勾的 checkbox 不会提交，靠隐藏字段兜底）
        self.client.post(f"/phone-numbers/{row.id}/edit", data={
            "xianyu": "0", "alipay": "0", "wechat": "0", "xiaohongshu": "0",
        })
        with SessionLocal() as s:
            got = s.get(PhoneNumber, row.id)
        self.assertFalse(got.xianyu, "取消勾选要能改回「没注册」")
        self.assertFalse(got.alipay)

    def test_75_edit_page_prefilled(self):
        """编辑页能打开并预填（与新建同一张表单）。"""
        row = _add("13800000001", "张三", nature="enterprise", plan="prepaid", monthly_fee=29)
        html = self.client.get(f"/phone-numbers/{row.id}/edit").text
        self.assertIn("13800000001", html)
        self.assertIn("张三", html)
        self.assertIn('value="enterprise"', html)
        self.assertIn("保存", html)
        # 改一下并保存
        self.client.post(f"/phone-numbers/{row.id}/edit", data={
            "owner_name": "张三", "plan": "postpaid", "monthly_fee": "19",
        })
        with SessionLocal() as s:
            self.assertEqual(s.get(PhoneNumber, row.id).plan, "postpaid")

    # ---------------------------------------------------------------- 停用 / 删除 / 权限
    def test_80_toggle_keeps_the_record(self):
        """停用不是删除：记录还在，只是不再计入在用的资源。"""
        row = _add("13800000001", "张三")
        self.client.post(f"/phone-numbers/{row.id}/toggle")
        with SessionLocal() as s:
            got = s.get(PhoneNumber, row.id)
        self.assertFalse(got.enabled)
        html = self.client.get("/phone-numbers").text
        self.assertIn("已停用", html)
        self.assertIn("138 0000 0001", html, "停用后记录仍在列表里")

    def test_85_delete(self):
        row = _add("13800000001", "张三")
        self.client.post(f"/phone-numbers/{row.id}/delete")
        with SessionLocal() as s:
            self.assertIsNone(s.get(PhoneNumber, row.id))

    def test_90_admin_only(self):
        """公司资源是管理员专属：默认菜单（子账号）里没有它。"""
        self.assertNotIn("company_assets", permissions.default_menu_keys())
        item = [i for section in nav.NAV for i in section.items if i.key == "company_assets"][0]
        self.assertTrue(item.admin_only)

    def test_95_idle_filter(self):
        """「未开任何平台账号」筛得出还没变成资产的号。"""
        _add("13800000001", "张三", xianyu="1")
        _add("13800000002", "李四")
        with SessionLocal() as s:
            self.assertEqual(phone_service.stats(s, idle="1")["total"], 1)
        html = self.client.get("/phone-numbers?idle=1").text
        self.assertIn("138 0000 0002", html)
        self.assertNotIn("138 0000 0001", html)


if __name__ == "__main__":
    unittest.main()
