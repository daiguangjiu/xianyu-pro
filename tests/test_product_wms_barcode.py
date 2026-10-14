"""渔枢 ↔ 渔集（2026-10-10）：新增商品扫渔集条码带出档案 + 数量 / 建议售价口径。

用户拍板的四条：
  1. 渔集的价格系数里加「建议售价系数」：建议售价 = 基准价 × 建议售价系数，
     它是渔枢 / 渔货栈建议售价的基础数据；**渔货价不能高于建议售价**；
  2. 渔枢新增商品（自营商品）多一个「商品条码」：手输 / 扫码枪都行，扫的是渔集货品的条码；
     带出商品名称、渔货价、建议售价、市场价（= 渔集基准价）；
  3. 商家编码（内部编号 / SKU 前缀）**直接复用渔集的条码**，品牌也来自渔集；
     库存 = 渔集的数量字段；
  4. 渔集的数量字段要带到各业务页面并重点显示。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Product  # noqa: E402
from app.services import wms_bridge  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402
from wms import grading, seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import WmsItem, WmsPriceRule  # noqa: E402

BARCODE = "QCHA26100999"


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(Product).delete(synchronize_session=False)
        s.query(WmsItem).delete(synchronize_session=False)
        s.commit()


class WmsBarcodeBridgeTest(unittest.TestCase):
    """扫渔集条码 → 带回名称 / 品牌 / 渔货价 / 建议售价 / 市场价 / 库存。"""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
        ensure_default_admin()
        cls.client = TestClient(app)
        cls.client.cookies.set(COOKIE_NAME, make_session(1)[0])

    def setUp(self) -> None:
        _wipe()
        with SessionLocal() as s:
            s.add(WmsItem(
                barcode=BARCODE, display_code="QC-HA-2610-0999", batch_no="2610-03",
                product_name="美的破壁机 家用多功能", brand="美的", model="MB-1",
                spec="3L", sku_code="T-1001", category="HA.KC.PBJ",
                qty=3,                       # 数量：这一条货 3 台
                base_price=1000.0,           # 基准价 = 渔枢的市场价
                purchase_cost=400.0,
                selling_price=800.0,         # 定级后落库的渔货价
                grade="S", status="SELLABLE", warehouse_code="SALE",
            ))
            s.commit()

    def tearDown(self) -> None:
        _wipe()

    def test_10_lookup_maps_fields(self):
        """字段映射：名称 / 品牌 / 渔货价 / 建议售价 / 市场价（= 基准价）/ 库存（= 数量）。"""
        row = wms_bridge.lookup_item(BARCODE)
        self.assertIsNotNone(row, "扫渔集条码要查得到")
        self.assertEqual(row["product_name"], "美的破壁机 家用多功能")
        self.assertEqual(row["brand"], "美的")
        self.assertEqual(row["fish_price"], 800.0, "渔货价 = 渔集定级后的售价")
        self.assertEqual(row["market_price"], 1000.0, "市场价 = 渔集的基准价")
        self.assertEqual(row["qty"], 3, "库存 = 渔集的数量")
        self.assertEqual(row["barcode"], BARCODE, "商家编码用这个条码")
        with SessionLocal() as s:
            rule = s.scalar(select(WmsPriceRule).where(WmsPriceRule.grade == "S"))
        self.assertAlmostEqual(row["suggest_price"], round(1000.0 * float(rule.suggest_factor), 2),
                               places=2, msg="建议售价 = 基准价 × 建议售价系数")

    def test_15_fish_never_above_suggest(self):
        """硬规矩：渔货价不能高于建议售价（带了过来也要守着）。"""
        with SessionLocal() as s:
            row = s.scalar(select(WmsPriceRule).where(WmsPriceRule.grade == "S"))
            row.suggest_factor = 0.5          # 建议售价 = 500 < 渔货价 800
            s.commit()
        got = wms_bridge.lookup_item(BARCODE)
        self.assertEqual(got["suggest_price"], 500.0)
        self.assertEqual(got["fish_price"], 500.0, "渔货价被压到建议售价")

    def test_20_api_and_page(self):
        """接口与页面：扫得到给档案，扫不到给人话提示。"""
        ok = self.client.get(f"/products/wms-item?code={BARCODE}").json()
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["item"]["qty"], 3)
        miss = self.client.get("/products/wms-item?code=NO-SUCH-CODE").json()
        self.assertFalse(miss["ok"])
        self.assertIn("渔集", miss.get("error") or "")
        # 扫货号也要认（现场更多是扫货号）
        by_sku = self.client.get("/products/wms-item?code=T-1001").json()
        self.assertTrue(by_sku["ok"], "扫货号同样认这台货")

        page = self.client.get("/products/new")
        self.assertEqual(page.status_code, 200)
        for frag in ('id="wmsCode"', "商品条码", "wmsLookup", "扫码枪"):
            self.assertIn(frag, page.text, frag)

    def test_22_picker_and_category(self):
        """条码那格能「选渔集货品」（下拉候选），并且把类目一起带过来。"""
        page = self.client.get("/products/new")
        self.assertEqual(page.status_code, 200)
        for frag in ('id="wmsCodeOptions"', "选渔集货品", "wmsSuggest", "wmsSetCategory"):
            self.assertIn(frag, page.text, frag)

        listed = self.client.get("/products/wms-items?q=").json()
        self.assertTrue(listed["ok"])
        self.assertEqual(listed["items"][0]["barcode"], BARCODE, "候选里能看到渔集在库货品")
        self.assertEqual(listed["items"][0]["qty"], 3)
        self.assertEqual(listed["items"][0]["product_name"], "美的破壁机 家用多功能")

        row = wms_bridge.lookup_item(BARCODE)
        # 类目：优先给对标好的 1688 官方名（渔枢类目树就是 1688 那套）
        self.assertEqual(row["category_1688"], ["家用电器", "厨房电器", "家用破壁机"])
        self.assertEqual(row["category_levels"], ["家用电器", "厨房电器", "家用破壁机"])
        # 渔集自己的中文名也一并给出来（对标不到时兜底）
        self.assertEqual(row["category_local"], ["家用电器", "厨房小电", "破壁机"])

    def test_24_code_is_locked_to_barcode(self):
        """商家编码锁死成渔集条码：它是两边唯一的关联键（出库要按它同步库存）。"""
        page = self.client.get("/products/new")
        self.assertIn("codeInput.setAttribute('readonly'", page.text,
                      "扫完之后商家编码要锁死，别让手改断了两边的关联")
        self.assertNotIn("codeInput.removeAttribute('readonly')", page.text)

    def test_30_create_uses_barcode_as_code(self):
        """提交后：商家编码 = 渔集条码，价格与库存都按带过来的值落库。"""
        resp = self.client.post("/products", data={
            "name": "美的破壁机 家用多功能",
            "code": BARCODE,                 # 商家编码复用渔集条码
            "brand": "美的",
            "category": "家用电器 / 厨房小电 / 破壁机",
            "fish_price": "800", "price": "1100", "original_price": "1000",
            "cost_amount": "400", "stock": "3",
            "source_type": "self_operated",
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            row = s.scalar(select(Product).where(Product.code == BARCODE))
            self.assertIsNotNone(row, "商家编码就是渔集条码")
            self.assertEqual(row.brand, "美的")
            self.assertAlmostEqual(float(row.fish_price or 0), 800.0)
            self.assertAlmostEqual(float(row.price or 0), 1100.0, places=2)
            self.assertAlmostEqual(float(row.original_price or 0), 1000.0)
            self.assertEqual(int(row.stock or 0), 3, "库存 = 渔集的数量")

    def test_40_suggest_price_from_rule(self):
        """建议售价 = 基准价 × 建议售价系数（渔枢 / 渔货栈的建议售价就以此为基础）。"""
        with SessionLocal() as s:
            rule = grading.price_rule_of(s, "S")
            self.assertIsNotNone(rule)
            rule.suggest_factor = 1.25
            s.commit()
            quote = grading.price_of(s, "S", 200.0, 80.0)
        self.assertAlmostEqual(float(quote["suggest_price"]), 250.0, places=2)
        self.assertFalse(quote["clamped_by_suggest"], "渔货价 200 < 建议售价 250")
        self.assertAlmostEqual(float(quote["price"]), round(200.0 * float(rule.factor), 2), places=2)
