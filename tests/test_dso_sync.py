"""渔货栈出单 → 渔枢系统自动建档（把闭环里缺的那一段接上）。

以前这条链路是**断**的：渔货栈店铺出的单只落在 downstream_orders，
渔枢系统要等飞书群里那条订单模板消息解析出来才有这笔单 —— 群里漏发 / 没解析，
渔枢系统就永远没有这笔单（看不见、不会推单、不会发货）。

现在 src/order/service.accept_order() 收单成功后直接同步建档：
  1 同单号在渔枢系统没有 → 新建一条「新建」订单，字段按下游订单带上；
  2 同单号已经有了（群消息先建的）→ 不重复建，只补空字段，**已有值不动**；
  3 同步失败只记日志，绝不把渔货栈的单弄丢。
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import ORDER_ORIGIN_SELF, ORDER_STATE_NEW, Order, OrderLog  # noqa: E402
from app.services import dso_sync, product as product_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402
from src.models import (  # noqa: E402
    ChannelListing,
    DownstreamOrder,
    PriceRule,
    PurchaseOrder,
    SkuMapping,
    SupplierItem,
)
from src.order.service import accept_order  # noqa: E402
from src.schemas import OrderCreateIn, OrderItemIn  # noqa: E402

ITEM_ID = "T-ITEM-0001"
ALI_SKU = "T-SKU-0001"
CHANNEL_SKU = "T-XY-SKU-0001"
DSO_ID = "TXY20261002001"
TITLE = "测试-亚朵四代记忆棉枕"


def _reset_tables() -> None:
    """清空本次用例要用到的表（顺序：先子表后父表，外键才不报错）。"""
    with SessionLocal() as s:
        s.query(OrderLog).delete()
        s.query(Order).delete()
        s.query(PurchaseOrder).delete()
        s.query(DownstreamOrder).delete()
        s.query(ChannelListing).delete()
        s.query(SkuMapping).delete()
        s.query(SupplierItem).delete()
        s.query(PriceRule).delete()
        s.commit()


class DsoSyncBase(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        _reset_tables()
        self._seed_supply()

    def tearDown(self) -> None:
        # 必须把自己造的货源 / SKU 映射清掉：sku_mappings 外键指向 supplier_items，
        # 留着会让后面清 supplier_items 的用例（test_self_goods_detail）删不掉而报外键错。
        _reset_tables()

    def _seed_supply(self) -> None:
        """货源 + SKU 映射 + 加价规则（售价 = 98×2 + 3 = 199）。"""
        with SessionLocal() as s:
            s.add(
                SupplierItem(
                    item_id=ITEM_ID,
                    supplier_id="T-SUP-001",
                    title=TITLE,
                    category_id="home-bedding",
                    cost_price=Decimal("98"),
                    stock=50,
                    return_rate=0.02,
                    sku_list={
                        "skus": [
                            {"sku_id": ALI_SKU, "price": 98, "stock": 50, "spec": "单只装"}
                        ]
                    },
                    last_synced_at=now(),
                )
            )
            s.commit()
            s.add(
                SkuMapping(
                    item_id=ITEM_ID,
                    ali_sku_id=ALI_SKU,
                    channel="xianyu",
                    channel_sku_id=CHANNEL_SKU,
                )
            )
            s.add(
                PriceRule(
                    scope="global",
                    scope_key=None,
                    margin_rate=1.0,
                    fixed_shipping=Decimal("3"),
                    priority=10,
                    is_active=True,
                )
            )
            s.commit()

    def _accept(self, order_no: str = DSO_ID):
        with SessionLocal() as s:
            return accept_order(
                s,
                OrderCreateIn(
                    downstream_order_id=order_no,
                    channel="xianyu",
                    buyer_name="测试买家",
                    buyer_phone="13800001111",
                    address="安徽省滁州市琅琊区凤凰湖畔18栋",
                    items=[OrderItemIn(channel_sku_id=CHANNEL_SKU, qty=1)],
                ),
            )

    def _orders(self, order_no: str = DSO_ID) -> list[Order]:
        with SessionLocal() as s:
            return list(s.scalars(select(Order).where(Order.order_no == order_no)).all())


class AutoCreateTest(DsoSyncBase):
    def test_accept_order_creates_hub_order(self):
        """渔货栈一出单，渔枢系统立刻有这笔单：单号、收货人、售价、成本、来源都对得上。"""
        self._accept()

        orders = self._orders()
        self.assertEqual(len(orders), 1, "渔枢系统应该自动建出这一笔订单")
        order = orders[0]
        self.assertEqual(order.order_no, DSO_ID)
        self.assertEqual(order.origin, ORDER_ORIGIN_SELF, "来源要标成渔货栈")
        self.assertEqual(order.order_state, ORDER_STATE_NEW, "新建的单等运营审单推单")
        self.assertEqual(order.receiver_name, "测试买家")
        self.assertEqual(order.receiver_phone, "13800001111")
        self.assertEqual(order.receiver_address, "安徽省滁州市琅琊区凤凰湖畔18栋")
        self.assertEqual(order.product_name, TITLE)
        self.assertEqual(order.spec, "单只装")
        self.assertEqual(order.sale_amount, 199.0, "售价按加价规则算出来")
        self.assertEqual(order.cost_amount, 98.0, "成本回落货源拿货价")

        with SessionLocal() as s:
            logs = list(s.scalars(select(OrderLog).where(OrderLog.order_id == order.id)).all())
        self.assertTrue(any("自动建档" in (log.action or "") for log in logs), "要留下一条建档日志")

    def test_matches_hub_product_by_name(self):
        """名字对得上就挂到渔枢系统的商品上，统计 / 详情页能直接跳过去。"""
        with SessionLocal() as s:
            product = product_service.create_product(
                s, {"name": TITLE, "price": "199", "cost_amount": "98"}, created_by="测试"
            )
        self._accept()
        order = self._orders()[0]
        self.assertEqual(order.product_id, product.id)

    def test_sync_is_idempotent(self):
        """同一笔下游订单同步两次，渔枢系统只会有一条订单。"""
        self._accept()
        with SessionLocal() as s:
            dso = s.scalar(select(DownstreamOrder).where(DownstreamOrder.downstream_order_id == DSO_ID))
            order, action = dso_sync.sync_downstream_order(s, dso)
        self.assertIn(action, (dso_sync.ACTION_UNCHANGED, dso_sync.ACTION_FILLED))
        self.assertEqual(len(self._orders()), 1, "重复同步不许再建一条")
        self.assertIsNotNone(order)


class NoOverwriteTest(DsoSyncBase):
    def test_fills_blanks_but_keeps_existing_values(self):
        """群里那条消息先建了同单号的单：只补空字段，运营已经审过的值不许被盖掉。"""
        with SessionLocal() as s:
            s.add(
                Order(
                    order_no=DSO_ID,
                    platform="xianyu",
                    order_state=ORDER_STATE_NEW,
                    receiver_name="群里填的收货人",
                    sale_amount=188.0,
                )
            )
            s.commit()

        self._accept()

        orders = self._orders()
        self.assertEqual(len(orders), 1, "不许重复建第二条")
        order = orders[0]
        # 已有值：原样保留
        self.assertEqual(order.receiver_name, "群里填的收货人")
        self.assertEqual(order.sale_amount, 188.0)
        # 空字段：补上
        self.assertEqual(order.receiver_phone, "13800001111")
        self.assertEqual(order.receiver_address, "安徽省滁州市琅琊区凤凰湖畔18栋")
        self.assertEqual(order.product_name, TITLE)
        self.assertEqual(order.cost_amount, 98.0)


class FailureToleranceTest(DsoSyncBase):
    def test_sync_failure_does_not_lose_the_order(self):
        """同步到渔枢系统失败也不能把渔货栈的单弄丢：单照样收，只是渔枢系统那边没建起来。"""
        with mock.patch.object(
            dso_sync, "sync_downstream_order", side_effect=RuntimeError("渔枢系统侧炸了")
        ):
            result = self._accept()

        self.assertEqual(len(result.downstream_orders), 1, "渔货栈照样收单")
        self.assertEqual(self._orders(), [], "渔枢系统侧失败就没建单（等下次补）")
        with SessionLocal() as s:
            dso = s.scalar(
                select(DownstreamOrder).where(DownstreamOrder.downstream_order_id == DSO_ID)
            )
            self.assertIsNotNone(dso, "下游订单必须还在库里")


if __name__ == "__main__":
    unittest.main()
