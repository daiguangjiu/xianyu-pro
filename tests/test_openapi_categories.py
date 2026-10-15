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

import tests  # noqa: F401  让 tests/conftest.py 的 fixtures 生效
from app.models import Base, ProductCategory
from src.deps import get_db
from src.external_identity import categories as categories_module
from src.external_identity.categories import router


class OpenAPICategoriesTests(unittest.TestCase):
    def setUp(self):
        categories_module.invalidate_cache()
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[get_db] = self.get_db
        self.client = TestClient(self.app)
        self.secret = "test-secret"
        with self.Session() as db:
            # 一级
            top = ProductCategory(parent_id=None, level=1, name="女装男装", sort_order=0, source_id="apparel")
            db.add(top)
            db.flush()
            # 二级
            sub = ProductCategory(parent_id=top.id, level=2, name="女装", sort_order=0, source_id="ap_women")
            db.add(sub)
            db.flush()
            # 三级
            db.add(ProductCategory(parent_id=sub.id, level=3, name="连衣裙", sort_order=0, source_id="ap_women_l3_0"))
            db.add(ProductCategory(parent_id=sub.id, level=3, name="针织衫", sort_order=1, source_id="ap_women_l3_1"))
            # 已停用的二级
            db.add(ProductCategory(parent_id=top.id, level=2, name="旧分类", sort_order=1, source_id="ap_legacy", enabled=False))
            db.commit()

    def tearDown(self):
        categories_module.invalidate_cache()
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

    def test_lists_three_levels_with_1688_ids_and_value(self):
        response = self.signed_get("/openapi/v1/xh-fish/categories")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source"], "hub")
        self.assertEqual(body["total"], 4)  # 1 一级 + 1 二级 + 2 三级；已停用被摘掉
        items = {it["id"]: it for it in body["items"]}
        self.assertEqual(items["apparel"]["level"], 1)
        self.assertEqual(items["apparel"]["parentId"], "")
        self.assertEqual(items["apparel"]["path"], "女装男装")
        self.assertEqual(items["apparel"]["value"], "apparel")
        self.assertEqual(items["ap_women"]["level"], 2)
        self.assertEqual(items["ap_women"]["parentId"], "apparel")
        self.assertEqual(items["ap_women"]["path"], "女装男装 / 女装")
        self.assertEqual(items["ap_women"]["value"], "ap_women")
        self.assertEqual(items["ap_women_l3_0"]["level"], 3)
        self.assertEqual(items["ap_women_l3_0"]["parentId"], "ap_women")
        self.assertEqual(items["ap_women_l3_0"]["path"], "女装男装 / 女装 / 连衣裙")
        # 三级没有 1688 id，过滤值要退回到 name 上才能命中 SupplierItem.category_l3
        self.assertEqual(items["ap_women_l3_0"]["value"], "连衣裙")
        # 停用节点不出现在结果里
        self.assertNotIn("ap_legacy", items)

    def test_filter_by_level_and_parent(self):
        response = self.signed_get("/openapi/v1/xh-fish/categories?level=3&parentId=ap_women")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 2)
        for item in body["items"]:
            self.assertEqual(item["level"], 3)
            self.assertEqual(item["parentId"], "ap_women")

    def test_filter_by_name_query(self):
        from urllib.parse import quote
        encoded = quote("针织")
        response = self.signed_get(f"/openapi/v1/xh-fish/categories?q={encoded}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["name"], "针织衫")

    def test_query_parameters_are_covered_by_signature(self):
        response = self.signed_get(
            "/openapi/v1/xh-fish/categories?level=1",
            signed_path="/openapi/v1/xh-fish/categories?level=2",
        )
        self.assertEqual(response.status_code, 401)

    def test_missing_signature_is_rejected(self):
        with patch("src.external_identity.products.get_settings") as settings:
            settings.return_value.xianmou_identity_sync_secret = self.secret
            response = self.client.get("/openapi/v1/xh-fish/categories")
        self.assertEqual(response.status_code, 401)

    def test_falls_back_to_builtin_when_table_is_empty(self):
        with self.Session() as db:
            db.query(ProductCategory).delete()
            db.commit()
        categories_module.invalidate_cache()
        response = self.signed_get("/openapi/v1/xh-fish/categories?level=1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source"], "builtin")
        self.assertGreaterEqual(body["total"], 1)
        for item in body["items"]:
            self.assertEqual(item["level"], 1)
            self.assertTrue(item["id"])
            self.assertTrue(item["value"])
