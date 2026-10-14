"""渔枢 · 发布商品「规格与 SKU」（2026-10-12 用户要求）：

1. 「添加规格」后面多一个「按照规则生成 SKU」—— 基于上面手动构建的规格维度做组合出矩阵；
2. 「应用规格模板」改名「应用类目规格模板」，没选类目时置灰，点了提醒「请先选择商品所属类目！」，
   选了类目就按该类目的既定模板铺规格维度。

页面是 JS 交互（组合、置灰都发生在前端），这里断言页面里下发的那套桩：
按钮文案 / 函数名 / 初始置灰 / 那句提醒 / 类目变化时的刷新钩子。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Product, ProductSku  # noqa: E402
from app.services.product import spec_template_for  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class ProductSpecMatrixTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(ProductSku).delete()
            s.query(Product).delete()
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def test_10_publish_page_spec_buttons(self):
        """发布页：「添加规格」旁边是「按照规则生成 SKU」；模板按钮改名并置灰。"""
        html = self.client.get("/products/new").text
        self.assertIn("添加规格", html)
        self.assertIn("按照规则生成 SKU", html)
        self.assertIn("genSkuMatrix()", html)
        # 改名：老文案一处不留
        self.assertIn("应用类目规格模板", html)
        self.assertNotIn("应用规格模板", html)
        # 未选类目 → 置灰（aria-disabled + 灰样式），点了给人话
        self.assertIn('id="tplBtn"', html)
        self.assertIn('aria-disabled="true"', html)
        self.assertIn("cursor:not-allowed", html)
        self.assertIn("请先选择商品所属类目！", html)
        # 类目一变就刷新按钮状态（onCat1Change → updateCatHint → syncTplBtn）
        self.assertIn("syncTplBtn", html)
        self.assertIn("window.addEventListener('load', syncTplBtn)", html)

    def test_20_edit_page_also_has_buttons(self):
        """编辑页与发布页同一套表单：按钮同样在（回填过类目，按钮不灰）。"""
        with SessionLocal() as s:
            s.add(Product(name="规格页测试", code="SPEC0001", category="家用电器"))
            s.commit()
        with SessionLocal() as s:
            pid = s.query(Product).one().id
        html = self.client.get(f"/products/{pid}/edit").text
        self.assertIn("按照规则生成 SKU", html)
        self.assertIn("应用类目规格模板", html)
        self.assertIn("syncTplBtn", html)

    def test_30_category_template_source(self):
        """「应用类目规格模板」铺的是类目模板：类目命中就给该套，认不出给兜底。"""
        self.assertEqual(
            [d["name"] for d in spec_template_for("家用电器")][:1], ["型号"], "一级命中")
        hit = spec_template_for("数码、电脑 / 手机配件")
        self.assertEqual(hit[0]["name"], "适配机型", "二级模板优先于一级")
        fallback = spec_template_for("")
        self.assertEqual([d["name"] for d in fallback], ["规格"], "认不出走兜底")


if __name__ == "__main__":
    unittest.main()
