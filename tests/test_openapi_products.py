from __future__ import annotations

import hashlib
import hmac
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tests
from src.deps import get_db
from src.external_identity.products import router
from src.models import Base, SkuMapping, SupplierItem


class OpenAPIProductsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[get_db] = self.get_db
        self.client = TestClient(self.app)
        self.secret = "test-secret"
        with self.Session() as db:
            db.add_all([
                SupplierItem(item_id="visible-1", title="可见商品", category_id="cat-1", cost_price=10,
                             stock=4, audit_status="approved", is_listed=True, fish_price=12,
                             sale_price=20, images=["https://example.com/a.jpg"],
                             sku_list={"skus": [{"sku_id": "sku-1", "spec_text": "红色", "price": 20, "stock": 4}]}),
                SupplierItem(item_id="hidden-1", title="待审商品", cost_price=10, stock=4,
                             audit_status="pending", is_listed=False),
            ])
            db.flush()
            db.add(SkuMapping(item_id="visible-1", ali_sku_id="sku-1", reserved_stock=1))
            db.commit()

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def get_db(self):
        db = self.Session()
        try:
            yield db
        finally:
            db.close()

    def signed_get(self, path, signed_path=None):
        timestamp = str(int(time.time()))
        nonce = f"test-{time.time_ns()}"
        digest = hashlib.sha256(b"").hexdigest()
        message = "\n".join(("GET", signed_path or path, timestamp, nonce, digest))
        signature = hmac.new(self.secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        with patch("src.external_identity.products.get_settings") as settings:
            settings.return_value.xianmou_identity_sync_secret = self.secret
            return self.client.get(path, headers={
                "X-Client-Id": "xh-fish", "X-Timestamp": timestamp,
                "X-Nonce": nonce, "X-Signature": signature,
            })

    def test_list_returns_all_listed_products_with_paging_and_legacy_shape(self):
        response = self.signed_get("/openapi/v1/xh-fish/products?page=1&pageSize=10",
                                   signed_path="/openapi/v1/xh-fish/products?page=1&pageSize=10")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["items"]), 1)
        product = body["items"][0]
        self.assertEqual(product["title"], "可见商品")
        self.assertEqual(product["sourceItemId"], "visible-1")
        self.assertEqual(product["skus"][0]["id"], 1)
        self.assertEqual(product["skus"][0]["sourceSkuId"], "sku-1")
        self.assertEqual(product["skus"][0]["available"], 3)

    def test_detail_returns_not_found_for_unlisted_product(self):
        response = self.signed_get("/openapi/v1/xh-fish/products/2")
        self.assertEqual(response.status_code, 404)

    def test_query_parameters_are_covered_by_signature(self):
        response = self.signed_get("/openapi/v1/xh-fish/products?page=1&pageSize=10&q=altered",
                                   signed_path="/openapi/v1/xh-fish/products?page=1&pageSize=10&q=original")
        self.assertEqual(response.status_code, 401)

    def test_invalid_signature_is_rejected(self):
        with patch("src.external_identity.products.get_settings") as settings:
            settings.return_value.xianmou_identity_sync_secret = self.secret
            response = self.client.get("/openapi/v1/xh-fish/products")
        self.assertEqual(response.status_code, 401)
