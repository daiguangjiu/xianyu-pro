"""规格里的「价与库存」口径（2026-10-04 用户拍板）：

1. 发布页步骤 5 上半改成**批量设置**：成本价 / 渔货价 / 建议售价 / 市场价 / 库存
   （成本价不再是必输，总库存与库存管理两个字段撤掉，新加的规格默认带这套值）；
2. 每个规格行都有 成本价 / 渔货价 / 建议售价 / 市场价 / 库存 / SKU 编码，
   渔货价与建议售价必填，其余留空就留空；
3. 留空的价在渔枢系统详情页与渔货栈详情页**都不显示假数据**（不显示 ¥0.00，给「待定」/「—」）；
4. 总库存 = 各规格库存之和（一条都没填才用商品级那个值）。
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
from app.models import Product, ProductCategory, ProductSku  # noqa: E402
from app.services import product as product_service  # noqa: E402
from app.services import sourcing_publish as sourcing_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.models import SupplierItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
CATS = ["家用电器", "手机配件"]


def _reset() -> None:
    with SessionLocal() as s:
        s.query(SupplierItem).filter(SupplierItem.item_id.like("hub-prd-%")).delete(
            synchronize_session=False
        )
        s.query(ProductSku).delete()
        s.query(Product).filter(Product.code.like("SPD8%")).delete(synchronize_session=False)
        s.query(ProductCategory).filter(ProductCategory.name.in_(CATS)).delete(
            synchronize_session=False
        )
        s.commit()


def _categories() -> None:
    with SessionLocal() as s:
        top = ProductCategory(level=1, name="家用电器", sort_order=1, source_id="digital")
        s.add(top)
        s.flush()
        s.add(
            ProductCategory(
                level=2, parent_id=top.id, name="手机配件", sort_order=1, source_id="dg_phone"
            )
        )
        s.commit()


class SkuPriceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self) -> None:
        _reset()
        _categories()

    def tearDown(self) -> None:
        _reset()

    # ---------------------------------------------------------- 逐规格的五个字段
    def test_10_sku_carries_all_five_fields(self):
        """规格行上的成本价 / 渔货价 / 建议售价 / 市场价 / 库存分别落库。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s,
                {
                    "name": "五价齐全",
                    "code": "SPD8001",
                    "category": "家用电器 / 手机配件",
                    "cost_amount": "60",
                    "fish_price": "88",
                    "price": "199",
                    "original_price": "299",
                    "stock": "7",
                    "skus_json": (
                        '[{"spec":"黑","cost":"61","fish":"89","price":"199","market":"299","stock":"5"},'
                        '{"spec":"白","cost":"62","fish":"90","price":"219","market":"309","stock":"3"}]'
                    ),
                },
            )
            skus = product_service.list_skus(s, product.id)
            self.assertEqual(len(skus), 2)
            self.assertEqual(float(skus[0].cost_amount), 61.0)
            self.assertEqual(float(skus[0].fish_price), 89.0)
            self.assertEqual(float(skus[0].price), 199.0)
            self.assertEqual(float(skus[0].market_price), 299.0)
            self.assertEqual(int(skus[0].stock), 5)
            self.assertEqual(float(skus[1].fish_price), 90.0)
            # 总库存 = 各规格之和（5 + 3）
            self.assertEqual(int(product.stock), 8)

    def test_15_sku_falls_back_to_batch_values(self):
        """规格行留空就继承批量设置（商品级）的值 —— 渔货价与建议售价不会空。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s,
                {
                    "name": "只填批量设置",
                    "code": "SPD8002",
                    "cost_amount": "60",
                    "fish_price": "88",
                    "price": "199",
                    "original_price": "299",
                    "stock": "7",
                    "skus_json": '[{"spec":"黑"},{"spec":"白","fish":"95"}]',
                },
            )
            skus = product_service.list_skus(s, product.id)
            self.assertEqual(float(skus[0].fish_price), 88.0)
            self.assertEqual(float(skus[0].price), 199.0)
            self.assertEqual(float(skus[0].cost_amount), 60.0)
            self.assertEqual(float(skus[0].market_price), 299.0)
            self.assertEqual(int(skus[0].stock), -1, "库存留空 = 不单独管")
            # 行里填了就用行里的
            self.assertEqual(float(skus[1].fish_price), 95.0)
            # 库存一条都没填 → 商品级那个 7 留着
            self.assertEqual(int(product.stock), 7)

    def test_20_fish_price_falls_back_to_cost(self):
        """渔货价没填（批量也没有）时按口径派生：渔货价 = 成本 × 1.15，建议售价 = 渔货价 × 1.35。

        （2026-10-07 改口径前是「渔货价回落成本价、售价回落渔货价」，即 45 / 45。）
        """
        with SessionLocal() as s:
            product = product_service.create_product(
                s,
                {"name": "只有成本价", "code": "SPD8003", "cost_amount": "45",
                 "skus_json": '[{"spec":"默认"}]'},
            )
            sku = product_service.list_skus(s, product.id)[0]
            self.assertEqual(float(sku.fish_price), 51.75)
            self.assertEqual(float(sku.price), 69.86)

    def test_25_edit_keeps_and_updates_all_fields(self):
        """编辑页：五个字段都能改，SKU 编码跟着商家编码派生。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s,
                {"name": "编辑五价", "code": "SPD8004", "fish_price": "80", "price": "180",
                 "skus_json": '[{"spec":"默认","fish":"80","price":"180"}]'},
            )
            product_service.sync_skus_json(
                s,
                product,
                '[{"spec":"默认","cost":"50","fish":"85","price":"199","market":"259","stock":"4"}]',
            )
            sku = product_service.list_skus(s, product.id)[0]
            self.assertEqual(float(sku.cost_amount), 50.0)
            self.assertEqual(float(sku.fish_price), 85.0)
            self.assertEqual(float(sku.price), 199.0)
            self.assertEqual(float(sku.market_price), 259.0)
            self.assertEqual(int(sku.stock), 4)
            self.assertEqual(int(product.stock), 4)
            self.assertEqual(sku.sku_code, f"{product.code}-01")

    def test_30_edit_state_and_listing_package(self):
        """编辑态回填五个字段；上架预览的规格价目带渔货价 / 建议售价 / 市场价 / SKU 编码。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s,
                {"name": "编辑态五价", "code": "SPD8005", "fish_price": "80", "price": "180",
                 "skus_json": '[{"spec":"默认","cost":"50","fish":"85","price":"199","market":"259","stock":"4"}]'},
            )
            skus = product_service.list_skus(s, product.id)
            state = product_service.edit_state(product, skus)
            self.assertEqual(state["skus"][0]["fish"], "85")
            self.assertEqual(state["skus"][0]["market"], "259")
            self.assertEqual(state["skus"][0]["cost"], "50")
            package = product_service.listing_package(product, skus)
            row = package["skus"][0]
            self.assertEqual(row["fish_price"], 85.0)
            self.assertEqual(row["price"], 199.0)
            self.assertEqual(row["market_price"], 259.0)
            self.assertEqual(row["sku_code"], f"{product.code}-01")

    # ---------------------------------------------------------- 页面：空值不显示
    def test_40_detail_hides_empty_prices(self):
        """渔枢系统详情页：空的价整项不渲染（不显示 ¥0.00）。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s, {"name": "只填建议售价", "code": "SPD8006", "price": "199"}
            )
            code = product.code
        html = self.client.get(f"/products/{code}").text
        self.assertIn('<span class="k">建议售价</span>', html)
        self.assertNotIn('<span class="k">成本价</span>', html)
        self.assertNotIn('<span class="k">渔货价</span>', html)
        self.assertNotIn('<span class="k">市场价（划线价）</span>', html)

    def test_45_new_page_has_batch_block(self):
        """发布页：批量设置五个字段 + 逐规格六个字段，库存管理字段撤掉。"""
        html = self.client.get("/products/new").text
        self.assertIn("批量设置", html)
        self.assertIn("批量设置到全部规格", html)
        self.assertNotIn('id="stockEnabledInput"', html, "库存管理字段撤掉了")
        self.assertNotIn("总库存", html, "总库存字段撤掉了（库存跟着规格走）")
        for key in ("cost", "fish", "price", "market", "stock"):
            self.assertIn(f"{key}: '", html, f"前端 SKU 行字段该有 {key}")
        self.assertIn("skuCodeText", html)

    # ---------------------------------------------------------- 渔货栈同步
    def test_50_self_detail_shows_pending_when_no_fish_price(self):
        """渔货价没定（0）时渔货栈不显示 ¥0.00，显示「待定」。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s,
                {"name": "没有渔货价", "code": "SPD8007", "category": "家用电器 / 手机配件",
                 "price": "199", "audit_status": "approved"},
            )
            item_id = sourcing_service.item_id_for(product)
            ok, msg = sourcing_service.publish(s, product)
            self.assertTrue(ok, msg)
            s.commit()
        html = self.client.get(f"/self/goods/detail/{item_id}").text
        self.assertIn("待定", html)
        self.assertNotIn('<span class="val">¥0.00</span>', html)

    def test_55_self_detail_shows_price_when_priced(self):
        """有渔货价时渔货栈正常显示数字（对照组：不是一律显示「待定」）。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s,
                {"name": "有渔货价", "code": "SPD8009", "category": "家用电器 / 手机配件",
                 "fish_price": "88", "price": "199"},
            )
            item_id = sourcing_service.item_id_for(product)
            ok, msg = sourcing_service.publish(s, product)
            self.assertTrue(ok, msg)
            s.commit()
        html = self.client.get(f"/self/goods/detail/{item_id}").text
        self.assertIn("¥88.00", html)
        self.assertNotIn(">待定</span>", html)

    # ---------------------------------------------------------- 老数据回填
    def test_70_migration_backfills_sku_prices(self):
        """一次性迁移：老规格行没有渔货价 / 市场价，按商品级的价往回填（只填空的）。"""
        from app.db import _migrate_sku_prices, engine

        with SessionLocal() as s:
            product = Product(
                name="老规格行", code="SPD8010", cost_amount=30.0, fish_price=45.5,
                price=99.0, original_price=169.0, stock=10,
            )
            s.add(product)
            s.commit()
            # 老数据：规格行上只有编码与售价，渔货价 / 市场价 / 成本都是 0
            s.add(ProductSku(product_id=product.id, spec="黄", sku_code="OLD-1", price=0, stock=12))
            s.add(ProductSku(product_id=product.id, spec="红", sku_code="OLD-2",
                             price=109.0, fish_price=48.0, stock=8))
            s.commit()
            pid = int(product.id)

        with engine.begin() as conn:
            conn.exec_driver_sql(
                "DELETE FROM schema_migrations WHERE name = '2026-10-product-sku-prices'"
            )
        _migrate_sku_prices()

        with SessionLocal() as s:
            rows = s.scalars(
                select(ProductSku).where(ProductSku.product_id == pid).order_by(ProductSku.id)
            ).all()
            self.assertEqual(float(rows[0].fish_price), 45.5, "空着的按商品渔货价填")
            self.assertEqual(float(rows[0].cost_amount), 30.0)
            self.assertEqual(float(rows[0].price), 99.0, "售价空着按商品建议售价填")
            self.assertEqual(float(rows[0].market_price), 169.0)
            # 已经填过的不动
            self.assertEqual(float(rows[1].fish_price), 48.0)
            self.assertEqual(float(rows[1].price), 109.0)

    def test_60_self_sku_list_keeps_prices(self):
        """上架到渔货栈：sku_list 带渔货价 / 建议售价 / 市场价，留空的带 0。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s,
                {"name": "带价上架", "code": "SPD8008", "category": "家用电器 / 手机配件",
                 "fish_price": "88", "price": "199", "original_price": "299",
                 "skus_json": '[{"spec":"黑","fish":"89","price":"199","market":"299","stock":"5"}]'},
            )
            code = product.code
            ok, msg = sourcing_service.publish(s, product)
            self.assertTrue(ok, msg)
            s.commit()
        with SessionLocal() as s:
            item = s.scalars(select(SupplierItem).where(SupplierItem.item_id.like("hub-prd-%"))).first()
            sku = item.sku_list["skus"][0]
            self.assertEqual(sku["price"], 89.0, "price 是渔货价（渔货栈进货价）")
            self.assertEqual(sku["sale_price"], 199.0)
            self.assertEqual(sku["market_price"], 299.0)
            self.assertEqual(sku["code"], f"{code}-01")


if __name__ == "__main__":
    unittest.main()
