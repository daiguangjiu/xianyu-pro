"""供应商档案：订单页只能从档案里选；识别 / 导入时遇到没建档的供应商自动补齐。

业务口径（2026-09）：供应商档案在「供应商管理」维护，订单页只提供选择；
群里识别出来的、登记表导入进来的供应商常常还没建档 —— 这时按订单里的写法
自动建一条档案（名称必填、编码留空），订单写法统一成「名称（编码）」；
名字互相包含（「梦想电商」与「安徽梦想电商」）算同一家，不重复建档。
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

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, Supplier  # noqa: E402
from app.services import importer  # noqa: E402
from app.services import supplier as supplier_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class SupplierAutoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self):
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(Supplier).delete()
            s.commit()
            s.add(Supplier(name="安徽梦想电商", code="BH001"))
            s.commit()

    def tearDown(self):
        """清掉本用例建的供应商 / 订单，避免影响其它用例（尤其是同名档案）。"""
        with SessionLocal() as s:
            s.query(Order).delete()
            s.query(Supplier).delete()
            s.commit()

    def _suppliers(self) -> dict[str, str]:
        with SessionLocal() as s:
            return {row.name: row.code for row in s.scalars(select(Supplier)).all()}

    def test_10_known_supplier_is_reused(self):
        """档案里有的供应商（名称 / 编码 / 名称（编码））直接复用，不新建。"""
        with SessionLocal() as s:
            for text in ("安徽梦想电商", "BH001", "安徽梦想电商（BH001）"):
                row = supplier_service.ensure_supplier(s, text)
                self.assertIsNotNone(row)
                self.assertEqual(supplier_service.supplier_label(row), "安徽梦想电商（BH001）")
        self.assertEqual(self._suppliers(), {"安徽梦想电商": "BH001"})

    def test_20_similar_name_is_not_duplicated(self):
        """「梦想电商」与档案里的「安徽梦想电商」是同一家：归一到档案，不重复建档。"""
        with SessionLocal() as s:
            label = supplier_service.ensure_order_supplier(s, "梦想电商")
        self.assertEqual(label, "安徽梦想电商（BH001）")
        self.assertEqual(self._suppliers(), {"安徽梦想电商": "BH001"})

    def test_30_unknown_supplier_is_created(self):
        """档案里确实没有的：自动建档（名称必填、编码留空），并记下来源。"""
        with SessionLocal() as s:
            label = supplier_service.ensure_order_supplier(s, "华强北数码", source="群消息订单识别")
            s.commit()
        self.assertEqual(label, "华强北数码")
        with SessionLocal() as s:
            row = s.scalar(select(Supplier).where(Supplier.name == "华强北数码"))
            self.assertIsNotNone(row)
            self.assertEqual(row.code, "", "编码留空，等运营补")
            self.assertIn("自动建档", row.note)

    def test_40_import_creates_and_normalizes(self):
        """登记表导入：供应商写法统一，没建档的顺带建档。"""
        rows = [
            {
                "day": datetime(2026, 9, 24).date(),
                "supplier": "梦想电商",
                "product": "亚朵深睡枕3.0",
                "spec": "蓝色",
                "cost": 110.0,
                "sale": 140.0,
                "account": "太阳味汽水",
                "owner": "周玉萍",
                "paid": 110.0,
                "note": "",
            },
            {
                "day": datetime(2026, 9, 24).date(),
                "supplier": "京东二手",
                "product": "JBL耳机",
                "spec": "粉色",
                "cost": 90.0,
                "sale": 99.0,
                "account": "珊瑚家的令狐冲",
                "owner": "令狐冲",
                "paid": 90.0,
                "note": "",
            },
        ]
        with SessionLocal() as s:
            importer.import_rows(s, rows, actor="admin", apply=True)
        names = self._suppliers()
        self.assertEqual(names.get("安徽梦想电商"), "BH001", "相似名归档到已有档案")
        self.assertIn("京东二手", names, "档案里没有的新供应商自动建档")
        with SessionLocal() as s:
            orders = {o.product_name: o.supplier_name for o in s.scalars(select(Order)).all()}
        self.assertEqual(orders["亚朵深睡枕3.0"], "安徽梦想电商（BH001）")
        self.assertEqual(orders["JBL耳机"], "京东二手")

    def test_50_detail_page_uses_select(self):
        """订单详情页的供应商是下拉选择（选项来自供应商档案）。"""
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no="HIS-20260924-01",
                product_name="亚朵深睡枕3.0",
                supplier_name="安徽梦想电商（BH001）",
                ordered_at=datetime(2026, 9, 24),
            )
            s.add(order)
            s.commit()
            order_id = order.id
        html = self.client.get(f"/orders/{order_id}").text
        self.assertIn('name="supplier_name"', html)
        self.assertIn("<select", html)
        self.assertIn("安徽梦想电商（BH001）", html)

    def test_60_backfill_relabels_orders(self):
        """历史订单里的旧写法（编码 / 简称）按档案统一，缺失的补档案。"""
        with SessionLocal() as s:
            s.add_all(
                [
                    Order(
                        platform="xianyu",
                        order_no="A-1",
                        product_name="枕头",
                        supplier_name="BH001",
                    ),
                    Order(
                        platform="xianyu",
                        order_no="A-2",
                        product_name="耳机",
                        supplier_name="京东二手",
                    ),
                ]
            )
            s.commit()
        with SessionLocal() as s:
            out = supplier_service.backfill_from_orders(s)
        # 「BH001」归一成展示名算一次改动；「京东二手」写法本来就是展示名，只补档案不改订单
        self.assertEqual(out["changed"], 1)
        self.assertEqual(out["created"], 1)
        with SessionLocal() as s:
            labels = {
                o.order_no: o.supplier_name for o in s.scalars(select(Order)).all()
            }
        self.assertEqual(labels["A-1"], "安徽梦想电商（BH001）")
        self.assertEqual(labels["A-2"], "京东二手")
        self.assertIn("京东二手", self._suppliers())


if __name__ == "__main__":
    unittest.main()
