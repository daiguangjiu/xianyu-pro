"""渔集出库 / 召回 → 渔枢商品库存 + 渔货栈平台库存跟着变（2026-10-10）。

用户口径：**数量就是库存数** —— 渔集那边一台货卖出去（销售出库核销），渔枢商品档案的库存
与渔货栈（货还在池子里）的库存都要跟着减；买家退货召回（货回来了）要加回去。

**认货靠渔货号**（2026-10-14 起）：以前渔枢建档时「商家编码」直接复用渔集那台货的条码，
同款多台就分散在好几个编码上；现在统一认渔集内部发的渔货号（``YS + 日期 + 流水``），
``Product.code == 渔货号``，库存 = 这个渔货号下所有条码商品的合计台数。
入口仍是条码（现场扫的就是它），``wms.bridge.push_stock`` 负责换算 + 同款合并。
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
from app.models import Product, ProductSku  # noqa: E402
from app.services import wms_stock  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402
from src.models import SupplierItem  # noqa: E402
from wms import bridge as wms_bridge  # noqa: E402
from wms import intake, outbound, seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import (  # noqa: E402
    WmsBatch,
    WmsItem,
    WmsLabelPrint,
    WmsOutboundItem,
    WmsOutboundOrder,
    WmsStockBalance,
    WmsStockLedger,
)

BARCODE = "QCCA26100999"


def _wipe() -> None:
    with SessionLocal() as s:
        s.query(WmsLabelPrint).delete(synchronize_session=False)
        s.query(WmsStockLedger).delete(synchronize_session=False)
        s.query(WmsStockBalance).delete(synchronize_session=False)
        s.query(WmsOutboundItem).delete(synchronize_session=False)
        s.query(WmsOutboundOrder).delete(synchronize_session=False)
        s.query(WmsItem).delete(synchronize_session=False)
        s.query(WmsBatch).delete(synchronize_session=False)
        s.query(SupplierItem).delete(synchronize_session=False)
        s.query(ProductSku).delete(synchronize_session=False)
        s.query(Product).delete(synchronize_session=False)
        s.commit()


class WmsStockSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
        ensure_default_admin()
        cls.client = TestClient(app)

    def setUp(self) -> None:
        _wipe()
        self.client.cookies.set(COOKIE_NAME, make_session(1)[0])

    # ------------------------------------------------------------ 造数据
    def _wms_item(self, qty: int = 2) -> tuple[int, str, str]:
        """渔集一条货：可售、S 档、在可售仓（出库要用的前置条件）。

        返回 ``(id, 条码, 渔货号)`` —— 渔货号是建档时自动认亲发出来的。
        """
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "HA", "source_type": "recycle", "qty_expected": qty,
                    "supplier_name": "库存同步测试"},
                operator="tester",
            )
            item = intake.register(
                s, batch.id,
                [{"brand": "美的", "product_name": "测试破壁机", "qty": qty,
                  "purchase_cost": 100, "base_price": 300, "sku_code": "T-SYNC"}],
                operator="tester",
            )[0]
            row = s.get(WmsItem, item.id)
            row.status = "SELLABLE"
            row.grade = "S"
            row.warehouse_code = "SALE"
            row.selling_price = 250.0
            s.commit()
            return int(row.id), str(row.barcode), str(row.fish_code or "")

    def _product(self, code: str, stock: int = 2, skus: tuple[int, ...] = ()) -> int:
        """渔枢商品：**商家编码 = 渔货号**（2026-10-14 起扫渔集货品建档写的就是它）。"""
        with SessionLocal() as s:
            product = Product(
                name="测试破壁机", code=code, brand="美的", stock=stock,
                source_type="self_operated",
                category="家用电器 / 厨房电器 / 家用破壁机",
            )
            s.add(product)
            s.flush()
            for idx, qty in enumerate(skus, start=1):
                s.add(ProductSku(product_id=product.id, spec=f"规格{idx}",
                                 sku_code=f"{code}-{idx:02d}", stock=qty))
            s.commit()
            return int(product.id)

    def _harbor_item(self, product_id: int, code: str, stock: int = 2,
                     skus: tuple[int, ...] = ()) -> int:
        """渔货栈那条货源（渔枢商品上架过才会有的 hub-prd-<商品id>）。"""
        with SessionLocal() as s:
            row = SupplierItem(
                item_id=f"hub-prd-{product_id}", item_code=code,
                title="测试破壁机", stock=stock, prev_stock=stock,
                sku_list={"skus": [{"spec": f"规格{i}", "stock": q}
                                   for i, q in enumerate(skus, start=1)]},
            )
            s.add(row)
            s.commit()
            return int(row.id)

    # ------------------------------------------------------------ 桥 / 服务
    def test_10_push_stock_hits_product_and_harbor(self):
        """直接推一次：**扫条码**进去，桥把它换成渔货号，渔枢与渔货栈库存一起减。"""
        _, code, fish = self._wms_item(qty=1)
        product_id = self._product(fish, stock=2)
        item_id = self._harbor_item(product_id, fish, stock=2)
        report = wms_bridge.push_stock([(code, -1)])
        self.assertTrue(fish.startswith("YS"), "建档要自动发渔货号")
        self.assertEqual(report["updated"], 1, report)
        self.assertEqual(report["missing"], [])
        with SessionLocal() as s:
            self.assertEqual(int(s.get(Product, product_id).stock or 0), 1)
            self.assertEqual(int(s.get(SupplierItem, item_id).stock or 0), 1)

    def test_15_missing_and_unmanaged(self):
        """认不到的渔货号记进 missing；不管理库存（-1）的商品一个数字都不动。"""
        _, code, fish = self._wms_item(qty=1)
        product_id = self._product(fish, stock=-1)
        report = wms_bridge.push_stock([("NO-SUCH-CODE", -1), (code, -1)])
        self.assertEqual(report["missing"], ["NO-SUCH-CODE"])
        self.assertEqual(report["updated"], 0)
        self.assertTrue(report["unchanged"], report)
        with SessionLocal() as s:
            self.assertEqual(int(s.get(Product, product_id).stock or 0), -1)

    def test_17_skus_absorb_the_change(self):
        """有规格的商品：先扣规格、再回算商品级（商品级 = 各规格之和）。

        这里直接调渔枢自己的服务（不经过桥），所以传的就是渔货号本身。
        """
        _, code, fish = self._wms_item(qty=1)
        product_id = self._product(fish, stock=5, skus=(2, 3))
        wms_stock.apply_stock_deltas([(fish, -3)])
        with SessionLocal() as s:
            rows = list(s.scalars(select(ProductSku).where(
                ProductSku.product_id == product_id).order_by(ProductSku.id)))
            self.assertEqual([int(r.stock or 0) for r in rows], [0, 2], "从前往后扣到 0")
            self.assertEqual(int(s.get(Product, product_id).stock or 0), 2)

    # ------------------------------------------------------------ 出库 / 召回
    def test_20_sale_outbound_syncs_stock(self):
        """销售出库核销：货没了 → 渔枢与渔货栈库存跟着减（真的走一遍出库动作）。"""
        item_id, code, fish = self._wms_item(qty=2)
        self.assertTrue(fish)
        product_id = self._product(fish, stock=2)
        harbor_id = self._harbor_item(product_id, fish, stock=2)
        with SessionLocal() as s:
            item = s.get(WmsItem, item_id)
            order = outbound.create(s, {"type": "SALE", "warehouse_code": "SALE",
                                        "target_no": "SO-SYNC"}, operator="t")
            outbound.lock(s, order, item, operator="t")
            outbound.pick(s, order, item.barcode, require_grade="S", operator="t")
            order = outbound.ship(s, order.id, operator="t", waybill_no="SF123456")
            self.assertEqual(s.get(WmsItem, item_id).status, "SOLD")
        self.assertEqual(int(order.hub_sync.get("updated") or 0), 1, order.hub_sync)
        with SessionLocal() as s:
            self.assertEqual(int(s.get(Product, product_id).stock or 0), 0, "2 台全卖出去了")
            self.assertEqual(int(s.get(SupplierItem, harbor_id).stock or 0), 0)

    def test_25_missing_product_does_not_break_outbound(self):
        """渔枢没这款商品（扫了渔集货品但没在渔枢建档）：出库照旧成功，只是没得同步。"""
        item_id, code, fish = self._wms_item(qty=1)
        with SessionLocal() as s:
            item = s.get(WmsItem, item_id)
            order = outbound.create(s, {"type": "SALE", "warehouse_code": "SALE",
                                        "target_no": "SO-SYNC2"}, operator="t")
            outbound.lock(s, order, item, operator="t")
            outbound.pick(s, order, item.barcode, require_grade="S", operator="t")
            order = outbound.ship(s, order.id, operator="t", waybill_no="SF123457")
            self.assertEqual(s.get(WmsItem, item_id).status, "SOLD")
        self.assertEqual(int(order.hub_sync.get("updated") or 0), 0)
        # 报出来的是渔货号（渔枢那边的认货凭据就是这个）
        self.assertEqual(order.hub_sync.get("missing"), [fish])

    def test_30_recall_adds_stock_back(self):
        """买家退货召回：货回到手上 → 渔枢与渔货栈库存加回去（终态唯一的出口）。"""
        item_id, code, fish = self._wms_item(qty=2)
        product_id = self._product(fish, stock=0)
        harbor_id = self._harbor_item(product_id, fish, stock=0)
        with SessionLocal() as s:
            row = s.get(WmsItem, item_id)
            row.status = "SOLD"
            s.commit()
        resp = self.client.post(f"/wms/items/{item_id}/move", data={
            "action": "status", "to_status": "RECALL", "remark": "买家退货",
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(s.get(WmsItem, item_id).status, "RECALL", "召回要登记得上")
            self.assertEqual(int(s.get(Product, product_id).stock or 0), 2, "2 台加回来")
            self.assertEqual(int(s.get(SupplierItem, harbor_id).stock or 0), 2)
