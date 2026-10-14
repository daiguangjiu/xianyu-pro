"""退货地址管理（2026-09-29 起）：与运费模板同一套管理，多一层「关联指定商品」。

命中优先级：指定商品 > 供应商 + 一级类目 > 一级类目 > 供应商 > 全局默认。
类目口径与运费模板一致：**只关联到一级类目**。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Product, ProductCategory, ReturnAddress, ReturnAddressProduct, Supplier  # noqa: E402
from app.services import category as category_service  # noqa: E402
from app.services import return_addr as return_service  # noqa: E402
from app.services import shipping as shipping_service  # noqa: E402


def _top_id(session, name: str = "家用电器") -> int:
    row = session.scalar(
        select(ProductCategory).where(ProductCategory.name == name, ProductCategory.level == 1)
    )
    return int(row.id)


def _sub_id(session, top_id: int) -> int:
    row = session.scalars(
        select(ProductCategory).where(ProductCategory.parent_id == top_id)
    ).first()
    return int(row.id)


class ReturnAddressTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        with SessionLocal() as s:
            s.query(ReturnAddressProduct).delete()
            s.query(ReturnAddress).delete()
            s.query(shipping_service.ShippingRule).delete()
            s.query(Product).delete()
            s.query(Supplier).delete()
            s.query(ProductCategory).delete()
            s.commit()
            category_service.ensure_seeded(s)
            self.top_id = _top_id(s)
            self.sub_id = _sub_id(s, self.top_id)
            self.supplier = Supplier(name="测试供应链", code="T01")
            s.add(self.supplier)
            s.commit()
            self.supplier_id = int(self.supplier.id)
            for name in ("亚朵枕头", "测试水杯"):
                s.add(Product(name=name, code=name, supplier_id=self.supplier_id))
            s.commit()
            self.p1, self.p2 = [int(p.id) for p in s.scalars(select(Product).order_by(Product.name))]

    def test_10_default_and_priority(self) -> None:
        """全局默认兜底；一级类目 > 供应商；供应商 + 类目最优先（商品之外）。"""
        with SessionLocal() as s:
            default = return_service.create_address(
                s, name="默认仓", contact="张三", phone="13800000000",
                region="广东省 广州市", address="A 仓", is_default=True,
            )
            cat_addr = return_service.create_address(
                s, name="家电退货仓", category_id=self.top_id, supplier_id=self.supplier_id,
            )
            sup_addr = return_service.create_address(s, name="供应链仓", supplier_id=self.supplier_id)
            self.assertEqual(
                return_service.pick(s, category_text="女装男装 / 外套").name, "默认仓",
                "别的类目走全局默认",
            )
            self.assertEqual(
                return_service.pick(s, category_text="家用电器 / 大家电 / 空调", supplier_id=self.supplier_id).name,
                "家电退货仓", "供应商 + 一级类目 比 供应商 更具体",
            )
            self.assertEqual(
                return_service.pick(s, category_text="家用电器 / 大家电", supplier_id=None).name,
                "默认仓", "类目规则还关联了供应商，不属于这家供应商时不生效",
            )
            self.assertEqual(return_service.default_address(s).id, default.id)
            self.assertEqual(sup_addr.name, "供应链仓")

    def test_20_product_link_beats_everything(self) -> None:
        """关联了指定商品：只对那些商品生效，且压过供应商 / 类目规则。"""
        with SessionLocal() as s:
            return_service.create_address(s, name="默认仓", is_default=True)
            cat_addr = return_service.create_address(
                s, name="家电退货仓", category_id=self.top_id, supplier_id=self.supplier_id
            )
            return_service.set_products(s, cat_addr, [self.p1])
            self.assertEqual(
                return_service.pick(s, product_id=self.p1, category_text="家用电器 / 大家电",
                                    supplier_id=self.supplier_id).name,
                "家电退货仓", "指定商品命中",
            )
            # 另一款商品没被指定 → 类目规则因为还绑了供应商，这里落到全局默认
            self.assertEqual(
                return_service.pick(s, product_id=self.p2, category_text="女装男装 / T恤").name,
                "默认仓", "没指定的商品不吃这条规则",
            )
            # 商品被指定后，即便类目完全不相关也生效（商品维度最具体）
            self.assertEqual(
                return_service.pick(s, product_id=self.p1, category_text="女装男装 / T恤").name,
                "家电退货仓",
            )

    def test_30_set_products_replace_and_clear(self) -> None:
        with SessionLocal() as s:
            addr = return_service.create_address(s, name="专用仓", product_ids=[self.p1])
            self.assertEqual(return_service.product_ids_of(s, int(addr.id)), {self.p1})
            return_service.set_products(s, addr, [self.p1, self.p2])
            self.assertEqual(return_service.product_ids_of(s, int(addr.id)), {self.p1, self.p2})
            return_service.set_products(s, addr, [])
            self.assertEqual(return_service.product_ids_of(s, int(addr.id)), set())
            self.assertIn("全局", return_service.scope_text(addr))

    def test_40_category_scope_is_top_level_only(self) -> None:
        """类目口径：传二级类目会自动收敛到它的一级（运费模板同口径）。"""
        with SessionLocal() as s:
            addr = return_service.create_address(s, name="二级类目传进来", category_id=self.sub_id)
            self.assertEqual(addr.category_id, self.top_id, "二 / 三级类目收敛到一级")
            rule = shipping_service.create_rule(
                s, kind=shipping_service.KIND_FREIGHT, name="运费规则", fee=6, category_id=self.sub_id
            )
            self.assertEqual(rule.category_id, self.top_id)
            # 下拉只给一级
            options = shipping_service.top_category_options(s)
            self.assertTrue(all(o["level"] == 1 for o in options))
            self.assertIn(self.top_id, [o["id"] for o in options])
            self.assertNotIn(self.sub_id, [o["id"] for o in options])

    def test_50_migrate_legacy_sub_level_rules(self) -> None:
        """历史规则关联的二 / 三级类目：迁移到一级后重新生效。"""
        with SessionLocal() as s:
            rule = shipping_service.create_rule(
                s, kind=shipping_service.KIND_FREIGHT, name="老规则", fee=8, category_id=self.top_id
            )
            rule.category_id = self.sub_id   # 假装是老数据：直接改库绕开收敛
            s.commit()
            self.assertIsNone(
                shipping_service.pick_rule(s, shipping_service.KIND_FREIGHT, category_text="家用电器 / 大家电"),
                "关联二级类目时命中不上",
            )
            self.assertEqual(shipping_service.ensure_top_level_scope(s), 1)
            picked = shipping_service.pick_rule(s, shipping_service.KIND_FREIGHT, category_text="家用电器 / 大家电")
            self.assertIsNotNone(picked)
            self.assertEqual(picked.name, "老规则")

    def test_60_only_one_global_default(self) -> None:
        with SessionLocal() as s:
            first = return_service.create_address(s, name="仓 A", is_default=True)
            second = return_service.create_address(s, name="仓 B", is_default=True)
            self.assertFalse(first.is_default)
            self.assertTrue(second.is_default)
            self.assertEqual(return_service.default_address(s).id, second.id)
            self.assertEqual(return_service.ensure_default(s), 0, "已有全局默认就不再播种")


class ReturnAddressPageTest(unittest.TestCase):
    """退货地址管理页：菜单在「货源」下，页面能开、能建、能指定商品。"""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        from fastapi.testclient import TestClient  # noqa: PLC0415

        from app.main import app  # noqa: PLC0415
        from app.web.auth import ensure_default_admin  # noqa: PLC0415

        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data={"username": "admin", "password": "xianyu2026"},
                               follow_redirects=False)
        assert resp.status_code == 303

    def test_70_page_and_create_flow(self) -> None:
        with SessionLocal() as s:
            s.query(ReturnAddressProduct).delete()
            s.query(ReturnAddress).delete()
            s.commit()
            category_service.ensure_seeded(s)
            product = s.scalar(select(Product).order_by(Product.name))
            product_id = int(product.id) if product else 0
        page = self.client.get("/return-addresses")
        self.assertEqual(page.status_code, 200)
        self.assertIn("命中顺序", page.text)
        if product_id:
            created = self.client.post(
                "/return-addresses/create",
                data={"name": "页面新建仓", "contact": "李四", "phone": "13900000000",
                      "region": "浙江省 杭州市", "address": "B 仓", "is_default": "1",
                      "product_ids": [str(product_id)]},
                follow_redirects=False,
            )
            self.assertEqual(created.status_code, 303)
            with SessionLocal() as s:
                addr = s.scalar(select(ReturnAddress).where(ReturnAddress.name == "页面新建仓"))
                self.assertIsNotNone(addr)
                self.assertEqual(return_service.product_ids_of(s, int(addr.id)), {product_id})

    def test_80_page_supports_both_kinds(self) -> None:
        """页面改名「地址管理」并支持两种用途，老链接 /return-addresses 仍能打开。"""
        for url in ("/addresses", "/addresses?kind=ship", "/return-addresses", "/return-addresses?kind=ship"):
            page = self.client.get(url)
            self.assertEqual(page.status_code, 200, url)
        self.assertIn("发货地址", self.client.get("/addresses?kind=ship").text)
        self.assertIn("退货地址", self.client.get("/addresses").text)
        # 两种用途的全局默认各自独立
        self.client.post(
            "/return-addresses/create",
            data={"kind": "ship", "name": "页面发货仓", "is_default": "1"},
            follow_redirects=False,
        )
        with SessionLocal() as s:
            self.assertEqual(return_service.default_address(s, kind="ship").name, "页面发货仓")


class ReturnAddressKindTest(unittest.TestCase):
    """退货地址 / 发货地址共用一张表（kind 区分），但「全局默认」必须各自独立。

    这是共用一张表最容易踩的坑：设了默认发货地址，把默认退货地址顶掉了。
    """

    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        with SessionLocal() as s:
            s.query(ReturnAddressProduct).delete()
            s.query(ReturnAddress).delete()
            s.commit()

    def test_10_default_per_kind(self) -> None:
        with SessionLocal() as s:
            ret = return_service.create_address(s, kind="return", name="退货默认仓", is_default=True)
            ship = return_service.create_address(s, kind="ship", name="发货默认仓", is_default=True)
            self.assertTrue(ret.is_default, "设了默认发货地址，不该把默认退货地址顶掉")
            self.assertTrue(ship.is_default)
            self.assertEqual(return_service.default_address(s, kind="return").id, ret.id)
            self.assertEqual(return_service.default_address(s, kind="ship").id, ship.id)

    def test_20_default_is_unique_within_kind(self) -> None:
        with SessionLocal() as s:
            first = return_service.create_address(s, kind="ship", name="发货 A", is_default=True)
            second = return_service.create_address(s, kind="ship", name="发货 B", is_default=True)
            self.assertFalse(first.is_default, "同一种用途只能有一条全局默认")
            self.assertTrue(second.is_default)

    def test_30_pick_is_isolated_by_kind(self) -> None:
        with SessionLocal() as s:
            return_service.create_address(s, kind="return", name="退货默认仓", is_default=True)
            return_service.create_address(s, kind="ship", name="发货默认仓", is_default=True)
            self.assertEqual(return_service.pick(s, kind="return").name, "退货默认仓")
            self.assertEqual(return_service.pick(s, kind="ship").name, "发货默认仓")
            self.assertEqual(
                [a.name for a in return_service.list_addresses(s, kind="ship")], ["发货默认仓"]
            )

    def test_40_ensure_default_seeds_both_kinds(self) -> None:
        with SessionLocal() as s:
            self.assertEqual(return_service.ensure_default(s, kind="return"), 1)
            self.assertEqual(return_service.ensure_default(s, kind="ship"), 1)
            self.assertEqual(return_service.ensure_default(s, kind="ship"), 0, "已有就不再播种")
            self.assertNotEqual(
                return_service.default_address(s, kind="return").name,
                return_service.default_address(s, kind="ship").name,
            )

    def test_50_unknown_kind_falls_back_to_return(self) -> None:
        """老链接 / 老表单不带 kind 时按退货地址处理，行为与升级前一致。"""
        self.assertEqual(return_service.normalize_kind(None), "return")
        self.assertEqual(return_service.normalize_kind(""), "return")
        self.assertEqual(return_service.normalize_kind("ship"), "ship")
        self.assertEqual(return_service.kind_label("ship"), "发货地址")


if __name__ == "__main__":
    unittest.main()
