"""类目管理的拖拽排序 + 渔货栈货源类目栏（「其他」兜底 / 超过 15 个收起 / 图标有效）。

2026-10-04 用户拍板：
  1. 渔枢系统「类目管理」的一级类目要能**拖着排序**（一级有几十个，逐个点「上移」太累），
     顺序就是前台（发布商品类目级联、渔货栈货源广场）的展示与选择顺序；
  2. 渔货栈的「货源类目」栏要有一个**「其他」**收底（认不出类目的货源不至于没地方去），
     类目**超过 15 个默认收起**、末尾给「…」，点开看全部；
  3. 类目图标按类目语义生成，但生成的图标名必须在项目自带的 bootstrap-icons 里真实存在
     —— 名字写错了页面上就是一个空白方块。
"""
from __future__ import annotations

import csv
import re
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
from app.models import ProductCategory  # noqa: E402
from app.services import category as category_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.goods.catalog import (  # noqa: E402
    SOURCING_CATEGORY_FALLBACK,
    SOURCING_CATEGORY_TREE,
    category_icon,
    reset_hub_caches,
    sourcing_tree,
)
from src.web.router import HARBOR_RAIL_VISIBLE  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
CSV_PATH = ROOT / "scripts" / "data" / "1688-category-tree.csv"


def _reset() -> None:
    with SessionLocal() as s:
        s.query(ProductCategory).delete()
        s.commit()


def _names_of_top_level() -> list[str]:
    """CSV 里的一级类目名（真实 52 个）。"""
    names: list[str] = []
    seen: set[str] = set()
    with CSV_PATH.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("level1_name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


class CategoryDragOrder(unittest.TestCase):
    """类目管理页：一级类目拖拽排序。"""

    def setUp(self):
        init_db()
        _reset()
        ensure_default_admin()
        reset_hub_caches()
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)

    def tearDown(self):
        _reset()
        reset_hub_caches()

    def test_10_reorder_tops(self):
        """按传上来的 id 顺序重排一级：树里的顺序跟着变。"""
        with SessionLocal() as s:
            for i, name in enumerate(["甲", "乙", "丙", "丁"]):
                s.add(ProductCategory(parent_id=None, level=1, name=name, sort_order=i))
            s.commit()
            ids = {n.name: int(n.id) for n in s.scalars(select(ProductCategory)).all()}

        with SessionLocal() as s:
            self.assertTrue(
                category_service.reorder_nodes(
                    s, [ids["丁"], ids["甲"], ids["丙"], ids["乙"]]
                )
            )
            self.assertEqual(
                [n["name"] for n in category_service.tree(s)], ["丁", "甲", "丙", "乙"]
            )

    def test_20_reorder_keeps_leftovers(self):
        """没拖进列表里的同级类目保持相对顺序排在后面（不会乱序）。"""
        with SessionLocal() as s:
            for i, name in enumerate(["甲", "乙", "丙"]):
                s.add(ProductCategory(parent_id=None, level=1, name=name, sort_order=i))
            s.commit()
            ids = {n.name: int(n.id) for n in s.scalars(select(ProductCategory)).all()}
            category_service.reorder_nodes(s, [ids["丙"]])  # 只提交了一个：它排到最前
        with SessionLocal() as s:
            self.assertEqual(
                [n["name"] for n in category_service.tree(s)], ["丙", "甲", "乙"]
            )

    def test_30_reorder_rejects_cross_level(self):
        """跨层拖拽不认：一级和二级混在一个列表里直接拒绝。"""
        with SessionLocal() as s:
            top = ProductCategory(parent_id=None, level=1, name="甲", sort_order=0)
            s.add(top)
            s.flush()
            s.add(ProductCategory(parent_id=top.id, level=2, name="甲二", sort_order=0))
            s.commit()
            ids = [int(n.id) for n in s.scalars(select(ProductCategory)).all()]
            self.assertFalse(category_service.reorder_nodes(s, ids))

    def test_40_reorder_route(self):
        """拖拽后页面提交的那个接口：表单 ids → JSON ok。"""
        with SessionLocal() as s:
            for i, name in enumerate(["甲", "乙", "丙"]):
                s.add(ProductCategory(parent_id=None, level=1, name=name, sort_order=i))
            s.commit()
            ids = [int(n.id) for n in s.scalars(select(ProductCategory)).all()]

        resp = self.client.post(
            "/products/categories/reorder", data={"ids": ",".join(str(i) for i in reversed(ids))}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        with SessionLocal() as s:
            self.assertEqual(
                [n["name"] for n in category_service.tree(s)], ["丙", "乙", "甲"]
            )

    def test_50_page_has_drag_handle(self):
        """管理页：每个一级卡片都带拖拽手柄与 data-id（拖完才知道提交什么）。"""
        with SessionLocal() as s:
            for i, name in enumerate(["甲", "乙"]):
                s.add(ProductCategory(parent_id=None, level=1, name=name, sort_order=i))
            s.commit()
        page = self.client.get("/products/categories")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.text.count('class="cat-drag"'), 2)
        self.assertIn("data-id=", page.text)
        self.assertIn("/products/categories/reorder", page.text)
        self.assertIn("按住", page.text)  # 说明里写了怎么拖


