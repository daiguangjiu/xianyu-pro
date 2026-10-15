"""渔货栈订单 → 渔集「出库管理」联动（2026-10-15）。

渔货栈的订单（origin='self'）不走供应商推单：
  1. 订单详情「复制并标记已推单」换成「同步订单到渔集发货」——把订单信息 /
     商品信息（含渔货号）/ 收货人信息同步成渔集一张销售出库单，幂等；
  2. 渔集出库详情页展示该订单名下的渔货号商品（条码 / 等级 / 状态 / 仓 / 库位），
     点「出库」直接挂单（与扫码拣货同一套校验，可售自动锁货）；
  3. 出库核销填了运单号 → 自动回填渔枢订单（发货页 / 群通知同一套事实）。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, Product, RawMessage, User  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402
from wms import inspection, intake, labeling  # noqa: E402
from wms import seed as wms_seed  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import (  # noqa: E402
    TEST_PASS,
    WmsAuditLog,
    WmsBatch,
    WmsDeliveryOrder,
    WmsItem,
    WmsLabelPrint,
    WmsOutboundItem,
    WmsOutboundOrder,
    WmsStockLedger,
    WmsTestRecord,
)

# 统一检测项全过（与 tests/test_wms.py 同一套 key）
_RESULTS = {
    "appear": TEST_PASS, "inner": TEST_PASS, "power": TEST_PASS, "panel": TEST_PASS,
    "touch": TEST_PASS, "charge": TEST_PASS, "camera": TEST_PASS, "wifi": TEST_PASS,
    "function": TEST_PASS, "accessory": TEST_PASS,
}

FISH = "YS202610150001"


class OrderWmsSyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        wms_init_tables()
        with SessionLocal() as s:
            wms_seed.ensure_defaults(s)
        ensure_default_admin()
        cls.client = TestClient(app)
        with SessionLocal() as s:
            uid = s.query(User).order_by(User.id).first().id
        cls.client.cookies.set(COOKIE_NAME, make_session(uid)[0])

    def setUp(self):
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with SessionLocal() as s:
            # 渔集侧：发货单 → 出库 → 打印 → 检测记录 → 流水 → 货品 → 批次 → 审计
            s.query(WmsDeliveryOrder).delete(synchronize_session=False)
            s.query(WmsOutboundItem).delete(synchronize_session=False)
            s.query(WmsOutboundOrder).delete(synchronize_session=False)
            s.query(WmsLabelPrint).delete(synchronize_session=False)
            s.query(WmsTestRecord).delete(synchronize_session=False)
            s.query(WmsStockLedger).delete(synchronize_session=False)
            s.query(WmsItem).delete(synchronize_session=False)
            s.query(WmsBatch).delete(synchronize_session=False)
            s.query(WmsAuditLog).delete(synchronize_session=False)
            # 渔枢侧：订单与来源消息、商品
            s.query(Order).delete(synchronize_session=False)
            s.query(RawMessage).delete(synchronize_session=False)
            s.query(Product).filter(Product.code == FISH).delete(synchronize_session=False)
            s.commit()

    def _order(self, order_no: str, with_fish: bool = True,
               origin: str = "self", supplier_name: str = "") -> int:
        with SessionLocal() as s:
            pid = None
            if with_fish:
                p = Product(code=FISH, name="测试渔货栈商品", category="数码", price=199.0)
                s.add(p)
                s.flush()
                pid = p.id
            msg = RawMessage(
                message_id=order_no,
                raw_text=f"闲鱼订单已支付\n关联渔货号 {FISH}",
                sender_id="ou_t", sender_name="报单人", chat_name="报单群",
                msg_time=datetime(2026, 10, 15, 10, 0),
            )
            s.add(msg)
            s.flush()
            order = Order(
                platform="xianyu",
                order_no=order_no,
                product_name="测试渔货栈商品",
                spec="黑色",
                quantity=1,
                receiver_name="张三",
                receiver_phone="13800000001",
                receiver_address="广东省 深圳市 南山区 测试路 1 号",
                buyer_nick="买家昵称",
                xianyu_account="Fish01",
                sale_amount=199.0,
                ordered_at=datetime(2026, 10, 15, 10, 1),
                origin=origin,
                supplier_name=supplier_name,
                product_id=pid,
                source_message_id=msg.message_id,
            )
            s.add(order)
            s.commit()
            return order.id

    def _sellable_item(self) -> int:
        """建一台 S 档可售货，渔货号挂在商品档案同一个号上，返回货品 id。"""
        with SessionLocal() as s:
            batch = intake.create_batch(
                s, {"category": "PH", "source_type": "recycle",
                    "qty_expected": 1, "supplier_name": "联动测试"},
                operator="t",
            )
            intake.register(
                s, batch.id,
                [{"brand": "Apple", "model": "iPhone 13", "spec": "128G",
                  "purchase_cost": 100.0, "base_price": 300.0, "fish_code": FISH}],
                operator="t",
            )
            item = intake.list_items(s, batch_id=batch.id)[0]
            inspection.submit(s, item, tester="t", results=dict(_RESULTS),
                              battery_health=95)
            labeling.verify(s, item.barcode, "S", operator="t")
            return item.id

    def _outbound_id(self, target_no: str) -> int:
        with SessionLocal() as s:
            return s.scalar(
                select(WmsOutboundOrder).where(WmsOutboundOrder.target_no == target_no)
            ).id

    # ---------------------------------------------------- ① 同步出库单
    def test_10_sync_creates_outbound_once(self):
        oid = self._order("XY202610150001")
        resp = self.client.post(f"/orders/{oid}/sync-wms", follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:300])
        with SessionLocal() as s:
            rows = list(s.scalars(select(WmsOutboundOrder)).all())
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row.type, "SALE")
            self.assertEqual(row.target_no, "XY202610150001")
            self.assertEqual(row.warehouse_code, "SALE")
            self.assertEqual(row.receiver_name, "张三")
            self.assertEqual(row.receiver_phone, "13800000001")
            self.assertIn("广东省", row.receiver_addr)
            self.assertIn("产品：测试渔货栈商品", row.remark)
            self.assertIn(FISH, row.remark)
            # 发货单主表（渔枢中台 → 渔集，用户定稿字段）
            dv = s.scalar(select(WmsDeliveryOrder))
            self.assertIsNotNone(dv)
            self.assertEqual(dv.platform_order_no, "XY202610150001")
            self.assertEqual(dv.platform_code, "XY")
            self.assertEqual(dv.order_no, str(oid))
            self.assertEqual(dv.receiver_name, "张三")
            self.assertEqual(dv.version, 1)
            self.assertEqual(dv.status, "pending")
            self.assertTrue(dv.ship_deadline, "要求发货时限必填")
            self.assertEqual(dv.outbound_id, row.id)
        # 幂等：再点一次不重复建单
        resp2 = self.client.post(f"/orders/{oid}/sync-wms", follow_redirects=False)
        self.assertEqual(resp2.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(len(list(s.scalars(select(WmsOutboundOrder)).all())), 1)
        # 订单详情：渔货栈订单显示同步按钮 + 已同步出库单入口，不再有推单按钮
        page = self.client.get(f"/orders/{oid}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("同步订单到渔集发货", page.text)
        self.assertIn("渔集出库单", page.text)
        push_block = page.text.split("</section>")[0]
        self.assertNotIn("复制并标记已推单", push_block)

    def test_20_supplier_order_keeps_push_button(self):
        """非渔货栈订单（origin=空）：按钮照旧，同步路由拦下。"""
        with SessionLocal() as s:
            msg = RawMessage(message_id="M2", raw_text="订单", sender_id="ou",
                             msg_time=datetime(2026, 10, 15, 10, 0))
            s.add(msg)
            s.flush()
            order = Order(
                platform="xianyu", order_no="XY202610150002",
                product_name="普通订单", xianyu_account="Fish01",
                sale_amount=10.0, ordered_at=datetime(2026, 10, 15, 10, 2),
                origin="", source_message_id=msg.message_id,
            )
            s.add(order)
            s.commit()
            oid = order.id
        resp = self.client.post(f"/orders/{oid}/sync-wms", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(len(list(s.scalars(select(WmsOutboundOrder)).all())), 0)
        page = self.client.get(f"/orders/{oid}")
        self.assertIn("复制并标记已推单", page.text)
        self.assertNotIn("同步订单到渔集发货", page.text)

    # ---------------------------------------------------- ② 面板 + 点选出库
    def test_30_detail_panel_and_pick_item(self):
        oid = self._order("XY202610150003")
        self.client.post(f"/orders/{oid}/sync-wms", follow_redirects=False)
        item_id = self._sellable_item()
        ob_id = self._outbound_id("XY202610150003")
        page = self.client.get(f"/wms/outbound/{ob_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("订单信息 · 渔货栈订单", page.text)
        self.assertIn(f"/wms/goods/{FISH}", page.text)
        with SessionLocal() as s:
            self.assertIn(s.get(WmsItem, item_id).barcode, page.text)
        self.assertIn("—（未上架）", page.text)  # 库位空着要直说
        # 点「出库」→ 与扫码同一套校验：可售自动锁货后挂单
        resp = self.client.post(
            f"/wms/outbound/{ob_id}/pick-item",
            data={"item_id": item_id}, follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303, resp.text[:300])
        with SessionLocal() as s:
            lines = list(s.scalars(
                select(WmsOutboundItem).where(WmsOutboundItem.outbound_id == ob_id)
            ).all())
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0].item_id, item_id)
            ob = s.get(WmsOutboundOrder, ob_id)
            self.assertEqual(s.get(WmsItem, item_id).locked_by, ob.order_no)
        # 再点：已经在本单里，不重复挂
        resp2 = self.client.post(
            f"/wms/outbound/{ob_id}/pick-item",
            data={"item_id": item_id}, follow_redirects=False,
        )
        self.assertEqual(resp2.status_code, 303)
        with SessionLocal() as s:
            self.assertEqual(len(list(s.scalars(
                select(WmsOutboundItem).where(WmsOutboundItem.outbound_id == ob_id)
            ).all())), 1)

    def test_31_panel_shows_status_and_location(self):
        oid = self._order("XY202610150004")
        self.client.post(f"/orders/{oid}/sync-wms", follow_redirects=False)
        item_id = self._sellable_item()
        with SessionLocal() as s:
            it = s.get(WmsItem, item_id)
            it.location_code = "SALE-01"
            s.commit()
        ob_id = self._outbound_id("XY202610150004")
        page = self.client.get(f"/wms/outbound/{ob_id}")
        # 状态与库位要摆出来：可售 + 库位
        self.assertIn("可售", page.text)
        self.assertIn("SALE-01", page.text)

    # ---------------------------------------------------- ③ 回填物流单号
    def test_40_ship_writes_tracking_back(self):
        oid = self._order("XY202610150005")
        self.client.post(f"/orders/{oid}/sync-wms", follow_redirects=False)
        item_id = self._sellable_item()
        ob_id = self._outbound_id("XY202610150005")
        resp = self.client.post(
            f"/wms/outbound/{ob_id}/pick-item",
            data={"item_id": item_id}, follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        ship = self.client.post(
            f"/wms/outbound/{ob_id}/ship",
            data={"waybill_no": "SF7777777777", "express_company": "顺丰速运"},
            follow_redirects=False,
        )
        self.assertEqual(ship.status_code, 303, ship.text[:300])
        with SessionLocal() as s:
            order = s.get(Order, oid)
            # 物流单号自动传回渔枢订单：发货页（已推单记录）与群通知同一套事实
            self.assertEqual((order.tracking_no or "").strip(), "SF7777777777")
            self.assertTrue(order.tracking_company, "物流公司要按单号自动认")
            self.assertEqual(order.order_state, "shipped")
            # 渔集侧出库单已核销；发货单状态与运单号跟着走
            ob = s.get(WmsOutboundOrder, ob_id)
            self.assertEqual(ob.status, "shipped")
            self.assertEqual(ob.waybill_no, "SF7777777777")
            dv = s.scalar(select(WmsDeliveryOrder))
            self.assertEqual(dv.status, "shipped")
            self.assertEqual(dv.tracking_no, "SF7777777777")
            self.assertEqual(dv.express_company, "顺丰速运")


    # ---------------------------------------------------- 渔货栈YS001 供应链口径
    def test_50_supplier_ys001_also_syncs(self):
        """供应链是渔货栈YS001 的订单也视为渔货栈自营商品，走同步渔集发货。"""
        oid = self._order("XY202610150006", origin="", supplier_name="渔货栈YS001")
        page = self.client.get(f"/orders/{oid}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("同步订单到渔集发货", page.text)
        resp = self.client.post(f"/orders/{oid}/sync-wms", follow_redirects=False)
        self.assertEqual(resp.status_code, 303, resp.text[:300])
        with SessionLocal() as s:
            dv = s.scalar(select(WmsDeliveryOrder))
            self.assertIsNotNone(dv)
            self.assertEqual(dv.platform_order_no, "XY202610150006")
            self.assertEqual(len(list(s.scalars(select(WmsOutboundOrder)).all())), 1)

    def test_55_repush_bumps_version(self):
        """同一单重推：字段有变 version 递增（防乱序），出库单不重建；无变提示已同步。"""
        oid = self._order("XY202610150007")
        self.client.post(f"/orders/{oid}/sync-wms", follow_redirects=False)
        with SessionLocal() as s:
            dv = s.scalar(select(WmsDeliveryOrder))
            self.assertEqual(dv.version, 1)
            ob_n = len(list(s.scalars(select(WmsOutboundOrder)).all()))
            # 收货地址变了 → 重推更新发货单
            order = s.get(Order, oid)
            order.receiver_address = "浙江省 杭州市 西湖区 文三路 9 号"
            s.commit()
        resp = self.client.post(f"/orders/{oid}/sync-wms", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            dv = s.scalar(select(WmsDeliveryOrder))
            self.assertEqual(dv.version, 2)
            self.assertIn("文三路 9 号", dv.receiver_address)
            self.assertEqual(dv.receiver_province, "浙江省")
            # 出库单不重建
            self.assertEqual(
                len(list(s.scalars(select(WmsOutboundOrder)).all())), ob_n
            )
        # 无变化再推一次：提示已同步，版本不再涨
        self.client.post(f"/orders/{oid}/sync-wms", follow_redirects=False)
        with SessionLocal() as s:
            self.assertEqual(s.scalar(select(WmsDeliveryOrder)).version, 2)

    def test_60_deliveries_page(self):
        """发货管理页：定稿字段全部展示；出库核销后状态与运单号跟着变。"""
        oid = self._order("XY202610150008")
        self.client.post(f"/orders/{oid}/sync-wms", follow_redirects=False)
        item_id = self._sellable_item()
        ob_id = self._outbound_id("XY202610150008")
        page = self.client.get("/wms/deliveries")
        self.assertEqual(page.status_code, 200)
        self.assertIn("发货管理", page.text)
        with SessionLocal() as s:
            dv = s.scalar(select(WmsDeliveryOrder))
            self.assertIn(dv.delivery_no, page.text)
            self.assertIn("XY202610150008", page.text)   # 平台原始单号
            self.assertIn("XY", page.text)               # 平台编码
            self.assertIn("张三", page.text)
            self.assertIn("广东省", page.text)            # 四级地址拆开存
            self.assertIn(dv.ship_deadline.strftime("%m-%d"), page.text)
        # 菜单入口在（沿用出库权限域）
        self.assertIn('/wms/deliveries', page.text)
        # 拣货 + 核销 → 发货单已发货 + 运单号
        self.client.post(f"/wms/outbound/{ob_id}/pick-item",
                         data={"item_id": item_id}, follow_redirects=False)
        self.client.post(f"/wms/outbound/{ob_id}/ship",
                         data={"waybill_no": "YT9999999999",
                               "express_company": "圆通速递"},
                         follow_redirects=False)
        page2 = self.client.get("/wms/deliveries")
        self.assertIn("已发货", page2.text)
        self.assertIn("YT9999999999", page2.text)


if __name__ == "__main__":
    unittest.main()
