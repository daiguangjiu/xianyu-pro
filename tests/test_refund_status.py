"""退单处理状态：按「推到哪一步了」定，不能一律记成「待拦截」。

2026-10-02 修：订单 3317081124078045869 从没推给供应商（pushed_at 空、没物流单号），
群里同步退单后却被记成「已退单 · 待拦截（勿推单）」—— 供应商那边压根没下过单，
哪来的包裹要拦。同类的还有：
  · 已经关闭完成的单，还挂着「待拦截」（状态没跟着收尾）；
  · 客户已收货的退单，也记成「待拦截」（其实拦不住了，该等客户寄回）。
口径统一按 services/refund.refund_path 的四条路径派生（未推单 / 已推单 / 已发货 / 已收货）。
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
from app.models import (  # noqa: E402
    REFUND_STATUS_CLOSED,
    REFUND_STATUS_NOT_PUSHED,
    REFUND_STATUS_PENDING,
    REFUND_STATUS_RECEIVED,
    Order,
    RawMessage,
)
from app.services import refund as refund_service  # noqa: E402
from app.timeutil import now  # noqa: E402
from app.web.auth import ensure_default_admin  # noqa: E402

NOT_PUSHED_LABEL = "无需拦截（未推单）"


class RefundStatusBase(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        ensure_default_admin()
        self._reset()

    def tearDown(self) -> None:
        self._reset()

    def _reset(self) -> None:
        with SessionLocal() as s:
            s.query(RawMessage).delete()
            s.query(Order).delete()
            s.commit()

    def _order(self, order_no: str, **kw) -> int:
        with SessionLocal() as s:
            order = Order(
                platform="xianyu",
                order_no=order_no,
                deal_amount=kw.pop("deal_amount", 100.0),
                msg_time=now(),
                **kw,
            )
            s.add(order)
            s.commit()
            return order.id

    def _read(self, order_id: int) -> Order:
        with SessionLocal() as s:
            return s.get(Order, order_id)

    def _refund(self, order_id: int) -> Order:
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            refund_service.mark_refund(s, order, keyword="此单退", reason="不想要了")
            s.commit()
            return order


class InitialStatusTest(RefundStatusBase):
    def test_10_not_pushed_is_not_intercept(self):
        """没推给供应商 -> 没有包裹可拦：记「无需拦截（未推单）」，不是「待拦截」。"""
        oid = self._order("RS-1")
        order = self._refund(oid)
        self.assertEqual(order.refund_status, REFUND_STATUS_NOT_PUSHED)
        self.assertEqual(order.refund_status_label, NOT_PUSHED_LABEL)

    def test_20_pushed_not_shipped_is_pending(self):
        """已推单、还在等物流单号 -> 要联系供应商撤回发货：待拦截。"""
        oid = self._order("RS-2", pushed_at=now(), supplier_status="pushed")
        order = self._refund(oid)
        self.assertEqual(order.refund_status, REFUND_STATUS_PENDING)

    def test_30_shipped_is_pending(self):
        """已发货、客户还没收到 -> 要联系快递做物流拦截：待拦截。"""
        oid = self._order("RS-3", pushed_at=now(), tracking_no="SF123", is_received=False)
        order = self._refund(oid)
        self.assertEqual(order.refund_status, REFUND_STATUS_PENDING)

    def test_40_received_is_not_intercept(self):
        """客户已收货 -> 包裹拦不住了，等客户寄回：不是待拦截。"""
        oid = self._order("RS-4", pushed_at=now(), tracking_no="SF123", is_received=True)
        order = self._refund(oid)
        self.assertEqual(order.refund_status, REFUND_STATUS_RECEIVED)
        self.assertEqual(order.refund_status_label, "已收货，等客户寄回")

    def test_50_manual_status_not_overwritten(self):
        """人工判过的状态（已发货无法拦截）不再被自动改回「待拦截」。"""
        oid = self._order("RS-5", pushed_at=now(), tracking_no="SF123", refund_status="shipped")
        order = self._refund(oid)
        self.assertEqual(order.refund_status, "shipped")

    def test_60_legacy_blank_status_label(self):
        """老数据没记处理状态：展示按事实派生，未推单的不能显示成「待拦截」。"""
        oid = self._order("RS-6")
        with SessionLocal() as s:
            order = s.get(Order, oid)
            order.order_state = "refund_request"  # 老数据：只落了订单状态
            s.commit()
        order = self._read(oid)
        self.assertTrue(order.is_refund)
        self.assertEqual(order.refund_status_label, NOT_PUSHED_LABEL)


class ClosedStatusTest(RefundStatusBase):
    def _close(self, order_id: int) -> Order:
        with SessionLocal() as s:
            order = s.get(Order, order_id)
            ok, reason = refund_service.close_refund_order(
                s, order, actor="测试", note="线下已微信退款给客户", force=True
            )
            self.assertTrue(ok, reason)
            s.commit()
            return order

    def test_10_closed_is_not_pending(self):
        """关闭完成 -> 处理状态落到「已关闭」，不能还挂着待拦截。"""
        oid = self._order("RS-C1")
        self._refund(oid)
        order = self._close(oid)
        self.assertEqual(order.refund_status, REFUND_STATUS_CLOSED)

    def test_20_supplier_refunded_auto_close(self):
        """供应商已退款自动关闭 -> 处理状态同样落到「已关闭」。"""
        oid = self._order("RS-C2", pushed_at=now(), transfer_status="paid")
        self._refund(oid)
        with SessionLocal() as s:
            order = s.get(Order, oid)
            ok, reason = refund_service.mark_supplier_refunded(s, order, actor="测试")
            self.assertTrue(ok, reason)
            s.commit()
        self.assertEqual(self._read(oid).refund_status, REFUND_STATUS_CLOSED)


class BackfillTest(RefundStatusBase):
    def test_10_fixes_not_pushed_and_closed(self):
        """老数据修正：未推单的「待拦截」改无需拦截；已关闭的改已关闭；人工改过的不动。"""
        self._order("RS-B1", refund_status="pending")  # 未推单却记了待拦截 -> 要改
        self._order("RS-B2", refund_status="pending", order_state="closed")  # 已关闭 -> 改已关闭
        self._order("RS-B3", refund_status="intercepted", pushed_at=now())  # 已推单 -> 不动
        self._order("RS-B4", refund_status="pending", pushed_at=now())  # 已推单 -> 保持待拦截

        with SessionLocal() as s:
            changed = refund_service.fix_intercept_status(s)

        self.assertEqual(changed, 2)
        by_no = {o.order_no: o.refund_status for o in self._all()}
        self.assertEqual(by_no["RS-B1"], REFUND_STATUS_NOT_PUSHED)
        self.assertEqual(by_no["RS-B2"], REFUND_STATUS_CLOSED)
        self.assertEqual(by_no["RS-B3"], "intercepted")
        self.assertEqual(by_no["RS-B4"], REFUND_STATUS_PENDING)

    def test_20_rerun_is_idempotent(self):
        """再跑一次不再改动（可重跑）。"""
        self._order("RS-B5", refund_status="pending")
        with SessionLocal() as s:
            self.assertEqual(refund_service.fix_intercept_status(s), 1)
            self.assertEqual(refund_service.fix_intercept_status(s), 0)

    def test_25_not_pushed_intercepted_is_fixed(self):
        """从没推过单却记成「已拦截」/「已发货无法拦截」：同样是没包裹可拦，一并改回来。"""
        self._order("RS-B9", refund_status="intercepted")  # 未推单却已拦截 -> 改
        self._order("RS-B10", refund_status="shipped")  # 未推单却说已发货 -> 改
        self._order("RS-B11", refund_status="refunded")  # 已退款（非拦截类）-> 不动

        with SessionLocal() as s:
            self.assertEqual(refund_service.fix_intercept_status(s), 2)

        by_no = {o.order_no: o.refund_status for o in self._all()}
        self.assertEqual(by_no["RS-B9"], REFUND_STATUS_NOT_PUSHED)
        self.assertEqual(by_no["RS-B10"], REFUND_STATUS_NOT_PUSHED)
        self.assertEqual(by_no["RS-B11"], "refunded")

    def test_30_backfill_blank_status_by_path(self):
        """启动时给老退单补处理状态：未推单的补「无需拦截」，不能再一律补成待拦截。"""
        self._order("RS-B6", order_state="refund_request")  # 未推单的老退单
        self._order("RS-B7", order_state="refund_request", pushed_at=now())  # 已推单
        self._order("RS-B8", order_state="refunded")  # 已退款

        with SessionLocal() as s:
            self.assertEqual(refund_service.backfill_refund_status(s), 3)

        by_no = {o.order_no: o.refund_status for o in self._all()}
        self.assertEqual(by_no["RS-B6"], REFUND_STATUS_NOT_PUSHED)
        self.assertEqual(by_no["RS-B7"], REFUND_STATUS_PENDING)
        self.assertEqual(by_no["RS-B8"], "refunded")

    def _all(self) -> list[Order]:
        with SessionLocal() as s:
            return list(s.scalars(select(Order)).all())


if __name__ == "__main__":
    unittest.main()