def _rail_nav_class(html: str) -> str:
    """左侧类目栏 <nav> 的 class（页面 CSS 里也有 rail-folded 字样，别串台）。"""
    m = re.search(r'<nav class="(mega-rail[^"]*)"', html)
    return m.group(1) if m else ""


def _rail_hidden_count(html: str) -> int:
    """被收起的类目条数（「…」按钮自己的 class 也叫 mega-rail-more，别算进去）。"""
    return len(re.findall(r'class="mega-rail-item[^"]*rail-more"', html))


def _rail_names(html: str) -> list[str]:
    """只取左侧「货源类目」栏里的类目名（页面别处也有 class="nm"，别串台）。"""
    start = html.find('id="catRail"')
    end = html.find("</nav>", start)
    seg = html[start:end] if start >= 0 else html
    return re.findall(r'class="nm">([^<]+)<', seg)


class HarborRail(unittest.TestCase):
    """渔货栈货源类目栏：「其他」兜底 + 超过 15 个收起。"""

    def setUp(self):
        init_db()
        _reset()
        ensure_default_admin()
        reset_hub_caches()
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)

    def tearDown(self):
        _reset()
        reset_hub_caches()

    def _add_tops(self, count: int) -> None:
        with SessionLocal() as s:
            for i in range(count):
                s.add(
                    ProductCategory(
                        parent_id=None, level=1, name=f"类目{i:02d}", sort_order=i
                    )
                )
            s.commit()
        reset_hub_caches()

    def test_10_other_is_last_in_rail(self):
        """类目栏有「其他」，且永远排在最后（兜底项不该抢在真类目前面）。"""
        self._add_tops(6)
        with SessionLocal() as s:
            s.add(ProductCategory(parent_id=None, level=1, name="其他", sort_order=99))
            s.commit()
        reset_hub_caches()
        html = self.client.get("/self/harbor").text
        names = _rail_names(html)
        self.assertIn(SOURCING_CATEGORY_FALLBACK, names)
        self.assertEqual(names[-1], SOURCING_CATEGORY_FALLBACK)

    def test_15_other_added_when_missing(self):
        """类目表里没有「其他」也补一个（回落内置常量 / 运营删了都算）。"""
        self._add_tops(3)
        html = self.client.get("/self/harbor").text
        names = _rail_names(html)
        self.assertIn(SOURCING_CATEGORY_FALLBACK, names)
        self.assertEqual(names[-1], SOURCING_CATEGORY_FALLBACK)

    def test_20_rail_folds_over_limit(self):
        """超过可见上限（10 个）：默认收起，末尾给「更多类目」按钮。"""
        self._add_tops(20)
        html = self.client.get("/self/harbor").text
        self.assertIn("rail-folded", _rail_nav_class(html))
        self.assertIn('id="catRailMore"', html)
        # 收起的条目数 = 总数 − 可见 10（20 个类目 + 「其他」是第 21 个）
        self.assertEqual(_rail_hidden_count(html), 20 + 1 - HARBOR_RAIL_VISIBLE)

    def test_25_rail_no_toggle_under_limit(self):
        """没超过可见上限就不出现「更多类目」，别平白多一个按钮。"""
        self._add_tops(8)
        html = self.client.get("/self/harbor").text
        self.assertNotIn('id="catRailMore"', html)
        self.assertEqual(_rail_hidden_count(html), 0)

    def test_30_active_in_folded_part_opens(self):
        """选中的类目正好在收起的那截里 → 页面直接展开，别让用户找不到自己在哪。"""
        self._add_tops(20)
        html = self.client.get("/self/harbor", params={"cat": "类目18"}).text
        nav_class = _rail_nav_class(html)
        self.assertIn("rail-open", nav_class)
        self.assertNotIn("rail-folded", nav_class)


