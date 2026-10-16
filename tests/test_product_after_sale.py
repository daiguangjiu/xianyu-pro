"""售后政策口径（2026-10-16 用户拍板）：

1. 渔枢系统**新增商品**的售后政策默认「非质量问题不退不换」—— 我们卖的是尾货 /
   二手检测机，只保质量问题，「7 天无理由退换」会把退货率抬上去；
2. **已经上架的商品**用一次性脚本 scripts/update_after_sale_policy.py 统一刷一遍，
   刷完顺手同步渔货栈那条 hub-prd-<商品id> 货源；
3. 渔枢同步货源到渔货栈时，商品上没填售后也用这个默认口径（不再是「7天无理由退换」）。
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
from app.models import Product  # noqa: E402
from app.services import product as product_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from scripts.update_after_sale_policy import apply_rows, plan  # noqa: E402
from src.models import SupplierItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
POLICY = "非质量问题不退不换"
CODES = ("ASP-1001", "ASP-1002", "ASP-1003")


def _reset() -> None:
    with SessionLocal() as s:
        s.query(SupplierItem).filter(SupplierItem.item_id.like("hub-prd-%")).delete(
            synchronize_session=False
        )
        s.query(Product).filter(Product.code.in_(CODES)).delete(synchronize_session=False)
        s.commit()


def _make(code: str, *, status: str, after_sale: str = "") -> int:
    with SessionLocal() as s:
        product = Product(
            name=f"售后政策用例 {code}",
            code=code,
            status=status,
            after_sale=after_sale,
        )
        s.add(product)
        s.commit()
        return int(product.id)


class AfterSalePolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self) -> None:
        _reset()

    def tearDown(self) -> None:
        _reset()

    def test_10_default_is_first_option_and_selected(self):
        """默认口径是候选里的第一项，新建页默认就选中它。"""
        self.assertEqual(product_service.DEFAULT_AFTER_SALE, POLICY)
        self.assertEqual(product_service.AFTER_SALE_OPTIONS[0], POLICY)
        html = self.client.get("/products/new").text
        self.assertIn(f"<option selected>{POLICY}</option>", html)

    def test_20_create_product_without_after_sale_uses_default(self):
        """建档时表单没带 after_sale（接口建档 / 老表单）就落默认口径；显式选了就尊重它。"""
        with SessionLocal() as s:
            one = product_service.create_product(
                s, {"name": "售后默认口径用例一", "category": "家用电器"}, created_by="test"
            )
            two = product_service.create_product(
                s,
                {"name": "售后默认口径用例二", "category": "家用电器", "after_sale": "质量问题包退换"},
                created_by="test",
            )
            self.assertEqual(one.after_sale, POLICY)
            self.assertEqual(two.after_sale, "质量问题包退换")
            s.query(Product).filter(Product.name.like("售后默认口径用例%")).delete(
                synchronize_session=False
            )
            s.commit()

    def test_30_script_only_touches_on_sale(self):
        """默认只改「已上架」的；--all 才把未上架的一起改。"""
        _make("ASP-1001", status=product_service.PRODUCT_STATUS_ON, after_sale="7 天无理由退换")
        _make("ASP-1002", status=product_service.PRODUCT_STATUS_DRAFT, after_sale="7 天无理由退换")
        with SessionLocal() as s:
            rows = plan(s, only_on_sale=True)
            self.assertEqual(len(rows), 1, "默认只挑已上架的")
            self.assertEqual(rows[0]["code"], "ASP-1001")
            self.assertEqual(rows[0]["new"], POLICY)
            self.assertEqual(len(plan(s, only_on_sale=False)), 2)

    def test_40_script_applies_and_syncs_supplier(self):
        """改完渔枢这一款，渔货栈那条 hub-prd-<商品id> 货源也跟着变；跑第二遍没有要改的。"""
        pid = _make("ASP-1003", status=product_service.PRODUCT_STATUS_ON, after_sale="7天无理由退换")
        with SessionLocal() as s:
            s.add(SupplierItem(item_id=f"hub-prd-{pid}", title="售后政策用例", after_sale="7天无理由退换"))
            s.commit()

        with SessionLocal() as s:
            rows = plan(s, only_on_sale=True)
            self.assertEqual(len(rows), 1)
            stat = apply_rows(s, rows)
            self.assertEqual(stat["products"], 1)
            self.assertEqual(stat["synced"], 1)

        with SessionLocal() as s:
            product = s.query(Product).filter(Product.code == "ASP-1003").one()
            item = s.query(SupplierItem).filter(SupplierItem.item_id == f"hub-prd-{pid}").one()
            self.assertEqual(product.after_sale, POLICY)
            self.assertEqual(item.after_sale, POLICY)
            self.assertEqual(len(plan(s, only_on_sale=True)), 0, "跑第二遍应当没有要改的")


if __name__ == "__main__":
    unittest.main()
