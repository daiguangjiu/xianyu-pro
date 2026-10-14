"""渔货栈发布管理页的分页与成本价（2026-10-10）。

口径：
① 列表用全站统一分页组件（``templates/_pager.html``）：默认每页 50（可选 20/50/100/200），
   翻页保留搜索与筛选条件，页码越界自动夹取；
② 「渔货栈状态」筛选（已上架 / 未上架 / 类目对不上）**先筛后分页** —— 总条数与页数按筛完的结果算；
③ 列表多一列**成本价**（商品级 ``cost_amount``，与「商品管理」列表同口径）。
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
from app.models import Product  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.models import SupplierItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
KEY = "发布页分页用例"  # 唯一前缀：只统计本用例造的商品
TOTAL = 25


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(SupplierItem).filter(SupplierItem.item_id.like("hub-prd-%")).delete(
            synchronize_session=False
        )
        s.query(Product).filter(Product.name.like(f"{KEY}%")).delete(synchronize_session=False)
        s.commit()


def _seed() -> None:
    with SessionLocal() as s:
        for i in range(TOTAL):
            s.add(
                Product(
                    name=f"{KEY} 商品{i:02d}",
                    code=f"SPDP{i:03d}",
                    category="家用电器 / 手机配件",
                    brand="雷柏",
                    fish_price=88.0,
                    # 第一款的成本价固定，用来断言成本价列（其余各不相同）
                    cost_amount=45.5 if i == 0 else 30.0 + i,
                    price=199.0,
                    stock=10,
                    source_type="self_operated",
                    audit_status="approved",
                )
            )
        s.commit()


class SourcingPagerTest(unittest.TestCase):
    """/sourcing：成本价列 + 分页（含筛选后的分页）。"""

    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        _wipe()
        _seed()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def tearDown(self) -> None:
        _wipe()

    def _rows(self, text: str) -> int:
        """当前页渲染了几行（每行一个勾选框）。"""
        return text.count('name="product_id"')

    def test_10_cost_column_and_first_page(self):
        page = self.client.get(f"/sourcing?q={KEY}&per_page=20")
        self.assertEqual(page.status_code, 200)
        text = page.text
        self.assertIn(">成本价<", text, "列表要有成本价列")
        self.assertIn(">¥45.50<", text, "成本价按商品级 cost_amount 显示（带 ¥ 符号，与同表渔货价一致）")
        self.assertEqual(self._rows(text), 20, f"每页 {TOTAL} 条里第一页 20 条")
        self.assertIn("第 1 / 2 页", text)
        self.assertIn(f"共 {TOTAL} 条", text)
        self.assertIn(">20 条<", text, "每页条数下拉带 20 档")

    def test_20_second_page_and_clamped_page(self):
        page = self.client.get(f"/sourcing?q={KEY}&per_page=20&page=2")
        self.assertIn("第 2 / 2 页", page.text)
        self.assertEqual(self._rows(page.text), TOTAL - 20, "第二页只剩 5 条")

        # 页码越界夹到最后一页，不出现空列表
        last = self.client.get(f"/sourcing?q={KEY}&per_page=20&page=99")
        self.assertIn("第 2 / 2 页", last.text)
        self.assertEqual(self._rows(last.text), TOTAL - 20)

    def test_30_pager_keeps_filters(self):
        page = self.client.get(f"/sourcing?q={KEY}&per_page=20&state=unlisted")
        text = page.text
        # 翻页链接保留搜索词与每页条数（中文会 urlencode，所以断言编码后的键）
        self.assertIn("per_page=20", text)
        self.assertIn("state=unlisted", text)
        self.assertIn("page=2", text)

    def test_40_state_filter_paginates_after_filtering(self):
        """先按渔货栈状态筛，再分页：总条数按筛完的结果算（本用例全都没上架）。"""
        page = self.client.get(f"/sourcing?q={KEY}&state=unlisted&per_page=20")
        self.assertEqual(page.status_code, 200)
        self.assertIn(f"共 {TOTAL} 条", page.text)
        self.assertIn("第 1 / 2 页", page.text)
        self.assertEqual(self._rows(page.text), 20)


if __name__ == "__main__":
    unittest.main()