class HarborLeaves(unittest.TestCase):
    """三级类目：渔枢类目管理里有的，渔货栈就得看得到（2026-10-04 用户反馈「男鞋」下三级不见了）。"""

    def setUp(self):
        init_db()
        _reset()
        ensure_default_admin()
        reset_hub_caches()
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)

    def tearDown(self):
        _reset()
        reset_hub_caches()

    def _shoe_tree(self, leaf_count: int = 20) -> None:
        """鞋 > 男鞋 > 20 个三级（最后一个「男式休闲皮鞋」按名字排在最末）。"""
        with SessionLocal() as s:
            top = ProductCategory(parent_id=None, level=1, name="鞋", sort_order=0)
            s.add(top)
            s.flush()
            sub = ProductCategory(parent_id=top.id, level=2, name="男鞋", sort_order=0)
            s.add(sub)
            s.flush()
            names = [f"A鞋{i:02d}" for i in range(leaf_count - 1)] + ["男式休闲皮鞋"]
            for i, name in enumerate(names):
                s.add(ProductCategory(parent_id=sub.id, level=3, name=name, sort_order=i))
            s.commit()
        reset_hub_caches()

    def test_10_all_leaves_rendered(self):
        """三级一次给全：排在后面的「男式休闲皮鞋」也在页面上（以前只给 12 个，它正好被截掉）。"""
        self._shoe_tree()
        html = self.client.get("/self/harbor", params={"cat": "鞋", "sub": "男鞋"}).text
        self.assertIn("男式休闲皮鞋", html)
        self.assertEqual(len(re.findall(r'class="lf-cell', html)), 20)

    def test_20_folded_over_limit(self):
        """超过 12 个的折叠起来：默认只摆 12 个，其余带 lf-hidden，末尾有「展开全部」。"""
        self._shoe_tree()
        html = self.client.get("/self/harbor", params={"cat": "鞋", "sub": "男鞋"}).text
        self.assertEqual(len(re.findall(r'class="lf-cell[^"]*lf-hidden"', html)), 20 - 12)
        self.assertIn("展开全部 20 个三级类目", html)
        self.assertIn("toggleLeafCol", html)

    def test_30_under_limit_no_button(self):
        """没超过 12 个就全摆着，不给「展开全部」按钮。"""
        self._shoe_tree(8)
        html = self.client.get("/self/harbor", params={"cat": "鞋", "sub": "男鞋"}).text
        self.assertEqual(len(re.findall(r'class="lf-cell[^"]*lf-hidden"', html)), 0)
        # 页面脚本里也有拼按钮的模板（renderLeafCols），所以按渲染出来的文案断言
        self.assertNotIn("展开全部 8 个三级类目", html)

    def test_40_picked_leaf_kept(self):
        """选中的三级无条件出现（哪怕排在很后面）。"""
        self._shoe_tree()
        html = self.client.get(
            "/self/harbor", params={"cat": "鞋", "sub": "男鞋", "l3": "男式休闲皮鞋"}
        ).text
        self.assertIn("男式休闲皮鞋", html)
        self.assertIn('class="lf-cell active', html)


class CategoryIcons(unittest.TestCase):
    """类目图标：名字必须在自带的 bootstrap-icons 里真实存在。"""

    def test_10_every_top_icon_exists(self):
        """一级类目（1688 全量 + 老的那套 + 其他）的图标都得是真图标、且不是兜底。"""
        css = (ROOT / "app" / "static" / "vendor" / "bootstrap-icons.css").read_text(
            encoding="utf-8"
        )
        have = {"bi-" + n for n in re.findall(r"\.bi-([a-z0-9-]+)::before", css)}
        names = _names_of_top_level() + [
            n["name"] for n in SOURCING_CATEGORY_TREE
        ] + [SOURCING_CATEGORY_FALLBACK]
        for name in names:
            icon = category_icon(name)
            self.assertIn(icon, have, f"「{name}」的图标 {icon} 在 bootstrap-icons 里不存在")
            self.assertNotEqual(icon, "bi-tag", f"「{name}」落到了兜底图标")

    def test_20_rule_icons_exist(self):
        """二级关键词规则里用到的图标也得存在（二级名认不出时靠它们）。"""
        from src.goods.catalog import _ICON_RULES

        css = (ROOT / "app" / "static" / "vendor" / "bootstrap-icons.css").read_text(
            encoding="utf-8"
        )
        have = {"bi-" + n for n in re.findall(r"\.bi-([a-z0-9-]+)::before", css)}
        for keyword, icon in _ICON_RULES:
            self.assertIn(icon, have, f"规则「{keyword}」的图标 {icon} 不存在")

    def test_30_tree_icons_all_valid(self):
        """真跑一遍类目树：生成的图标一个都不许是空名。"""
        css = (ROOT / "app" / "static" / "vendor" / "bootstrap-icons.css").read_text(
            encoding="utf-8"
        )
        have = {"bi-" + n for n in re.findall(r"\.bi-([a-z0-9-]+)::before", css)}
        for top in sourcing_tree():
            self.assertIn(top["icon"], have, f"一级「{top['name']}」图标无效")
            for sub in top["children"]:
                self.assertIn(sub["icon"], have, f"二级「{sub['name']}」图标无效")


if __name__ == "__main__":
    unittest.main()
