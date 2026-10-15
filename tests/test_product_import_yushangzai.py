"""渔货栈图包导入渔枢自营商品（2026-10-15 数据需求）。

导入规则（用户拍板）：
· 目录名 / 渔货号 = 商家编码（Product.code，也即新增页的条码）；
· 商品标题 / 品牌 / 型号 / 规格 / 三级类目按渔货号从渔集带；
· 渔货价 / 建议售价 / 市场价 / 库存 ← 渔集渔货价 / 建议售价 / 基准价 / 台数；
· 一组图 = 商品图片，二三四组 = 图文详情图片；默认 48 小时内发货；
· 幂等：同渔货号重导 = 覆盖更新。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  测试库配置要在任何 app 模块之前生效

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Product, ProductSku  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from wms import intake  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import WmsBatch, WmsItem  # noqa: E402

from app.services import product as product_service  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
FISH = "YS202610150001"


def _png() -> bytes:
    # 1x1 PNG（带完整 IEND），服务层导入只管落盘不验文件头
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6260000000060005"
        "27de41bc0000000049454e44ae426082")


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsBatch).delete(synchronize_session=False)
        # 先删 YS 商品的规格行（SQLite 复用 product id，残留 SKU 会串到新商品上）
        ys_ids = [int(x[0]) for x in s.query(Product.id).filter(
            Product.code.like("YS%")).all()]
        if ys_ids:
            s.query(ProductSku).filter(ProductSku.product_id.in_(ys_ids)).delete(
                synchronize_session=False)
        s.query(Product).filter(Product.code.like("YS%")).delete(
            synchronize_session=False)
        s.commit()
    folder = Path(settings.media_path) / "products"
    if folder.is_dir():
        for p in folder.glob("ys*20261015*-g*"):
            p.unlink(missing_ok=True)


class ImportYushangzaiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text[:200]

    def setUp(self):
        _wipe()

    def tearDown(self):
        _wipe()

    def _mk_fish(self, fish: str = FISH, qty: int = 2) -> None:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "purchase",
                    "qty_expected": qty, "supplier_name": "导入测试"},
                operator="tester")
            intake.register(s, batch.id, [
                {"sku_code": f"IM-{fish}-1", "product_name": "摩飞便携破壁机",
                 "fish_code": fish, "qty": qty, "brand": "摩飞",
                 "model": "MR9800", "spec": "白色 标准款",
                 "category": "HA.KC.PBJ",
                 "selling_price": 199.0, "suggest_price": 259.0,
                 "base_price": 399.0},
            ], operator="tester")

    def _groups(self) -> dict[str, list[tuple[str, bytes]]]:
        data = _png()
        return {
            "1": [("01.jpg", data), ("02.jpg", data)],
            "2": [("d1.jpg", data)],
            "3": [("d2.jpg", data)],
            "4": [("d3.jpg", data)],
        }

    def test_10_service_import_creates_product(self):
        self._mk_fish()
        with SessionLocal() as s:
            report = product_service.import_from_yushangzai(
                s, FISH, self._groups(), operator="tester")
        self.assertTrue(report["ok"] and report["created"], report)
        self.assertEqual(report["code"], FISH)
        with SessionLocal() as s:
            p = s.query(Product).filter(Product.code == FISH).one()
            self.assertEqual(p.name, "摩飞便携破壁机")
            self.assertEqual(p.brand, "摩飞")
            self.assertEqual(p.model, "MR9800")
            self.assertEqual(p.spec, "白色 标准款")
            self.assertIn("家用电器", str(p.category), "渔集对标类目要带过来")
            self.assertNotIn("›", str(p.category), "渔枢类目用「 / 」分隔")
            self.assertEqual(float(p.fish_price), 199.0)
            self.assertEqual(float(p.price), 259.0)
            self.assertEqual(float(p.original_price), 399.0)
            self.assertEqual(float(p.cost_amount), 199.0, "成本 = 渔货价")
            self.assertEqual(int(p.stock), 2, "库存 = 该渔货号台数合计")
            self.assertEqual(p.delivery_days, "48 小时内发货")
            self.assertEqual(p.status, "on_sale")
            self.assertEqual(p.audit_status, "approved")
            self.assertEqual(p.source_type, "self_operated")
            # 一组 = 商品图片：第一张主图，第二张进 images
            folder = Path(settings.media_path) / "products"
            g1 = f"/media/products/{FISH.lower()}-g1-01.jpg"
            g2 = f"/media/products/{FISH.lower()}-g1-02.jpg"
            self.assertEqual(p.image_path, g1)
            self.assertEqual(json.loads(p.images or "[]"), [g2])
            self.assertTrue((folder / f"{FISH.lower()}-g1-01.jpg").is_file())
            # 二三四组 = 详情图，按组顺序
            for i in (2, 3, 4):
                u = f"/media/products/{FISH.lower()}-g{i}-01.jpg"
                self.assertIn(f'<p><img src="{u}"', str(p.detail_html or ""))

    def test_20_reimport_is_idempotent_update(self):
        self._mk_fish()
        with SessionLocal() as s:
            product_service.import_from_yushangzai(s, FISH, self._groups())
            report = product_service.import_from_yushangzai(
                s, FISH, self._groups(), operator="tester")
        self.assertFalse(report["created"], "同号重导 = 覆盖更新")
        with SessionLocal() as s:
            count = s.query(Product).filter(Product.code == FISH).count()
            self.assertEqual(count, 1, "不重复建商品")

    def test_30_bad_inputs_rejected(self):
        with SessionLocal() as s:
            with self.assertRaises(ValueError):
                product_service.import_from_yushangzai(s, "ABC123", self._groups())
            with self.assertRaises(ValueError):
                product_service.import_from_yushangzai(
                    s, "YS209912310001", self._groups())

    def test_40_endpoint_import(self):
        self._mk_fish()
        data = _png()
        resp = self.client.post(
            "/products/import-yushangzai",
            data={"fish_code": FISH},
            files=[("g1", ("01.jpg", data, "image/jpeg")),
                   ("g1", ("02.jpg", data, "image/jpeg")),
                   ("g2", ("d1.jpg", data, "image/jpeg"))])
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        body = resp.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["main_images"], 2)
        self.assertEqual(body["detail_images"], 1)
        self.assertEqual(body["url"], f"/products/{FISH}")

    def test_50_endpoint_validations(self):
        self._mk_fish()
        data = _png()
        # 缺渔货号
        resp = self.client.post("/products/import-yushangzai",
                                files=[("g1", ("a.jpg", data, "image/jpeg"))])
        self.assertEqual(resp.status_code, 400)
        # 缺一组图
        resp = self.client.post("/products/import-yushangzai",
                                data={"fish_code": FISH},
                                files=[("g2", ("a.jpg", data, "image/jpeg"))])
        self.assertEqual(resp.status_code, 400)
        # 渔集查不到的渔货号
        resp = self.client.post("/products/import-yushangzai",
                                data={"fish_code": "YS209912310001"},
                                files=[("g1", ("a.jpg", data, "image/jpeg"))])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("渔集", resp.json()["error"])

    def test_60_multi_sku_split(self):
        """同渔货号下多个条码商品：导入默认分拆成多条规格，价格库存各带各的。"""
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "purchase",
                    "qty_expected": 2, "supplier_name": "导入测试"},
                operator="tester")
            intake.register(s, batch.id, [
                {"sku_code": "SP-A", "product_name": "多规格款破壁机",
                 "fish_code": FISH, "qty": 1, "spec": "白色",
                 "selling_price": 199.0, "suggest_price": 259.0, "base_price": 399.0},
                {"sku_code": "SP-B", "product_name": "多规格款破壁机",
                 "fish_code": FISH, "qty": 2, "spec": "黑色",
                 "selling_price": 219.0, "suggest_price": 279.0, "base_price": 419.0},
            ], operator="tester")
        with SessionLocal() as s:
            report = product_service.import_from_yushangzai(
                s, FISH, {"1": [("01.jpg", _png())]}, operator="tester")
        self.assertEqual(report["skus"], 2, report)
        with SessionLocal() as s:
            p = s.query(Product).filter(Product.code == FISH).one()
            skus = product_service.list_skus(s, int(p.id))
            self.assertEqual(len(skus), 2, "渔货号名下 2 个条码商品 → 2 条规格")
            self.assertEqual([k.spec for k in skus], ["白色", "黑色"])
            self.assertEqual([int(k.stock or 0) for k in skus], [1, 2])
            self.assertEqual([float(k.fish_price or 0) for k in skus], [199.0, 219.0])
            self.assertEqual([float(k.market_price or 0) for k in skus], [399.0, 419.0])
            self.assertEqual([k.sku_code for k in skus],
                             [f"{FISH}-01", f"{FISH}-02"], "SKU 编码由商家编码派生")
            self.assertEqual(int(p.stock), 3, "总库存 = 各规格之和")

    def test_65_single_sku_not_split(self):
        """一款只一条记录（哪怕一台带 2 台）：不分拆。"""
        self._mk_fish(qty=2)
        with SessionLocal() as s:
            report = product_service.import_from_yushangzai(
                s, FISH, {"1": [("01.jpg", _png())]}, operator="tester")
        self.assertEqual(report["skus"], 0, report)
        with SessionLocal() as s:
            p = s.query(Product).filter(Product.code == FISH).one()
            self.assertEqual(len(product_service.list_skus(s, int(p.id))), 0)
            self.assertEqual(int(p.stock), 2)

    def test_70_excluded_fish_code(self):
        """YS202610120001 用户点名不拆：即使名下多条码也保持一条库存。"""
        from app.services.product import SKU_SPLIT_EXCLUDED, split_product_skus
        self.assertIn("YS202610120001", SKU_SPLIT_EXCLUDED)
        with SessionLocal() as s:
            p = Product(name="例外款", code="YS202610120001", stock=20,
                        source_type="self_operated")
            s.add(p)
            s.flush()
            self.assertEqual(split_product_skus(s, p, items=[object(), object()]), 0)

    def test_75_price_fallback_market_half(self):
        """方案 B（2026-10-15 用户拍板）：渔集建议售价没填 → 销售价 = 市场价 × 0.5，
        不再回落渔货价（渔货价=销售价、账面毛利 0 的根源）。"""
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "purchase",
                    "qty_expected": 1, "supplier_name": "导入测试"}, operator="tester")
            intake.register(s, batch.id, [
                {"sku_code": "PF-1", "product_name": "兜底价测试破壁机",
                 "fish_code": FISH, "qty": 1,
                 "selling_price": 100.0, "suggest_price": 0, "base_price": 400.0},
            ], operator="tester")
        with SessionLocal() as s:
            report = product_service.import_from_yushangzai(
                s, FISH, {"1": [("01.jpg", _png())]}, operator="tester")
            self.assertEqual(float(report["price"]), 200.0, report)
            self.assertEqual(float(report["fish_price"]), 100.0, report)
        # SKU 行同口径：suggest 空 → base×0.5（换一个渔货号，避免同名款并号）
        with SessionLocal() as s:
            fish2 = "YS202610150075"
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "purchase",
                    "qty_expected": 2, "supplier_name": "导入测试"}, operator="tester")
            intake.register(s, batch.id, [
                {"sku_code": "PF-A", "product_name": "兜底价测试多规格",
                 "fish_code": fish2, "qty": 1, "spec": "白",
                 "selling_price": 100.0, "base_price": 400.0},
                {"sku_code": "PF-B", "product_name": "兜底价测试多规格",
                 "fish_code": fish2, "qty": 1, "spec": "黑",
                 "selling_price": 120.0, "base_price": 600.0},
            ], operator="tester")
            report = product_service.import_from_yushangzai(
                s, fish2, {"1": [("01.jpg", _png())]}, operator="tester")
            self.assertEqual(report["skus"], 2, report)
            p = s.query(Product).filter(Product.code == fish2).one()
            skus = product_service.list_skus(s, int(p.id))
            self.assertEqual([float(k.price or 0) for k in skus], [200.0, 300.0])


if __name__ == "__main__":
    unittest.main()
