"""商品标签（2026-10-04 用户拍板）：固定标签人工勾选、动态标签按数据算。

1. **固定标签** = 「货源 · 商品标签」字典里维护的事实标签（源头工厂 / 现货速发 …），
   发布 / 编辑商品时勾选，存进 products.tags（一行一个），上架到渔货栈时一并同步过去；
2. **动态标签** = 渔货栈按商品数据当场算（今日上新 / 降价 x 元 / 高毛利 x% / 库存预警 /
   已开单），不落库、也不在渔枢系统配；
3. 勾过固定标签的商品，渔货栈**不再**按库存 / 供货商款数重复派生「现货速发」「源头工厂」；
   没勾过的（1688 货源没有「发布勾选」这一步）才走兜底派生。
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
from app.models import Product, ProductCategory, ProductTag  # noqa: E402
from app.services import sourcing_publish as sourcing_service  # noqa: E402
from app.services import tag as tag_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.models import SupplierItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _reset() -> None:
    with SessionLocal() as s:
        s.query(Product).delete()
        s.query(ProductTag).delete()
        s.query(SupplierItem).delete()
        s.query(ProductCategory).delete()
        s.commit()


def _categories() -> None:
    with SessionLocal() as s:
        digital = ProductCategory(level=1, name="家用电器", sort_order=1, source_id="digital")
        s.add(digital)
        s.flush()
        s.add(ProductCategory(level=2, parent_id=digital.id, name="手机配件",
                              sort_order=1, source_id="dg_phone"))
        s.commit()


def _feed(text: str) -> str:
    """只取 feed 流那一段（顶部运营位用的是全量货源，不算筛选结果）。"""
    start = text.find('class="fish-grid feed-grid"')
    return text[start:] if start >= 0 else text


def _tag_id(name: str) -> int:
    with SessionLocal() as s:
        row = s.scalars(select(ProductTag).where(ProductTag.name == name)).first()
        return int(row.id) if row else 0


class TagDict(unittest.TestCase):
    """标签字典：播种 + 「商品 · 商品标签」页维护。"""

    def setUp(self):
        init_db()
        _reset()
        ensure_default_admin()
        with SessionLocal() as s:
            tag_service.ensure_seeds(s)
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)

    def tearDown(self):
        _reset()

    def test_10_seeded_and_page(self):
        """内置标签播种到了，管理页能打开并列出它们。"""
        with SessionLocal() as s:
            names = [t.name for t in tag_service.list_tags(s)]
        for want in ("源头工厂", "现货速发", "工厂直供"):
            self.assertIn(want, names, f"内置标签 {want} 没播种")
        html = self.client.get("/product-tags").text
        self.assertIn("源头工厂", html)
        self.assertIn("动态标签", html, "页面上要说清楚动态标签不在这里配")

    def test_20_create_rename_and_guards(self):
        """新增 / 改名（连带商品旧名）/ 在用不可删 / 停用后发布页不再给选项。"""
        with SessionLocal() as s:
            tag, err = tag_service.create_tag(s, name="当日发货", kind="success")
        self.assertEqual(err, "")
        self.assertIsNotNone(tag)
        with SessionLocal() as s:
            _dup, err2 = tag_service.create_tag(s, name="当日发货")
        self.assertIn("已经存在", err2)

        # 商品上用了「源头工厂」，改名要把商品上的旧名一起换掉
        with SessionLocal() as s:
            s.add(Product(name="改名测试", code="TAG-RN", category="家用电器 / 手机配件",
                          source_type="self_operated", price=100.0, fish_price=60.0,
                          cost_amount=50.0, stock=5, tags="源头工厂\n现货速发"))
            s.commit()
        tid = _tag_id("源头工厂")
        self.client.post(f"/product-tags/{tid}/edit", data={"name": "源头工厂直营"})
        with SessionLocal() as s:
            row = s.scalars(select(Product).where(Product.code == "TAG-RN")).first()
        self.assertEqual(row.tags, "源头工厂直营\n现货速发", "改名没同步到商品上")

        # 还在被商品用的标签删不掉（否则商品上留下字典里没有的野标签）
        html = self.client.post(f"/product-tags/{_tag_id('源头工厂直营')}/delete",
                                follow_redirects=True).text
        self.assertIn("还有 1 款商品在用", html)
        with SessionLocal() as s:
            self.assertIsNotNone(s.scalars(
                select(ProductTag).where(ProductTag.name == "源头工厂直营")).first())

        # 停用后，发布页的候选里不再有它（已在用的商品照旧显示）
        self.client.post(f"/product-tags/{_tag_id('当日发货')}/toggle")
        publish_html = self.client.get("/products/new").text
        self.assertNotIn(">当日发货</span>", publish_html)
        self.assertNotIn("当日发货", publish_html.split('id="tagPick"')[1].split("</div>")[0])

    def test_30_hint_and_kind_survive(self):
        """配色与提示落库（详情页按配色渲染靠它）。"""
        with SessionLocal() as s:
            s.add(ProductTag(name="支持验货", kind="danger", hint="可视频验货"))
            s.commit()
        with SessionLocal() as s:
            kinds = tag_service.kind_map(s)
        self.assertEqual(kinds.get("支持验货"), "danger")


class PublishTags(unittest.TestCase):
    """发布 / 编辑商品时勾选标签 → 落库 → 详情页展示 → 上架同步到渔货栈。"""

    def setUp(self):
        init_db()
        _reset()
        _categories()
        ensure_default_admin()
        with SessionLocal() as s:
            tag_service.ensure_seeds(s)
        self.client = TestClient(app)
        self.client.post("/login", data=ADMIN, follow_redirects=False)

    def tearDown(self):
        _reset()

    def _publish_product(self) -> str:
        self.client.post(
            "/products",
            data={
                "name": "标签测试键盘", "category": "家用电器 / 手机配件",
                "source_type": "self_operated", "price": "199", "fish_price": "60",
                "cost_amount": "50", "stock": "10", "submit_action": "draft",
                "tags": "源头工厂，现货速发",
            },
            follow_redirects=False,
        )
        with SessionLocal() as s:
            row = s.scalars(select(Product).where(Product.name == "标签测试键盘")).first()
            return row.code if row else ""

    def test_10_saved_one_per_line(self):
        """逗号 / 顿号分隔的勾选值统一成一行一个存库。"""
        code = self._publish_product()
        self.assertTrue(code)
        with SessionLocal() as s:
            row = s.scalars(select(Product).where(Product.code == code)).first()
        self.assertEqual(row.tags, "源头工厂\n现货速发")
        self.assertEqual(row.tag_list, ["源头工厂", "现货速发"])

    def test_20_detail_page_shows_tags(self):
        """商品详情页把标签当商品属性展示，并按字典配色。"""
        code = self._publish_product()
        html = self.client.get(f"/products/{code}").text
        self.assertIn("商品标签", html)
        self.assertIn('<span class="ftag brand">源头工厂</span>', html)
        self.assertIn('<span class="ftag success">现货速发</span>', html)

    def test_30_edit_page_backfills(self):
        """编辑页把已选标签带回去（hidden 字段），JS 按它勾选 chips。"""
        code = self._publish_product()
        with SessionLocal() as s:
            row = s.scalars(select(Product).where(Product.code == code)).first()
            pid = row.id
        html = self.client.get(f"/products/{pid}/edit").text
        self.assertIn('id="tagsField"', html)
        self.assertIn("源头工厂", html)
        self.assertIn("syncTagField", html)
        self.assertIn("initTags()", html)

    def test_40_publish_to_harbor_syncs_tags(self):
        """上架到渔货栈：tags 同步进 supplier_items，货源广场卡片当固定标签展示。"""
        code = self._publish_product()
        with SessionLocal() as s:
            product = s.scalars(select(Product).where(Product.code == code)).first()
            ok, note = sourcing_service.publish(s, product)
            s.commit()  # publish 只改对象，提交由调用方（页面路由 / 这里）负责
        self.assertTrue(ok, note)
        with SessionLocal() as s:
            item = s.scalars(select(SupplierItem).where(SupplierItem.item_code == code)).first()
        self.assertIsNotNone(item, "渔货栈没收到这个商品")
        self.assertEqual(item.tags, "源头工厂\n现货速发")

        feed = _feed(self.client.get("/self/harbor", params={"q": "标签测试键盘"}).text)
        self.assertIn("标签测试键盘", feed, "货源广场没搜到这个商品")
        self.assertIn('<span class="ftag brand">源头工厂</span>', feed)
        self.assertIn('<span class="ftag success">现货速发</span>', feed)
        # 动态标签：渔货价 60 / 售价 199 → 毛利率 70%，卡片上要写出来
        self.assertIn("高毛利 70%", feed)

        detail = self.client.get(f"/self/goods/detail/{item.item_id}").text
        self.assertIn("商品标签", detail)
        self.assertIn("源头工厂", detail)

    def test_50_dynamic_tags_fallback_without_picked(self):
        """没勾过固定标签的货源（1688 货源没有勾选这一步）：仍按库存 / 供货商款数兜底派生。"""
        with SessionLocal() as s:
            s.add(SupplierItem(item_id="T-NOTAG-1", title="无标签的保温杯", category_id="dg_phone",
                               supplier_id="sup-x", is_listed=True, audit_status="approved",
                               fish_price=20.0, sale_price=60.0, stock=200, tags=None))
            s.commit()
        feed = _feed(self.client.get("/self/harbor", params={"q": "无标签的保温杯"}).text)
        self.assertIn("现货速发", feed, "库存 200 该兜底派生出「现货速发」")
        self.assertIn("高毛利", feed)


if __name__ == "__main__":
    unittest.main()
