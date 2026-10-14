"""店铺档案（闲鱼自营账号）：台账导入 + 自动建档 + 按历史订单补信息。

业务口径（2026-09）：
  · 店铺 = 闲鱼自营账号，基础信息来自运营的「闲鱼自营账号台账」；
  · 账号名称对应订单的「出货闲鱼号」，账号运营人对应飞书发送人；
  · 出单 / 导入时账号在店铺管理里没有 -> 自动补一条记录；
  · 历史订单能补的信息（回款归属人、主营供应链、运营人）回填到档案里，
    订单数 / 销售额 / 毛利 / 最近出单时间按订单实时统计。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, Shop  # noqa: E402
from app.services import shop as shop_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}

LEDGER_ROW = {
    "code": "XYSK0005",
    "name": "太阳味汽水",
    "operator": "周玉萍",
    "owner": "彩虹果冻",
    "device": "红米 10X",
    "category": "家电",
    "phone": "19121016998",
    "status": "正常运营",
    "supply": "京东家电类",
    "alipay": "19121016998",
    "credit_score": "690",
    "real_name": "周玉萍",
    "xianqi": "",
    "yuli": "",
    "violation": "无",
    "wechat": "本机",
    "wechat_password": "",
    "wechat_pay_password": "",
    "account_age": "",
    "note": "",
    "payee_name": "",
}


class ShopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self):
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(Shop).delete()
            s.commit()

    def tearDown(self):
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(Shop).delete()
            s.commit()

    def test_10_import_ledger(self):
        """台账导入：新建店铺并带上台账里的资料。"""
        with SessionLocal() as s:
            out = shop_service.import_ledger(s, [dict(LEDGER_ROW)], apply=True)
        self.assertEqual(out["created"], 1)
        with SessionLocal() as s:
            row = s.scalar(select(Shop).where(Shop.name == "太阳味汽水"))
            self.assertEqual(row.code, "XYSK0005")
            self.assertEqual(row.operator, "周玉萍", "账号运营人 = 飞书发送人")
            self.assertEqual(row.category, "家电")
            self.assertEqual(row.supply, "京东家电类")
            self.assertEqual(row.credit_score, "690")
            self.assertEqual(row.status, "正常运营")

    def test_20_import_is_idempotent(self):
        """同一份台账重复导入：不重复建档，已有的资料不被空值覆盖。"""
        with SessionLocal() as s:
            shop_service.import_ledger(s, [dict(LEDGER_ROW)], apply=True)
            row = s.scalar(select(Shop).where(Shop.name == "太阳味汽水"))
            row.category = "数码"
            s.commit()
            again = shop_service.import_ledger(s, [dict(LEDGER_ROW)], apply=True)
        self.assertEqual(again["created"], 0)
        with SessionLocal() as s:
            rows = s.scalars(select(Shop)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].category, "数码", "已维护的资料不被覆盖")

    def test_30_auto_create_by_account(self):
        """订单里的出货账号在店铺管理里没有 -> 自动建档（运营人取发送人）。"""
        with SessionLocal() as s:
            shop = shop_service.ensure_shop(
                s, "允川不吃大米饭", operator="范志强", source="群消息订单识别"
            )
            s.commit()
        self.assertIsNotNone(shop)
        self.assertEqual(shop.code, "XYSK0001", "编号接着台账往后编")
        self.assertEqual(shop.operator, "范志强")
        with SessionLocal() as s:
            # 再遇到同一个账号：不重复建档
            again = shop_service.ensure_shop(s, "允川不吃大米饭", operator="范志强")
            s.commit()
        self.assertEqual(again.id, shop.id)
        self.assertEqual(
            len([r for r in SessionLocal().scalars(select(Shop)).all()]), 1
        )

    def test_40_enrich_from_orders(self):
        """按历史订单补：回款归属人、主营供应链、运营人（只补空字段）。"""
        with SessionLocal() as s:
            s.add_all(
                [
                    Order(
                        platform="xianyu",
                        order_no="H-1",
                        xianyu_account="太阳味汽水",
                        supplier_name="安徽梦想电商（BH001）",
                        payee_name="周玉萍",
                        sender_name="周玉萍",
                        sale_amount=140.0,
                        cost_amount=110.0,
                        ordered_at=datetime(2026, 9, 24),
                    ),
                    Order(
                        platform="xianyu",
                        order_no="H-2",
                        xianyu_account="太阳味汽水",
                        supplier_name="安徽梦想电商（BH001）",
                        payee_name="周玉萍",
                        sender_name="周玉萍",
                        sale_amount=50.0,
                        cost_amount=45.0,
                        ordered_at=datetime(2026, 9, 25),
                    ),
                ]
            )
            s.add(Shop(name="太阳味汽水", code="XYSK0005"))
            s.commit()
        with SessionLocal() as s:
            shop = s.scalar(select(Shop).where(Shop.name == "太阳味汽水"))
            filled = shop_service.enrich_from_orders(s, shop)
            stat = shop_service.shop_stat(s, shop)
            s.commit()
        self.assertIn("回款归属人", filled)
        self.assertIn("账号运营人", filled)
        with SessionLocal() as s:
            shop = s.scalar(select(Shop).where(Shop.name == "太阳味汽水"))
            self.assertEqual(shop.payee_name, "周玉萍")
            self.assertEqual(shop.operator, "周玉萍")
            self.assertEqual(shop.supply, "安徽梦想电商（BH001）")
        self.assertEqual(stat["orders"], 2)
        self.assertEqual(stat["sale"], 190.0)
        self.assertEqual(stat["profit"], 35.0)
        self.assertEqual(stat["last_order_at"], datetime(2026, 9, 25))

    def test_50_backfill_creates_and_enriches(self):
        """按历史订单补齐：没建档的建出来，建档了的补信息（可反复执行）。"""
        with SessionLocal() as s:
            s.add_all(
                [
                    Order(
                        platform="xianyu",
                        order_no="H-3",
                        xianyu_account="珊瑚家的令狐冲",
                        sender_name="令狐冲",
                        payee_name="令狐冲",
                        sale_amount=99.0,
                        cost_amount=90.0,
                        ordered_at=datetime(2026, 9, 24),
                    ),
                    Order(
                        platform="xianyu",
                        order_no="H-4",
                        xianyu_account="谷神星穿蒲系的大荠菜",
                        sender_name="周玉萍",
                        sale_amount=2599.0,
                        cost_amount=2400.0,
                        ordered_at=datetime(2026, 9, 24),
                    ),
                ]
            )
            s.commit()
        with SessionLocal() as s:
            out = shop_service.backfill_from_orders(s)
        self.assertEqual(out["created"], 2)
        with SessionLocal() as s:
            names = {r.name: r for r in s.scalars(select(Shop)).all()}
        self.assertEqual(names["珊瑚家的令狐冲"].operator, "令狐冲")
        self.assertEqual(names["谷神星穿蒲系的大荠菜"].operator, "周玉萍")
        with SessionLocal() as s:
            out2 = shop_service.backfill_from_orders(s)
        self.assertEqual(out2["created"], 0, "再跑一次不重复建档")

    def test_60_pages(self):
        """店铺管理页：列表 / 新增 / 详情 / 编辑 / 删除。"""
        resp = self.client.post(
            "/shops",
            data={"name": "太阳味汽水", "code": "XYSK0005", "operator": "周玉萍"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            shop_id = s.scalar(select(Shop).where(Shop.name == "太阳味汽水")).id
        for path in ("/shops", f"/shops/{shop_id}"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        html = self.client.get("/shops").text
        self.assertIn("太阳味汽水", html)
        resp = self.client.post(
            f"/shops/{shop_id}/edit",
            data={"name": "太阳味汽水", "category": "家电"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(Shop, shop_id).category, "家电")
        # 重名不能建
        self.client.post("/shops", data={"name": "太阳味汽水"}, follow_redirects=False)
        with SessionLocal() as s:
            self.assertEqual(
                len([r for r in s.scalars(select(Shop)).all() if r.name == "太阳味汽水"]), 1
            )

    def test_65_shop_type(self):
        """店铺类型：三个固定值下拉选；默认 / 非法值都落到「个人闲置店铺」。"""
        from app.models import (
            SHOP_TYPE_CHOICES,
            SHOP_TYPE_DEFAULT,
            SHOP_TYPE_ENTERPRISE_SHOP,
            SHOP_TYPE_PERSONAL_SHOP,
        )

        self.assertEqual(
            SHOP_TYPE_CHOICES,
            ("个人闲置店铺", "个人认证鱼小铺", "企业认证鱼小铺"),
        )
        # 新建：没选就按历史口径默认「个人闲置店铺」
        self.client.post("/shops", data={"name": "默认类型店"}, follow_redirects=False)
        # 新建：选了就按选的记
        self.client.post(
            "/shops", data={"name": "企业店", "shop_type": SHOP_TYPE_ENTERPRISE_SHOP}, follow_redirects=False
        )
        with SessionLocal() as s:
            self.assertEqual(
                s.scalar(select(Shop).where(Shop.name == "默认类型店")).shop_type, SHOP_TYPE_DEFAULT
            )
            self.assertEqual(
                s.scalar(select(Shop).where(Shop.name == "企业店")).shop_type, SHOP_TYPE_ENTERPRISE_SHOP
            )
            shop_id = s.scalar(select(Shop).where(Shop.name == "默认类型店")).id

        # 编辑：下拉改成「个人认证鱼小铺」
        self.client.post(
            f"/shops/{shop_id}/edit",
            data={"name": "默认类型店", "shop_type": SHOP_TYPE_PERSONAL_SHOP},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            self.assertEqual(s.get(Shop, shop_id).shop_type, SHOP_TYPE_PERSONAL_SHOP)

        # 乱传的值（接口 / 老数据）不抛错，回落默认
        self.client.post(
            f"/shops/{shop_id}/edit",
            data={"name": "默认类型店", "shop_type": "旗舰店"},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            self.assertEqual(s.get(Shop, shop_id).shop_type, SHOP_TYPE_DEFAULT)

        # 页面上有下拉（三个选项都在），列表能按类型筛
        html = self.client.get(f"/shops/{shop_id}").text
        for t in SHOP_TYPE_CHOICES:
            self.assertIn(t, html, f"店铺详情缺店铺类型选项 {t}")
        self.assertIn("个人闲置店铺", self.client.get("/shops").text)
        self.assertIn("企业店", self.client.get(f"/shops?shop_type={SHOP_TYPE_ENTERPRISE_SHOP}").text)
        self.assertNotIn(
            "企业店",
            self.client.get(f"/shops?shop_type={SHOP_TYPE_PERSONAL_SHOP}").text,
            "按店铺类型筛选要真的过滤",
        )

    def test_66_service_fee_rate(self):
        """平台技术服务费率挂在店铺上：订单按出货店铺的费率算，店铺没设就用默认 0.6%。"""
        from app.models import SERVICE_FEE_RATE_DEFAULT
        from app.services import finance as finance_service

        self.client.post(
            "/shops", data={"name": "费率店", "service_fee_rate": "1"}, follow_redirects=False
        )
        with SessionLocal() as s:
            row = s.scalar(select(Shop).where(Shop.name == "费率店"))
            self.assertEqual(row.service_fee_rate_value, 1.0, "店铺费率填 1 就是 1%")
            self.assertEqual(row.shop_type, "个人闲置店铺", "店铺类型同时按默认记")
            # 乱填回落默认
        self.client.post(
            "/shops", data={"name": "乱填店", "service_fee_rate": "abc"}, follow_redirects=False
        )
        with SessionLocal() as s:
            self.assertEqual(
                s.scalar(select(Shop).where(Shop.name == "乱填店")).service_fee_rate_value,
                SERVICE_FEE_RATE_DEFAULT,
                "费率填了非数字 / 离谱的值回落 0.6%",
            )
            # 订单按店铺费率算：费率店 1% -> 1000 收 10 元
            s.add(Order(order_no="FEE-RATE", sale_amount=1000, xianyu_account="费率店"))
            s.flush()
            order = s.scalar(select(Order).where(Order.order_no == "FEE-RATE"))
            self.assertEqual(finance_service.sync_service_fee(s, order), 10.0, "1000 × 1% = 10")
            # 没建档的店铺（账号对不上）走默认 0.6%
            s.add(Order(order_no="FEE-DEFAULT", sale_amount=1000, xianyu_account="没建档的号"))
            s.flush()
            order2 = s.scalar(select(Order).where(Order.order_no == "FEE-DEFAULT"))
            self.assertEqual(finance_service.sync_service_fee(s, order2), 6.0, "1000 × 0.6% = 6")
            s.rollback()

    def test_70_order_page_links_shop(self):
        """订单详情页的出货账号能链到店铺档案。"""
        with SessionLocal() as s:
            s.add(Shop(name="太阳味汽水", code="XYSK0005", operator="周玉萍"))
            order = Order(
                platform="xianyu",
                order_no="H-5",
                xianyu_account="太阳味汽水",
                ordered_at=datetime(2026, 9, 24),
            )
            s.add(order)
            s.commit()
            shop_id = s.scalar(select(Shop).where(Shop.name == "太阳味汽水")).id
            order_id = order.id
        html = self.client.get(f"/orders/{order_id}").text
        self.assertIn(f"/shops/{shop_id}", html)

    def test_80_edit_order_creates_shop(self):
        """订单页改出货账号：店铺管理里没有的自动建档。"""
        with SessionLocal() as s:
            order = Order(
                platform="xianyu", order_no="H-6", xianyu_account="", ordered_at=datetime(2026, 9, 24)
            )
            s.add(order)
            s.commit()
            order_id = order.id
        resp = self.client.post(
            f"/orders/{order_id}/edit",
            data={"xianyu_account": "拾光好物仓", "sender_name": "范志强"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            row = s.scalar(select(Shop).where(Shop.name == "拾光好物仓"))
            self.assertIsNotNone(row, "订单页维护的账号要自动建档")
            self.assertEqual(row.operator, "范志强")


if __name__ == "__main__":
    unittest.main()
