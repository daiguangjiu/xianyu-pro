"""商品合并支持反复合并（多次合并）：

业务口径（2026-09）：
  1 运营会不断发现新的雷同产品：先并成一组，之后再把「那一组」和新的商品并进更大的一组；
  2 合并只在统计口径生效，订单原始商品名不变，删掉规则即恢复；
  3 已合并的组再被合并时，链要一路解析到最终那一组，数字不能重复也不能丢；
  4 造成互相指（A->B 同时 B->A）的规则直接拒绝，避免统计里绕圈。
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
from app.models import Order, ProductMerge  # noqa: E402
from app.services import stats as stats_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
BIG = "倍思Q2充电宝"  # 第一轮合并出来的组名
FINAL = "倍思Q2充电宝（含显示器）"  # 第二轮合并后的新组名
PRODUCTS = [
    "倍思Q2充电宝 10000毫安",
    "倍思Q2充电宝 20000毫安",
    "雷鸟32U8A显示器",
    "JBL耳机",
]


class ProductMergeChainTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(ProductMerge).delete()
            for i, product in enumerate(PRODUCTS):
                s.add(
                    Order(
                        order_no=f"CHAIN-{i}",
                        product_name=product,
                        sale_amount=100.0,
                        cost_amount=60.0,
                        quantity=1,
                    )
                )
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text)

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(ProductMerge).delete()
            s.commit()

    def _merge_twice(self, s) -> None:
        """第一轮：两个写法 -> 倍思Q2充电宝；第二轮：把这一组和显示器再并一次。"""
        first = stats_service.save_product_merge(
            s, BIG, PRODUCTS[:2], actor="tester"
        )
        self.assertEqual(first, 2, "第一轮合并 2 个写法")
        second = stats_service.save_product_merge(
            s, FINAL, [BIG, "雷鸟32U8A显示器"], actor="tester"
        )
        self.assertEqual(second, 2, "第二轮把合并组 + 新商品并进新组")

    def test_01_merge_twice_keeps_all_numbers(self):
        """反复合并：最终那一组要包含全部原始写法，数字不重不漏。"""
        with SessionLocal() as s:
            self._merge_twice(s)
            rows = stats_service.top_products(s, limit=0)
            names = [r["product"] for r in rows]
            self.assertIn(FINAL, names)
            self.assertNotIn(BIG, names, "中间组名不再单独出现")
            self.assertNotIn("雷鸟32U8A显示器", names)
            final = next(r for r in rows if r["product"] == FINAL)
            self.assertEqual(final["count"], 3, "2 个充电宝写法 + 1 个显示器")
            self.assertEqual(
                sorted(final["members"]),
                sorted(PRODUCTS[:3]),
                "成员是原始商品名，不是中间组名",
            )
            self.assertAlmostEqual(final["sale"], 300.0, places=2)
            self.assertEqual(len(rows), 2, "还有 JBL 耳机单独一行")

    def test_02_merge_twice_via_page(self):
        """页面：勾选已合并的组再合并一次，明细里能直接看到最终组。"""
        with SessionLocal() as s:
            self._merge_twice(s)
        page = self.client.get("/stats/products")
        self.assertEqual(page.status_code, 200)
        self.assertIn(FINAL, page.text)
        self.assertIn("合3", page.text, "最终组标出合并了 3 个写法")
        self.assertIn("含 2 个写法", page.text, "中间组提示它下面还有 2 个写法")

    def test_03_reject_circular_merge(self):
        """拒绝互相合并（A->B 后不许 B->A），数字保持第一次的结果。"""
        with SessionLocal() as s:
            first = stats_service.save_product_merge(
                s, "组一", ["倍思Q2充电宝 10000毫安"], actor="tester"
            )
            self.assertEqual(first, 1)
            bad = stats_service.save_product_merge(
                s, "倍思Q2充电宝 10000毫安", ["组一"], actor="tester"
            )
            self.assertEqual(bad, 0, "会造成循环的规则被跳过")
            rows = stats_service.top_products(s, limit=0)
            self.assertEqual(
                [r["product"] for r in rows].count("组一"), 1, "还是第一次那一组"
            )
            self.assertEqual(min(r["count"] for r in rows), 1)

    def test_04_delete_middle_link_restores_previous_group(self):
        """取消中间那条合并：这一组退回上一轮的分组，不会把订单弄丢。"""
        with SessionLocal() as s:
            self._merge_twice(s)
            self.assertTrue(
                stats_service.delete_product_merge(s, BIG), "取消「那一组」并进新组"
            )
            rows = stats_service.top_products(s, limit=0)
            names = [r["product"] for r in rows]
            self.assertIn(BIG, names, "两个充电宝写法退回上一轮的组名")
            big = next(r for r in rows if r["product"] == BIG)
            self.assertEqual(big["count"], 2)
            # 显示器当时是单独并进新组的，取消中间层后它仍留在新组名下
            final = next(r for r in rows if r["product"] == FINAL)
            self.assertEqual(final["count"], 1)
            self.assertEqual(sum(r["count"] for r in rows), 4, "总单数不变")

    def test_05_suggest_skips_merged_names(self):
        """已经合并过的写法不再出现在推荐里，避免重复打扰。"""
        with SessionLocal() as s:
            self._merge_twice(s)
            suggests = stats_service.suggest_product_merges(s)
            flat = [n for sg in suggests for n in sg["names"]]
            self.assertNotIn(BIG, flat, "中间组名不再推荐")
            self.assertNotIn("雷鸟32U8A显示器", flat, "已合并的写法不再推荐")
