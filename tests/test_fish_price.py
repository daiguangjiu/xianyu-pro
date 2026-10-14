"""渔货价：商品维度统一增加的一个价格字段。

业务口径（2026-09-28 需求）：
  · 渔货价 = 渔货栈对外的成本价 / 进货价，与「供货成本（cost_amount / cost_price）」分开记；
  · 渔枢系统（Product）与渔货栈（SupplierItem）都要有这一列，
    发布页能填、详情页能看、列表里能扫到；
  · 两端的新增字段都靠「启动补列」落地（app/db.py 与 src/deps.py），不用手工改库。
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Product  # noqa: E402
from app.services import product as product_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _client() -> TestClient:
    init_db()
    ensure_default_admin()
    client = TestClient(app)
    resp = client.post("/login", data=ADMIN, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return client


class FishPriceHubTest(unittest.TestCase):
    """渔枢系统：发布表单填渔货价 → 落到商品上 → 详情页与列表能看到。"""

    def setUp(self):
        init_db()
        self.client = _client()
        with SessionLocal() as db:
            db.query(Product).filter(Product.name == "渔货价测试商品").delete()
            db.commit()

    def tearDown(self):
        with SessionLocal() as db:
            db.query(Product).filter(Product.name == "渔货价测试商品").delete()
            db.commit()

    def test_10_form_saves_fish_price(self):
        with SessionLocal() as db:
            product = product_service.create_product(
                db,
                {
                    "name": "渔货价测试商品",
                    "cost_amount": "80",
                    "fish_price": "¥88.50",
                    "price": "129",
                },
            )
            self.assertEqual(round(float(product.fish_price), 2), 88.5)
            # 与供货成本是两个字段，互不覆盖
            self.assertEqual(round(float(product.cost_amount), 2), 80.0)
            product_id = product.id

        page = self.client.get(f"/products/{product_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("渔货价", page.text)
        self.assertIn("88.50", page.text)

        listing = self.client.get("/products")
        self.assertIn("渔货价", listing.text)

    def test_20_edit_keeps_fish_price(self):
        with SessionLocal() as db:
            product = product_service.create_product(
                db, {"name": "渔货价测试商品", "cost_amount": "10", "fish_price": "12.5"}
            )
            product_id = product.id
        self.client.post(
            f"/products/{product_id}/edit", data={"fish_price": "13.8"}, follow_redirects=True
        )
        with SessionLocal() as db:
            self.assertEqual(round(float(db.get(Product, product_id).fish_price), 2), 13.8)

    def test_30_package_carries_fish_price(self):
        """闲鱼预填包里也带上渔货价，交给下游时口径一致。"""
        with SessionLocal() as db:
            product = product_service.create_product(
                db, {"name": "渔货价测试商品", "cost_amount": "20", "fish_price": "25"}
            )
            package = product_service.listing_package(product)
        self.assertEqual(package["fish_price"], 25.0)
        self.assertEqual(package["cost"], 20.0)


class FishPriceSelfSideTest(unittest.TestCase):
    """渔货栈：SupplierItem 也有渔货价，创建商品时能带进去。"""

    def test_10_model_and_create(self):
        from src.goods.service import create_item
        from src.models import SupplierItem
        from src.schemas import GoodsItemCreateIn

        # 渔货栈有自己的库 / 自己的会话（src/deps.py），测试里走它那条链路
        from src.deps import _get_engine, session_scope  # noqa: PLC0415
        from src.models import Base as SelfBase

        self.assertIn("fish_price", GoodsItemCreateIn.model_fields)
        SelfBase.metadata.create_all(_get_engine())
        with session_scope() as db:
            payload = GoodsItemCreateIn(
                title="渔货价测试货源",
                cost_price=Decimal("30"),
                sale_price=Decimal("59"),
                fish_price=Decimal("39.9"),
                stock=10,
            )
            item = create_item(db, payload)
            item_id = item.item_id
        with session_scope() as db:
            saved = db.query(SupplierItem).filter(SupplierItem.item_id == item_id).one()
            try:
                self.assertEqual(Decimal(str(saved.fish_price)), Decimal("39.9"))
                self.assertEqual(Decimal(str(saved.cost_price)), Decimal("30"))
                self.assertEqual(Decimal(str(saved.sale_price)), Decimal("59"))
            finally:
                db.delete(saved)
                db.commit()


if __name__ == "__main__":
    unittest.main()
