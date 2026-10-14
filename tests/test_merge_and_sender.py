"""订单详情页维护发送人 + 订单合并（退单消息误建的那笔并回真正的订单）。

业务口径（2026-09）：
  · 历史订单（导入 / 老数据）没有飞书发送人，详情页要能手填 —— 填写后资金页的
    「公司自营应收」对象才对得上；
  · 群里「此单退」这类消息早期被误建成独立订单（成本 0、临时单号），真正的订单是另一笔：
    在要保留的那笔上把对方合并进来 —— 收货信息 / 发送人 / 截图 / 退单标记并过来，
    对方订单删除，同一单不会在统计里重复算。
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
from app.models import Order, OrderLog, Settlement  # noqa: E402
from app.services import finance as finance_service  # noqa: E402
from app.services import order_log as order_log_service  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}


def _order(**kw) -> Order:
    base = {
        "platform": "xianyu",
        "product_name": "倍思 Q2电充电宝 20000毫安 30w",
        "sale_amount": 50.0,
        "ordered_at": datetime(2026, 9, 24),
        "order_state": "pushed",
    }
    base.update(kw)
    return Order(**base)


class MergeAndSenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_default_admin()
        cls.client = TestClient(app)
        resp = cls.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def setUp(self):
        with SessionLocal() as s:
            s.query(OrderLog).delete()
            s.query(Settlement).delete()
            s.query(Order).delete()
            s.commit()
            # 主订单：历史导入的订单（有成本，没有收货信息与发送人）
            self.dst = _order(
                order_no="HIS-20260924-03", cost_amount=45.0, supplier_name="梦想电商"
            )
            # 误建的退单订单：有收货信息、发送人、退单标记，成本 0
            self.src = _order(
                order_no="TMP-20260926162511-A6E6EC",
                receiver_name="蔡",
                receiver_phone="15952742074",
                receiver_address="江苏省徐州市新沂市马陵山镇马陵山",
                sender_id="ou_661086d67455bee9a5bd6e6dce712656",
                sender_name="范志强",
                order_status="refund",
                order_state="canceled",
                refund_status="refunded",
                refund_keyword="此单退",
                refund_at=datetime(2026, 9, 26, 16, 25, 12),
                is_recalled=True,
                recall_reason="人工标记：这条消息其实是退单（误建成新订单）",
            )
            s.add_all([self.dst, self.src])
            s.commit()
            finance_service.sync_order(s, self.dst, commit=True)
            finance_service.sync_order(s, self.src, commit=True)
            self.dst_id, self.src_id = self.dst.id, self.src.id
            self.src_no = self.src.order_no

    def _open(self, order_id: int):
        return self.client.get(f"/orders/{order_id}")

    def test_10_sender_can_be_edited(self):
        """历史订单没有发送人：详情页编辑保存后要落到订单上，并写一条处理日志。"""
        resp = self.client.post(
            f"/orders/{self.dst_id}/edit",
            data={"sender_name": "范志强"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            order = s.get(Order, self.dst_id)
            self.assertEqual(order.sender_name, "范志强")
            logs = s.scalars(
                select(OrderLog).where(
                    OrderLog.order_id == self.dst_id,
                    OrderLog.action == order_log_service.ACTION_SENDER,
                )
            ).all()
            self.assertTrue(logs, "改发送人要留一条处理日志")
            # 应收流水的结算对象 = 订单发起人：填了发送人之后要跟着变
            row = s.scalar(
                select(Settlement).where(
                    Settlement.order_id == self.dst_id,
                    Settlement.kind == finance_service.SETTLEMENT_INITIATOR,
                )
            )
            self.assertEqual(row.counterparty, "范志强")

    def test_20_merge_moves_receiver_and_refund(self):
        """退单订单并入正向订单：收货信息 / 发送人 / 退单标记并过来，源订单删除。"""
        resp = self.client.post(
            f"/orders/{self.dst_id}/merge",
            data={"merge_order_no": self.src_no},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertIsNone(s.get(Order, self.src_id), "被合并的订单要删掉")
            dst = s.get(Order, self.dst_id)
            self.assertEqual(dst.receiver_name, "蔡")
            self.assertEqual(dst.receiver_phone, "15952742074")
            self.assertEqual(dst.receiver_address, "江苏省徐州市新沂市马陵山镇马陵山")
            self.assertEqual(dst.sender_name, "范志强")
            self.assertEqual(dst.refund_status, "refunded", "退单标记要跟着并过来")
            self.assertEqual(dst.refund_keyword, "此单退")
            self.assertEqual(dst.refund_at, datetime(2026, 9, 26, 16, 25, 12), "沿用原退单时间")
            self.assertEqual(dst.order_state, "refunded", "并过来的退单让本单变成已退单")
            self.assertFalse(dst.is_recalled, "误建单的撤回标记不能带到本单")
            self.assertEqual(dst.cost_amount, 45.0, "本单已有的成本不能被对方的 0 覆盖")
            self.assertEqual(dst.supplier_name, "梦想电商")
            self.assertEqual(dst.temp_order_no, self.src_no, "原单号留档备查")
            logs = s.scalars(
                select(OrderLog).where(
                    OrderLog.order_id == self.dst_id,
                    OrderLog.action == order_log_service.ACTION_MERGE,
                )
            ).all()
            self.assertTrue(logs, "合并要留一条处理日志")
            # 退单不再结算：合并后本单的两条流水都要作废
            rows = s.scalars(select(Settlement).where(Settlement.order_id == self.dst_id)).all()
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row.status, finance_service.SETTLEMENT_VOID)

    def test_30_merge_rejects_unknown_and_self(self):
        """订单号找不到 / 填了自己：提示原因，不动数据。"""
        resp = self.client.post(
            f"/orders/{self.dst_id}/merge",
            data={"merge_order_no": "NOT-EXIST"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        resp = self.client.post(
            f"/orders/{self.dst_id}/merge",
            data={"merge_order_no": "HIS-20260924-03"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        with SessionLocal() as s:
            self.assertIsNotNone(s.get(Order, self.src_id), "合并失败时源订单不能被删")
            self.assertIsNone(s.scalars(
                select(OrderLog).where(
                    OrderLog.order_id == self.dst_id,
                    OrderLog.action == order_log_service.ACTION_MERGE,
                )
            ).first())

    def test_40_detail_page_has_sender_and_merge(self):
        """详情页要有发送人输入框与合并入口。"""
        html = self._open(self.dst_id).text
        self.assertIn('name="sender_name"', html)
        self.assertIn(f"/orders/{self.dst_id}/merge", html)


if __name__ == "__main__":
    unittest.main()
