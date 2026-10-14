"""统计页改版：供应商维度退单 / 退款金额、热销 TOP 榜单子页面、商品合并、飞书消息页权限。

业务口径：
  1 供应商维度要能看到退单单数与退款金额，占比直接用百分比（销售占比 / 毛利占比）；
  2 首页热销 TOP 只露 5 条，完整榜单在「TOP 榜单」子页面（带图表 + 商品合并）；
  3 商品合并只在统计里生效，订单原始商品名不变，删掉规则即恢复；
  4 飞书消息页（含同步）只给管理员开放，首页不再放同步按钮。
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
from app.models import Order, User  # noqa: E402
from app.services import stats as stats_service  # noqa: E402
from app.services import users as users_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
# 三个单品：前两个是同一个商品的不同写法（容量写法不一致），其中一笔是退单
SEED = (
    ("倍思Q2充电宝 10000毫安", "安徽梦想电商", ""),
    ("倍思Q2充电宝 20000毫安", "安徽梦想电商", "pending"),
    ("亚朵枕头四代全套", "南通家纺", ""),
)


class StatsPagesTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(User).filter(User.username != "admin").delete()
            s.commit()
            for i, (product, supplier, refund_status) in enumerate(SEED):
                s.add(
                    Order(
                        order_no=f"STATS-{i}",
                        product_name=product,
                        supplier_name=supplier,
                        sale_amount=100.0,
                        cost_amount=60.0,
                        quantity=1,
                        refund_status=refund_status,
                        xianyu_account="小鱼",
                        payee_name="小鱼",
                    )
                )
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(Order).delete()
            s.commit()

    def test_01_supplier_stats_has_refund_fields(self):
        """供应商维度：退单单数 + 退款金额。"""
        with SessionLocal() as s:
            row = next(
                r for r in stats_service.by_supplier(s, 8)
                if r["supplier"] == "安徽梦想电商"
            )
            self.assertEqual(row["refund_count"], 1, "要统计退单单数")
            self.assertAlmostEqual(row["refund_amount"], 100.0, places=2)

    def test_02_dashboard_shows_refund_and_percent_columns(self):
        """首页：供应商维度出现退单 / 退款金额 / 销售占比 / 毛利占比，且不再有同步按钮。"""
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("退款金额", page.text)
        self.assertIn("销售占比", page.text)
        self.assertIn("毛利占比", page.text)
        self.assertIn("chart-pie", page.text, "闲鱼号 / 归属人要能展开图表")
        self.assertNotIn("同步今日消息", page.text, "首页不再开放同步按钮")

    def test_03_dashboard_top_five_only_with_link(self):
        """首页热销 TOP 只显示 5 条，并给出完整榜单入口。"""
        page = self.client.get("/")
        self.assertIn("热销产品 TOP 5", page.text)
        self.assertIn("查看完整 TOP 榜单", page.text)
        self.assertIn("/stats/products", page.text)

    def test_04_products_page_and_merge(self):
        """TOP 榜单子页面：全量明细 + 商品合并（只影响统计，不动原始订单）。"""
        page = self.client.get("/stats/products")
        self.assertEqual(page.status_code, 200)
        self.assertIn("倍思Q2充电宝 10000毫安", page.text)

        with SessionLocal() as s:
            self.assertEqual(len(stats_service.top_products(s, limit=0)), 3)
            count = stats_service.save_product_merge(
                s,
                "倍思Q2充电宝",
                ["倍思Q2充电宝 10000毫安", "倍思Q2充电宝 20000毫安"],
                actor="tester",
            )
            self.assertEqual(count, 2)
            rows = stats_service.top_products(s, limit=0)
            merged = [r for r in rows if r["product"] == "倍思Q2充电宝"]
            self.assertEqual(len(merged), 1, "合并后只剩一个单品")
            self.assertEqual(merged[0]["count"], 2)
            self.assertEqual(
                sorted(merged[0]["members"]),
                ["倍思Q2充电宝 10000毫安", "倍思Q2充电宝 20000毫安"],
            )
            # 原始订单里的商品名保持原样
            names = {o.product_name for o in s.query(Order).all()}
            self.assertIn("倍思Q2充电宝 10000毫安", names, "合并不改原始订单数据")

        page = self.client.get("/stats/products")
        self.assertIn("合2", page.text, "合并后的行要标出合并了几种写法")

        with SessionLocal() as s:
            self.assertTrue(
                stats_service.delete_product_merge(s, "倍思Q2充电宝 10000毫安")
            )
            self.assertEqual(
                len(stats_service.top_products(s, limit=0)), 3, "取消合并后恢复原样"
            )

    def test_05_messages_page_admin_only(self):
        """飞书消息页只给管理员：管理员能进，子账号看不到入口也进不去。"""
        self.assertEqual(self.client.get("/messages").status_code, 200)
        with SessionLocal() as s:
            users_service.create_user(
                s, "staff01", "xianyu2026", role="user", display_name="员工"
            )
        sub = TestClient(app)
        resp = sub.post(
            "/login",
            data={"username": "staff01", "password": "xianyu2026"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, resp.text)
        home = sub.get("/", follow_redirects=False)
        self.assertNotIn(">飞书消息<", home.text, "非管理员导航里不显示飞书消息")
        blocked = sub.get("/messages", follow_redirects=False)
        self.assertEqual(blocked.status_code, 303, "非管理员访问飞书消息要被拦回")
