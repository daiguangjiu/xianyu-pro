"""渔集价格更新 → 渔枢商品档案 + 渔货栈货源 + 「商品价格变动」通知（2026-10-15 用户拍板）。

三条口径：
· 渔集那边改价（检测定价 / 编辑 / 改价页）提交成功后**一定要同步**给渔枢与渔货栈；
· 同步口径与 import_from_yushangzai 一致：渔货价 = selling_price（也是 cost_amount），
  销售价 = 建议售价，没设按市场价 × 0.5 兜底，市场价 = base_price；
· 真变了时在渔货栈「异常提醒」（/self/logs）顶部落一条「商品价格变动」通知，
  写清楚涨价了还是降价了。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  测试库配置要在任何 app 模块之前生效

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Product  # noqa: E402
from app.services.wms_bridge import sync_price_from_wms  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from src.models import PriceChangeNotice, SupplierItem  # noqa: E402
from wms import bridge as wms_bridge  # noqa: E402
from wms import intake  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import WmsBatch, WmsItem  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
FISH = "YS202610159001"


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(PriceChangeNotice).filter(
            PriceChangeNotice.fish_code.like("YS202610159%")).delete(synchronize_session=False)
        s.query(SupplierItem).filter(
            SupplierItem.item_id.like("hub-prd-%")).filter(
            SupplierItem.title.like("价格同步测试%")).delete(synchronize_session=False)
        s.query(Product).filter(Product.code.like("YS202610159%")).delete(
            synchronize_session=False)
        s.query(WmsItem).filter(WmsItem.fish_code.like("YS202610159%")).delete(
            synchronize_session=False)
        s.query(WmsBatch).filter(WmsBatch.supplier_name == "价格同步测试").delete(
            synchronize_session=False)
        s.commit()


class PriceSyncTest(unittest.TestCase):
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

    # ------------------------------------------------------------ 造数
    def _mk_product(self, fish: str = FISH) -> Product:
        with SessionLocal() as s:
            p = Product(name="价格同步测试款", code=fish, stock=2,
                        source_type="self_operated")
            s.add(p)
            s.commit()
            return s.get(Product, p.id)

    def _mk_supplier_item(self, product: Product, fish: str = FISH) -> SupplierItem:
        with SessionLocal() as s:
            row = SupplierItem(
                item_id=f"hub-prd-{product.id}", title="价格同步测试款",
                cost_price=0, stock=2,
            )
            s.add(row)
            s.commit()
            return s.get(SupplierItem, row.id)

    def _mk_item(self, fish: str = FISH, **kw) -> WmsItem:
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "purchase", "qty_expected": 2,
                    "supplier_name": "价格同步测试"}, operator="tester")
            batch = s.get(WmsBatch, batch.id)
            rows = intake.register(s, batch.id, [{
                "sku_code": f"PS-{fish[-4:]}", "product_name": "价格同步测试款",
                "fish_code": fish, "base_price": 200.0, "qty": 1, **kw,
            }], operator="tester")
            return s.get(WmsItem, rows[0].id)

    # ------------------------------------------------------------ 同步服务本体
    def test_10_sync_updates_product_and_supplier_and_notice(self):
        p = self._mk_product()
        self._mk_supplier_item(p)
        report = sync_price_from_wms(
            FISH, name="价格同步测试款", selling_price=100.0,
            suggest_price=150.0, base_price=300.0)
        self.assertTrue(report["updated"] and report["notice"], report)
        with SessionLocal() as s:
            p = s.query(Product).filter(Product.code == FISH).one()
            self.assertEqual(float(p.fish_price), 100.0)
            self.assertEqual(float(p.cost_amount), 100.0, "成本 = 渔货价")
            self.assertEqual(float(p.price), 150.0)
            self.assertEqual(float(p.original_price), 300.0)
            src = s.scalar(select(SupplierItem).where(
                SupplierItem.item_id == f"hub-prd-{p.id}"))
            self.assertIsNotNone(src)
            self.assertEqual(float(src.cost_price), 100.0, "渔货栈货源渔货价跟着改")
            self.assertEqual(float(src.sale_price), 150.0)
            self.assertEqual(float(src.market_price), 300.0)
            notice = s.scalar(select(PriceChangeNotice).where(
                PriceChangeNotice.fish_code == FISH))
            self.assertIsNotNone(notice, "价格变了要落一条通知")
            self.assertEqual(notice.direction, "up", "0 → 100 是涨价")
            self.assertIn("价格同步测试款", notice.product_name)

    def test_15_no_change_no_new_notice(self):
        p = self._mk_product()
        self._mk_supplier_item(p)
        sync_price_from_wms(FISH, selling_price=100.0, suggest_price=150.0,
                            base_price=300.0)
        with SessionLocal() as s:
            first = s.query(PriceChangeNotice).filter(
                PriceChangeNotice.fish_code == FISH).count()
        self.assertEqual(first, 1)
        report = sync_price_from_wms(FISH, selling_price=100.0, suggest_price=150.0,
                                     base_price=300.0)
        self.assertTrue(report["updated"])
        self.assertFalse(report["notice"], "价没变不再发通知")
        with SessionLocal() as s:
            self.assertEqual(s.query(PriceChangeNotice).filter(
                PriceChangeNotice.fish_code == FISH).count(), 1)

    def test_20_suggest_fallback_market_half(self):
        """建议售价没设 → 销售价 = 市场价 × 0.5（与导入方案 B 同一条兜底链）。"""
        self._mk_product()
        sync_price_from_wms(FISH, selling_price=100.0, suggest_price=0.0,
                            base_price=300.0)
        with SessionLocal() as s:
            p = s.query(Product).filter(Product.code == FISH).one()
            self.assertEqual(float(p.price), 150.0, "300 × 0.5")

    def test_30_missing_product_returns_notes(self):
        report = sync_price_from_wms("YS202610159999", selling_price=10.0)
        self.assertFalse(report["updated"])
        self.assertTrue(report["notes"], "认不到商品要说明，别悄悄丢")

    # ------------------------------------------------------------ 渔集侧桥
    def test_40_bridge_push_price_change(self):
        p = self._mk_product()
        self._mk_supplier_item(p)
        report = wms_bridge.push_price_change(
            FISH.lower(), name="价格同步测试款", selling_price=66.0,
            suggest_price=88.0, base_price=160.0)
        self.assertTrue(report["updated"], report)
        with SessionLocal() as s:
            p = s.query(Product).filter(Product.code == FISH).one()
            self.assertEqual(float(p.fish_price), 66.0)
        note = wms_bridge.price_sync_note(report)
        self.assertIn("已同步", note)

    def test_50_update_item_triggers_sync(self):
        """编辑改价（update_item 传价格字段）→ 同步链自己走完。"""
        p = self._mk_product()
        self._mk_supplier_item(p)
        item = self._mk_item()
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            intake.update_item(s, row, fields={"selling_price": 77.0},
                               operator="tester")
        with SessionLocal() as s:
            p = s.query(Product).filter(Product.code == FISH).one()
            self.assertEqual(float(p.fish_price), 77.0)
            notice = s.scalar(select(PriceChangeNotice).where(
                PriceChangeNotice.fish_code == FISH))
            self.assertIsNotNone(notice)
            self.assertEqual(notice.direction, "up")

    def test_60_price_page_triggers_sync(self):
        """改价页 POST → 渔枢 / 渔货栈跟着改 + 渔货栈「通知」页看得见。"""
        p = self._mk_product()
        self._mk_supplier_item(p)
        item = self._mk_item()
        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            s.execute(
                __import__("sqlalchemy").text(
                    "UPDATE wms_items SET grade='S', status='SELLABLE' "
                    f"WHERE id={int(row.id)}"))
            s.commit()
        resp = self.client.post(
            f"/wms/items/{item.id}/price",
            data={"selling_price": "180", "base_price": "200", "purchase_cost": "50"},
            follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:300])
        with SessionLocal() as s:
            p = s.query(Product).filter(Product.code == FISH).one()
            self.assertEqual(float(p.fish_price), 180.0)
            notice = s.scalar(select(PriceChangeNotice).where(
                PriceChangeNotice.fish_code == FISH))
            self.assertIsNotNone(notice)
        page = self.client.get("/self/logs")
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn("商品价格变动", page.text)
        self.assertIn(FISH, page.text)
        self.assertIn("涨价", page.text)

    def test_70_inspection_submit_triggers_sync(self):
        """检测定价（submit 落渔货价）→ 同步链自己走完。

        用 C 档 + 人工定价：C 档审批线为 0、人工定价必定落库，不依赖价格规则表
        的当前状态（其它用例会改规则，共享测试库不能赌系数）。
        """
        p = self._mk_product()
        self._mk_supplier_item(p)
        item = self._mk_item()
        from wms import inspection
        from wms.models import WmsTestRecord

        with SessionLocal() as s:
            row = s.get(WmsItem, item.id)
            inspection.submit(s, row, tester="tester", results={},
                              final_grade="C", override_reason="测试定档",
                              manual_price=150.0,
                              # 共享测试库里 WmsItem id 会复用，旧检测记录可能把
                              # 这台顶成「第 N 轮复检」—— 统一带上覆盖理由
                              recheck_reason="测试：复检覆盖")
        with SessionLocal() as s:
            p = s.query(Product).filter(Product.code == FISH).one()
            self.assertEqual(float(p.fish_price), 150.0, "人工定价要同步到渔枢")
            notice = s.scalar(select(PriceChangeNotice).where(
                PriceChangeNotice.fish_code == FISH))
            self.assertIsNotNone(notice)
            self.assertEqual(notice.direction, "up")


if __name__ == "__main__":
    unittest.main()
