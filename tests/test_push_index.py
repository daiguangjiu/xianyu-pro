"""「今日第 x 单」的供应商排队口径（2026-09-25 版本）。

业务口径：
  1 按供应商维度计数：同一家供应商「当天产生的订单」按订单发送时间先后排队；
  2 与「推没推」无关：还没推的订单也占号，同一个号不会重复发给供应商；
  3 撤回的订单不占号：撤了中间一单，后面订单的单号自动往前补
    （撤回处理即时重排，后台定时任务兜底修复）；
  4 修复会记下「推单时报的号 -> 修正后的号」并写操作日志，重复执行不重复记账，
    撤回被撤销后自动复位。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    OperationLog,
    Order,
    OrderImage,
    RawMessage,
    Supplier,
)
from app.services import push as push_service  # noqa: E402
from app.services import recall as recall_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

ADMIN = {"username": "admin", "password": "xianyu2026"}
SUPPLIER_A = "安徽梦想电商（BH001）"
SUPPLIER_B = "华强北数码（HQ002）"


def _at(hour: int, minute: int = 0, days_ago: int = 0) -> datetime:
    """构造「今天（或前几天）几点几分」的订单发送时间。"""
    base = now() - timedelta(days=days_ago)
    return datetime(base.year, base.month, base.day, hour, minute)


class PushIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        with SessionLocal() as s:
            s.query(OperationLog).delete()
            s.query(OrderImage).delete()
            s.query(Order).delete()
            s.query(RawMessage).delete()
            s.query(Supplier).delete()
            s.commit()
        self.client = TestClient(app)
        resp = self.client.post("/login", data=ADMIN, follow_redirects=False)
        assert resp.status_code == 303, resp.text

    def tearDown(self) -> None:
        with SessionLocal() as s:
            s.query(OperationLog).delete()
            s.query(OrderImage).delete()
            s.query(Order).delete()
            s.query(RawMessage).delete()
            s.query(Supplier).delete()
            s.commit()

    # ---------------------------------------------------------------- 工具
    def _order(
        self,
        order_no: str,
        msg_time: datetime,
        supplier: str = SUPPLIER_A,
        message_id: str = "",
        **kw,
    ) -> int:
        with SessionLocal() as s:
            if message_id:
                # 撤回事件要求「本地有这条消息」才会去找关联订单
                s.add(
                    RawMessage(
                        message_id=message_id,
                        chat_id="oc_index",
                        sender_name="小鱼",
                        msg_time=msg_time,
                        raw_text="产品：倍思Q2充电宝",
                        parse_status="parsed",
                    )
                )
            order = Order(
                platform="xianyu",
                order_no=order_no,
                supplier_name=supplier,
                msg_time=msg_time,
                source_message_id=message_id,
                receiver_name="李女士",
                receiver_phone="18303136964",
                **kw,
            )
            s.add(order)
            s.commit()
            return order.id

    def _index(self, order_id: int) -> int:
        with SessionLocal() as s:
            return push_service.supplier_daily_index(s, s.get(Order, order_id))

    def _push(self, order_id: int) -> int:
        """复制推单信息：把订单标成已推单，并返回此时报给供应商的第几单。"""
        with SessionLocal() as s:
            order = push_service.mark_pushed(s, s.get(Order, order_id))
            return order.push_index

    def _get(self, order_id: int) -> Order:
        with SessionLocal() as s:
            return s.get(Order, order_id)

    def _detail(self, order_id: int) -> str:
        page = self.client.get(f"/orders/{order_id}")
        self.assertEqual(page.status_code, 200)
        return page.text

    # ---------------------------------------------------------------- 排队口径
    def test_index_follows_message_order_not_push_order(self):
        """按订单发送时间排队：先推第 3 单也是第 3 单，未推的订单同样占号。"""
        first = self._order("IDX-A", _at(9, 0))
        second = self._order("IDX-B", _at(10, 0))
        third = self._order("IDX-C", _at(11, 0))

        self.assertEqual(self._index(first), 1)
        self.assertEqual(self._index(second), 2)
        self.assertEqual(self._index(third), 3)

        # 乱序推单：号码仍然按发送时间排，不会出现两个「第 2 单」
        self.assertEqual(self._push(third), 3)
        self.assertEqual(self._push(first), 1)
        self.assertEqual(self._push(second), 2)
        self.assertEqual(
            {self._get(o).push_index for o in (first, second, third)}, {1, 2, 3}
        )

    def test_day_bucket_uses_message_time(self):
        """自然日按订单产生时间切分：跨天才推的单不算进今天的排队。"""
        today = self._order("IDX-TODAY", _at(9, 0))
        yesterday = self._order("IDX-YESTERDAY", _at(23, 0, days_ago=1))
        with SessionLocal() as s:
            s.get(Order, yesterday).pushed_at = now()
            s.commit()

        self.assertEqual(self._index(today), 1, "昨天的单不占今天的号")
        self.assertEqual(self._index(yesterday), 1, "昨天那一批里它是第 1 单")
        with SessionLocal() as s:
            label = push_service.day_label(
                push_service.index_day(s.get(Order, yesterday))
            )
        old = now() - timedelta(days=1)
        self.assertEqual(label, f"（{old.month}/{old.day}）" + push_service.WEEKDAYS[old.weekday()])

    def test_suppliers_are_counted_separately(self):
        """供应商维度：A 家的单不影响 B 家的号，同一家的不同写法算一家。"""
        # 供应商档案：订单里记「安徽梦想电商（BH001）」或只记编码都要认成一家
        with SessionLocal() as s:
            s.add(Supplier(name="安徽梦想电商", code="BH001"))
            s.commit()

        a1 = self._order("IDX-S1", _at(9, 0))
        b1 = self._order("IDX-S2", _at(10, 0), supplier=SUPPLIER_B)
        a2 = self._order("IDX-S3", _at(11, 0))
        # 同一家的另一种写法（只写编码）也要算一家
        a3 = self._order("IDX-S4", _at(12, 0), supplier="BH001")

        self.assertEqual(self._index(a1), 1)
        self.assertEqual(self._index(b1), 1)
        self.assertEqual(self._index(a2), 2)
        self.assertEqual(self._index(a3), 3)

    # ---------------------------------------------------------------- 撤回重排
    def test_recall_shifts_following_numbers_and_logs_fix(self):
        """撤回中间一单：后面订单的单号自动往前补，并记下「推单时报的号」。"""
        first = self._order("IDX-R1", _at(9, 0), message_id="om_idx_1")
        second = self._order("IDX-R2", _at(10, 0), message_id="om_idx_2")
        third = self._order("IDX-R3", _at(11, 0), message_id="om_idx_3")
        for oid in (first, second, third):
            self._push(oid)
        self.assertEqual(self._get(third).push_index, 3)

        with SessionLocal() as s:
            out = recall_service.handle_message_recalled(s, "om_idx_2")
        self.assertEqual(out["marked"], 1)

        with SessionLocal() as s:
            o = s.get(Order, third)
            self.assertEqual(o.push_index, 3, "推单时报给供应商的号要留着对账")
            self.assertEqual(o.push_index_fixed, 2, "撤回后自动修正为第 2 单")
            self.assertEqual(push_service.supplier_daily_index(s, o), 2)
            self.assertIsNotNone(
                s.scalar(select(OperationLog.id).where(OperationLog.action == "单号修正"))
            )

        text = self._detail(third)
        self.assertIn("第 2 单", text)
        self.assertIn("单号已修正（推单时报第 3 单）", text)

    def test_repair_is_idempotent_and_resets_after_undo(self):
        """定时修复重复执行不重复记账；撤销撤回后单号回到原位。"""
        first = self._order("IDX-U1", _at(9, 0), message_id="om_undo_1")
        second = self._order("IDX-U2", _at(10, 0), message_id="om_undo_2")
        self._push(first)
        self._push(second)

        with SessionLocal() as s:
            recall_service.handle_message_recalled(s, "om_undo_1")
        with SessionLocal() as s:
            first_run = push_service.repair_daily_indexes(s)
            second_run = push_service.repair_daily_indexes(s)
        self.assertEqual(first_run["fixed"], 0, "撤回时已经即时重排过，无需再修")
        self.assertEqual(second_run["fixed"], 0)
        self.assertEqual(self._index(second), 1)
        self.assertIn("单号已修正（推单时报第 2 单）", self._detail(second))

        with SessionLocal() as s:
            recall_service.undo_recalled(s, s.get(Order, first))
            out = push_service.repair_daily_indexes(s)
        self.assertEqual(out["fixed"], 0)
        with SessionLocal() as s:
            self.assertEqual(s.get(Order, second).push_index_fixed, 0, "单号回到原位")
        self.assertEqual(self._index(second), 2)
        self.assertNotIn("单号已修正", self._detail(second))

    def test_repair_backfills_legacy_pushed_orders(self):
        """老数据（本版本前推的单）没有记录报给供应商的号：修复时按当前口径回填。"""
        legacy = self._order("IDX-LEGACY", _at(9, 0))
        with SessionLocal() as s:
            o = s.get(Order, legacy)
            o.pushed_at = now()
            o.supplier_status = "pushed"
            s.commit()

        with SessionLocal() as s:
            out = push_service.repair_daily_indexes(s)
        self.assertEqual(out["filled"], 1)
        self.assertEqual(self._get(legacy).push_index, 1)

    def test_recalled_order_does_not_hold_a_number(self):
        """撤回的订单不占号：第一个单被撤回后，第二个单就是第 1 单。"""
        first = self._order("IDX-N1", _at(9, 0), message_id="om_no_1")
        second = self._order("IDX-N2", _at(10, 0))
        with SessionLocal() as s:
            recall_service.handle_message_recalled(s, "om_no_1")
        self.assertTrue(self._get(first).is_recalled)
        self.assertEqual(self._index(second), 1)

    def test_push_text_uses_message_day(self):
        """推单文案里的日期与第几单都按订单产生时间（对账口径一致）。"""
        order_id = self._order("IDX-TXT", _at(9, 0))
        with SessionLocal() as s:
            order = push_service.mark_pushed(s, s.get(Order, order_id))
            index = push_service.supplier_daily_index(s, order)
            day = push_service.day_label(push_service.index_day(order))
        self.assertIn(f"今日{day} 第{index}单", push_service.push_text(order, index))
        self.assertEqual(order.push_index, index)


if __name__ == "__main__":
    unittest.main()
