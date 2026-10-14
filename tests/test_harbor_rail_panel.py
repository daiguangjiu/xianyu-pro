"""货源广场类目栏（2026-10-04 用户拍板）：

1. 类目栏只摆前 10 个一级类目 —— 连第 1 格「全部货源」正好 6 行（两列网格），
   末尾一个「更多类目」点开显示全部未停用类目、文案变「收起类目」；
2. 鼠标移到某个一级类目上，展开的二级 + 三级面板要跟点进该类目看到的一致，
   二级 / 三级都能点着选下去（三级一共一万多个，页面只带当前展开的那个一级，
   其余悬停时按需拉 /harbor/leaves —— 拉一次就缓存）；
3. 类目栏展开后整行被拉高，趋势飙升榜 / 采购节点把多出来的商品补上（按高度摆完整）；
4. 经营卡不再重复显示「订单 N 单 / 采购 N 单 / 失败 N 单」（用户截图红框处）。
"""
from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ProductCategory  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.goods.catalog import reset_hub_caches  # noqa: E402
from src.models import SupplierItem  # noqa: E402
from src.web.router import (  # noqa: E402
    HARBOR_NODE_LIMIT,
    HARBOR_RAIL_VISIBLE,
    HARBOR_TREND_LIMIT,
)

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(ProductCategory).delete()
        s.query(SupplierItem).delete()
        s.commit()


def _add_tops(count: int) -> None:
    with SessionLocal() as s:
        for i in range(count):
            s.add(ProductCategory(parent_id=None, level=1, name=f"类目{i:02d}", sort_order=i))
        s.commit()
    reset_hub_caches()


def _add_shoe_tree() -> None:
    with SessionLocal() as s:
        top = ProductCategory(parent_id=None, level=1, name="鞋", sort_order=0)
        s.add(top)
        s.flush()
        sub = ProductCategory(parent_id=top.id, level=2, name="男鞋", sort_order=0)
        s.add(sub)
        s.flush()
        for i, name in enumerate(["男式休闲皮鞋", "男式商务皮鞋"]):
            s.add(ProductCategory(parent_id=sub.id, level=3, name=name, sort_order=i))
        s.commit()
    reset_hub_caches()


def _add_items(count: int) -> None:
    with SessionLocal() as s:
        for i in range(count):
            s.add(
                SupplierItem(
                    item_id=f"it-{i:03d}",
                    title=f"测试货源{i:03d}",
                    category_id="",
                    cost_price=Decimal("10.00"),
                    stock=100,
                    is_listed=True,
                    audit_status="approved",
                    last_synced_at=datetime.now(timezone.utc),
                )
            )
        s.commit()


class HarborBase(unittest.TestCase):
    def setUp(self):
        init_db()
        _wipe()
        ensure_default_admin()
        reset_hub_caches()
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)

    def tearDown(self):
        _wipe()
        reset_hub_caches()


class LeavesApi(HarborBase):
    """三级类目清单接口：悬停别的一级时前端靠它补全。"""

    def test_10_tree_shape(self):
        _add_shoe_tree()
        resp = self.client.get("/self/harbor/leaves")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["fold_limit"], 12)
        self.assertEqual(data["tree"]["鞋"]["男鞋"], ["男式休闲皮鞋", "男式商务皮鞋"])
        self.assertEqual(data["tree"]["鞋"]["男鞋"][0], "男式休闲皮鞋")

    def test_20_page_carries_lazy_hook(self):
        """页面带上三级面板的懒加载钩子（URL / data-cat / 渲染函数）。"""
        _add_shoe_tree()
        html = self.client.get("/self/harbor").text
        self.assertIn("/self/harbor/leaves", html)
        self.assertIn('data-cat="鞋"', html)
        self.assertIn("renderLeafCols", html)
        self.assertIn("正在加载三级类目", html)


class RailFold(HarborBase):
    """类目栏：前 10 个 + 「更多类目」。"""

    def _hidden(self, html: str) -> int:
        return len(re.findall(r'class="mega-rail-item[^"]*rail-more"', html))

    def test_10_ten_visible(self):
        _add_tops(13)
        html = self.client.get("/self/harbor").text
        nav = re.search(r'<nav class="(mega-rail[^"]*)"', html).group(1)
        self.assertIn("rail-folded", nav)
        # 13 个类目 + 兜底的「其他」= 14 项，前 10 个可见（连「全部货源」正好 6 行 = 12 格）
        self.assertEqual(self._hidden(html), 13 + 1 - HARBOR_RAIL_VISIBLE)
        self.assertIn("更多类目", html)
        self.assertNotIn(">收起类目<", html)

    def test_20_open_when_active_is_folded(self):
        """选中的类目在收起的那截里 → 页面直接展开、按钮文案是「收起类目」。"""
        _add_tops(13)
        html = self.client.get("/self/harbor", params={"cat": "类目12"}).text
        nav = re.search(r'<nav class="(mega-rail[^"]*)"', html).group(1)
        self.assertIn("rail-open", nav)
        self.assertNotIn("rail-folded", nav)
        self.assertIn("收起类目", html)

    def test_30_under_limit_no_button(self):
        _add_tops(6)
        html = self.client.get("/self/harbor").text
        self.assertNotIn('id="catRailMore"', html)
        self.assertEqual(self._hidden(html), 0)

    def test_40_more_button_is_a_grid_cell(self):
        """「更多类目」自己也是类目栏的一格（两列网格里凑满 6 行）。"""
        _add_tops(13)
        html = self.client.get("/self/harbor").text
        self.assertIn('class="mega-rail-more"', html)
        self.assertIn("bi-chevron-down", html)


class SidePanels(HarborBase):
    """趋势飙升榜 / 采购节点 / 经营卡。"""

    def test_10_limits(self):
        _add_items(12)
        html = self.client.get("/self/harbor").text
        self.assertLessEqual(html.count('class="trend-item"'), HARBOR_TREND_LIMIT)
        self.assertEqual(html.count('class="trend-item"'), HARBOR_TREND_LIMIT)
        self.assertLessEqual(html.count('class="node-item"'), HARBOR_NODE_LIMIT - 1)

    def test_20_css_reveals_more_when_rail_open(self):
        """收起状态只摆放得下的，类目栏展开（:has(.rail-open)）后把多出来的补上。"""
        _add_items(12)
        html = self.client.get("/self/harbor").text
        self.assertIn(".sourcing-mega:not(:has(.mega-rail.rail-open))", html)
        self.assertIn(".trend-item:nth-child(n + 5)", html)
        self.assertIn(".node-item:nth-child(n + 3)", html)

    def test_30_no_duplicated_stats_row(self):
        """经营卡不再有「订单 N 单 / 采购 N 单 / 失败 N 单」那一行。"""
        html = self.client.get("/self/harbor").text
        self.assertNotIn('class="me-alert"', html)
        self.assertNotIn("失败 0 单", html)
        self.assertIn("我的采购单", html)


if __name__ == "__main__":
    unittest.main()
