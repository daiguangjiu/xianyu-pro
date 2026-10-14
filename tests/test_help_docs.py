"""流程说明页、推单列表收起已关闭单、订单列表流水号列。

业务口径：
  1 每个模块都能看到「流程 + 状态」说明（推单 / 退单 / 订单），文案与页面提示同源（app/services/help.py）；
  2 推单列表默认不列「已关闭完成」的订单 —— 钱货都处理完了，留在列表里只会让人以为还要催回单、填单号；
  3 订单列表第二列是系统内订单 ID（详情页网址 /orders/57 里的 57），报数、对账用它，点开就是详情。
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
from app.models import (  # noqa: E402
    ORDER_STATE_CLOSED,
    OperationLog,
    Order,
    OrderImage,
    PushBatch,
    PushBatchItem,
    RawMessage,
)
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


class HelpAndPushListTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        self.admin = ensure_default_admin()
        self._clean()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)

    def _clean(self) -> None:
        with SessionLocal() as s:
            s.query(OperationLog).delete()
            s.query(PushBatchItem).delete()
            s.query(PushBatch).delete()
            s.query(OrderImage).delete()
            s.query(Order).delete()
            s.query(RawMessage).delete()
            s.commit()

    def _pushed_order(self, order_no: str, closed: bool = False) -> int:
        """一条已推单的订单；closed=True 则是走完逆向流程的「关闭完成」终态。"""
        with SessionLocal() as s:
            order = Order(
                order_no=order_no,
                product_name="测试商品",
                receiver_name="张三",
                receiver_phone="13800000000",
                sale_amount=100,
                cost_amount=60,
                supplier_name="测试供应商",
                pushed_at=now(),
                supplier_status="pushed",
                order_state=ORDER_STATE_CLOSED if closed else "pushed",
                refund_status="closed" if closed else "",
            )
            s.add(order)
            s.commit()
            return order.id

    def test_help_topics_render(self):
        """推单 / 退单 / 订单三份说明都能打开，流程与状态都在；/help 默认进推单说明。"""
        for topic, keyword in (
            ("push", "复制推单文案"),
            ("refund", "已退款给客户"),
            ("orders", "流水号"),
        ):
            resp = self.client.get(f"/help/{topic}")
            self.assertEqual(resp.status_code, 200, topic)
            self.assertIn(keyword, resp.text)
            self.assertIn("状态：每个取值什么意思", resp.text)
        self.assertEqual(
            self.client.get("/help", follow_redirects=False).headers["location"],
            "/help/push",
        )
        self.assertEqual(self.client.get("/help/not-exist").status_code, 404)

    def test_help_entry_links_on_pages(self):
        """订单列表、推单列表、订单详情都能点到对应的流程说明。"""
        order_id = self._pushed_order("HE-1")
        self.assertIn("/help/orders", self.client.get("/orders").text)
        self.assertIn("/help/push", self.client.get("/batches").text)
        detail = self.client.get(f"/orders/{order_id}").text
        self.assertIn("/help/push", detail)

    def test_push_list_hides_closed_orders(self):
        """已关闭完成的订单默认收起（无需再操作），勾上「显示已关闭 / 已完成」才出现。"""
        self._pushed_order("PL-OPEN")
        self._pushed_order("PL-CLOSED", closed=True)
        resp = self.client.get("/batches")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("PL-OPEN", resp.text)
        self.assertNotIn("PL-CLOSED", resp.text)
        self.assertIn("已关闭完成", resp.text)  # 顶部要写清楚收起了几单、为什么
        shown = self.client.get("/batches?show_closed=1").text
        self.assertIn("PL-CLOSED", shown)
        self.assertIn("已关闭完成，无需操作", shown)  # 灰底 + 单号只读

    def test_order_list_shows_serial_number(self):
        """订单列表第二列是流水号（系统内 ID），点开就是订单详情。"""
        order_id = self._pushed_order("OL-SERIAL")
        html = self.client.get("/orders").text
        self.assertIn("流水号", html)
        self.assertIn(f'href="/orders/{order_id}"', html)
        self.assertIn(f"#{order_id}", html)


if __name__ == "__main__":
    unittest.main()
