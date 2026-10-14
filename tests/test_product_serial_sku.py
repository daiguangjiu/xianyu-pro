"""商品流水 ID 与「规格 · 价格 · 库存」合并（2026-10-03 定稿）：

  1 规格 / 价格 / 库存合成一块：价格与库存跟着 SKU 走，
    发布页与详情页都是「规格 · 价格 · 库存」一块（SKU 表里逐行有成本 / 售价 / 库存）；
  2 商家编码（内部编号 / SKU 前缀）系统自动生成、锁定不可改：
    XYSK + 日期 + 4 位序号，序号全局累计；SKU 编码由商家编码派生（-01 / -02…）；
  3 商品的流水 ID 就是这个商家编码（老的 YHZ-… 由一次性迁移换过来）。
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

from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Product, ProductSku  # noqa: E402
from app.services import product as product_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
MIGRATION = "2026-10-product-serial-code"


class ProductSerialSkuTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self) -> None:
        with SessionLocal() as s:
            s.query(ProductSku).delete()
            s.query(Product).delete()
            s.commit()

    # -------------------------------------------------- 1 流水 ID
    def test_10_code_is_global_serial(self):
        """流水 ID = XYSK + 日期 + 4 位序号，第 N 个商品就是 …000N（全局累计）。"""
        with SessionLocal() as s:
            first = product_service.create_product(
                s, {"name": "流水一号", "category": "家用电器 / 耳机音响", "price": "199"}
            )
            second = product_service.create_product(
                s, {"name": "流水二号", "category": "女装男装 / 男装", "price": "99"}
            )
            day = f"{datetime.now():%Y%m%d}"
            self.assertEqual(first.code, f"XYSK{day}0001")
            self.assertEqual(second.code, f"XYSK{day}0002", "换类目不重新起算")
            # 跨天也不重置：把日期改成昨天，序号仍接着走
            yesterday = datetime.now().replace(day=1)
            self.assertEqual(
                product_service.next_code(s, on=yesterday),
                f"XYSK{yesterday:%Y%m%d}0003",
            )

    def test_15_next_code_api(self):
        """/products/next-code 预览的号与实际落库的号是同一套规则。"""
        data = self.client.get("/products/next-code?category=家用电器").json()
        self.assertTrue(data["code"].startswith(f"XYSK{datetime.now():%Y%m%d}"))
        self.assertRegex(data["code"], r"^XYSK\d{12}$")

    # -------------------------------------------------- 2 SKU 编码派生
    def test_20_sku_code_derived_from_merchant_code(self):
        """SKU 编码 = 商家编码 + 两位序号（-01 / -02…），系统派生不手填。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s,
                {
                    "name": "多规格耳机",
                    "category": "家用电器 / 耳机音响",
                    "price": "199",
                    "cost_amount": "80",
                    "spec_input": "颜色: 黑, 白\n容量: 128G",
                    "skus_json": '[{"spec":"黑 / 128G","price":"199","stock":"5","code":"手填也不算"},'
                                 '{"spec":"白 / 128G","price":"219","stock":"3"}]',
                },
            )
            skus = product_service.list_skus(s, product.id)
            self.assertEqual(len(skus), 2)
            self.assertEqual(skus[0].sku_code, f"{product.code}-01")
            self.assertEqual(skus[1].sku_code, f"{product.code}-02")
            # 价格 / 库存跟着 SKU 走（成本也逐行记）
            self.assertEqual(float(skus[1].price), 219.0)
            self.assertEqual(int(skus[1].stock), 3)

    def test_25_sku_cost_follows_sku(self):
        """SKU 行里填了成本就用它，不回落商品成本价。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s,
                {
                    "name": "成本看规格",
                    "cost_amount": "80",
                    "price": "199",
                    "skus_json": '[{"spec":"大份","cost":"95","price":"219","stock":"2"}]',
                },
            )
            sku = product_service.list_skus(s, product.id)[0]
            self.assertEqual(float(sku.cost_amount), 95.0)
            self.assertEqual(float(sku.price), 219.0)

    def test_30_edit_renumbers_after_delete(self):
        """编辑保存后 SKU 编码按商家编码重排：删掉中间一行，序号不留空档。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s,
                {
                    "name": "重排测试",
                    "price": "99",
                    "skus_json": '[{"spec":"S"},{"spec":"M"},{"spec":"L"}]',
                },
            )
            codes_before = [sku.sku_code for sku in product_service.list_skus(s, product.id)]
            self.assertEqual(codes_before[-1], f"{product.code}-03")
            product_service.sync_skus_json(
                s, product, '[{"spec":"S","price":"99"},{"spec":"L","price":"119"}]'
            )
            skus = product_service.list_skus(s, product.id)
            self.assertEqual([sku.spec for sku in skus], ["S", "L"])
            self.assertEqual([sku.sku_code for sku in skus], [f"{product.code}-01", f"{product.code}-02"])
            self.assertEqual(float(skus[1].price), 119.0)

    def test_35_edit_state_carries_cost(self):
        """编辑页状态里带成本，SKU 编码是派生值（前端只读展示）。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s, {"name": "编辑态", "price": "88", "cost_amount": "40",
                    "skus_json": '[{"spec":"单规格","cost":"45"}]'}
            )
            skus = product_service.list_skus(s, product.id)
            state = product_service.edit_state(product, skus)
            self.assertEqual(state["skus"][0]["cost"], "45")
            self.assertEqual(state["skus"][0]["code"], f"{product.code}-01")
            self.assertEqual(product_service.edit_fields(product)["code"], product.code)

    # -------------------------------------------------- 3 页面：合并成一块
    def test_50_new_page_merges_price_and_sku(self):
        """发布页：规格 / 价格 / 库存合成一块，上半是批量设置，下半是逐规格的价与库存。"""
        page = self.client.get("/products/new")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn("规格 · 价格 · 库存", html)
        self.assertIn("规格与 SKU", html, "一块里仍然有规格矩阵的小标题")
        # 老的两块标题不再单独出现
        self.assertNotIn("<span class=\"step\">6</span> 价格与库存", html)
        # 批量设置五个字段（默认套用到全部规格）+ 批量按钮
        self.assertIn("批量设置", html)
        self.assertIn("批量设置到全部规格", html)
        for field in ('name="cost_amount"', 'name="fish_price"', 'name="price"', 'name="original_price"', 'name="stock"'):
            self.assertIn(field, html, f"批量设置应该有 {field}")
        # 总库存 / 库存管理两个字段撤掉
        self.assertNotIn('name="stock_enabled"', html)
        self.assertNotIn("总库存", html)
        # SKU 表头：规格组合 / 成本价 / 渔货价 / 建议售价 / 市场价 / 库存 / SKU 编码
        for text in ("规格组合", "成本价", "渔货价", "建议售价", "市场价", "库存", "SKU 编码"):
            self.assertIn(text, html)
        # 商家编码系统生成、锁定不可改
        self.assertIn('id="codeInput"', html)
        self.assertIn("readonly", html)
        self.assertIn("XYSK", html)
        # 前端的 SKU 行字段与校验都跟着改（渔货价 / 建议售价必填）
        self.assertIn("var BATCH_INPUTS", html)
        self.assertIn("渔货价与建议售价必填", html)

    def test_60_detail_page_merges_blocks(self):
        """详情页：价格库存与规格合成一块「规格 · 价格 · 库存」，SKU 编码按商家编码显示。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s,
                {"name": "详情页合并", "price": "199", "cost_amount": "80",
                 "skus_json": '[{"spec":"黑","price":"199","stock":"5"},{"spec":"白","price":"219","stock":"3"}]'},
            )
            code = product.code
        html = self.client.get(f"/products/{code}").text
        self.assertIn("规格 · 价格 · 库存", html)
        self.assertIn("价格与库存跟着 SKU 走", html)
        self.assertIn(f"{code}-01", html)
        self.assertIn(f"{code}-02", html)
        # 旧的独立「价格与库存」区块不再出现
        self.assertNotIn("bi-cash-coin\"> 价格与库存", html)
        for text in ("成本价", "渔货价", "建议售价", "市场价", "总库存", "SKU 编码"):
            self.assertIn(text, html)
        # 发布页不再单独维护这两个字段
        self.assertNotIn(">库存管理<", html)

    # -------------------------------------------------- 4 老数据迁移
    def test_70_migration_backfills_legacy_code(self):
        """一次性迁移：老编码（YHZ-… / SPD0001）换成 XYSK + 日期 + 序号，SKU 编码跟着重排。"""
        from app.db import _migrate_product_serial_code

        with SessionLocal() as s:
            old = Product(name="老编码商品", code="YHZ-JYDQ-EJYX-20260929-0001", price=10)
            s.add(old)
            s.commit()
            s.add(ProductSku(product_id=old.id, spec="默认", sku_code="OLD-SKU-1", price=10))
            s.commit()
            old_id = int(old.id)

        with engine.begin() as conn:
            conn.exec_driver_sql(f"DELETE FROM schema_migrations WHERE name = '{MIGRATION}'")
        _migrate_product_serial_code()

        with SessionLocal() as s:
            row = s.get(Product, old_id)
            self.assertTrue(row.code.startswith("XYSK"), row.code)
            self.assertRegex(row.code, r"^XYSK\d{12}$")
            sku = s.scalar(select(ProductSku).where(ProductSku.product_id == old_id))
            self.assertEqual(sku.sku_code, f"{row.code}-01")

    def test_75_migration_is_idempotent(self):
        """迁移做过一次就不再动数据（幂等，靠 schema_migrations 标记）。"""
        from app.db import _migrate_product_serial_code

        with SessionLocal() as s:
            kept = Product(name="已是新规则", code=f"XYSK{datetime.now():%Y%m%d}0077", price=1)
            s.add(kept)
            s.commit()
            kept_id = int(kept.id)

        with engine.begin() as conn:
            conn.exec_driver_sql(f"DELETE FROM schema_migrations WHERE name = '{MIGRATION}'")
        _migrate_product_serial_code()
        _migrate_product_serial_code()

        with SessionLocal() as s:
            self.assertEqual(s.get(Product, kept_id).code, f"XYSK{datetime.now():%Y%m%d}0077")


if __name__ == "__main__":
    unittest.main()
