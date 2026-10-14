"""渔货栈采购域 → 渔枢系统资金页的桥（2026-10-03）。

渔货栈新采购域（`stock_orders`）付款以前是「自己库里转个状态」，
渔枢系统的资金页完全看不到这笔支出 —— 应付流水只有老 1688 推单才有。
这里把采购付款记成渔枢系统资金页的一条「公司自营应付」流水：

  · 付款成功 → 建 / 更新一条待结算流水（按采购单号幂等）；
  · 已支付的采购单取消 → 那条流水置为已作废（钱要退回来，不该再挂着应付）。

分端部署（渔枢系统那套库读不到）时一律静默跳过：采购流程不能因为桥断了走不下去。
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# 采购付款的流水备注：资金页上一眼能看出这不是销售订单的应付
_NOTE_PAID = "渔货栈采购单 {order_no}（{kinds} 种 / {qty} 件）"
_NOTE_VOID = "采购单 {order_no} 已取消（已支付，退款处理），不再结算"


def _hub():
    """拿到渔枢系统那套的 Session / Settlement / 常量；不可用返回 None。"""
    try:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import Settlement
        from app.services import finance as finance_service
        return select, SessionLocal, Settlement, finance_service
    except Exception as exc:  # noqa: BLE001  分端部署时压根没有渔枢系统那套库
        logger.info("渔枢系统不可用，采购流水不上传：%s", exc)
        return None


def _biz_day(value: datetime | None) -> str:
    """账单日：库里存 UTC，业务日按**北京时间**算（北京凌晨 0–8 点的单算当天）。

    与渔枢系统侧 `finance.biz_day_of` 同一个口径，别拿 UTC 的零点当边界。
    """
    try:
        from app.timeutil import BEIJING_ZONE, beijing_now, to_local
    except Exception:  # noqa: BLE001  分端部署没有渔枢系统那套时按服务器本地时间兜底
        from datetime import datetime as _dt

        when = value or _dt.now()
    else:
        when = to_local(value, BEIJING_ZONE) or beijing_now()
    return when.strftime("%Y-%m-%d")


def sync_paid_purchase(stock_order) -> int | None:
    """一笔已付款的采购单 → 渔枢系统资金页的应付流水（幂等，按采购单号）。"""
    hub = _hub()
    if hub is None:
        return None
    select, SessionLocal, Settlement, finance_service = hub
    order_no = (stock_order.order_no or "").strip()
    if not order_no:
        return None
    amount = round(float(stock_order.pay_amount or 0), 2)
    if amount <= 0:
        return None
    counterparty = (stock_order.supplier_name or "").strip() or "渔货栈平台"
    note = _NOTE_PAID.format(
        order_no=order_no,
        kinds=int(stock_order.item_kinds or 0),
        qty=int(stock_order.total_qty or 0),
    )
    day = _biz_day(stock_order.paid_at)
    try:
        with SessionLocal() as s:
            row = s.scalar(
                select(Settlement).where(
                    Settlement.order_no == order_no,
                    Settlement.kind == finance_service.SETTLEMENT_SUPPLIER,
                )
            )
            if row is None:
                row = Settlement(
                    kind=finance_service.SETTLEMENT_SUPPLIER,
                    order_no=order_no,
                    counterparty=counterparty,
                    direction=finance_service.SETTLEMENT_DIRECTIONS[
                        finance_service.SETTLEMENT_SUPPLIER
                    ],
                    amount=amount,
                    biz_day=day,
                    biz_month=day[:7],
                    status=finance_service.SETTLEMENT_PENDING,
                    note=note,
                )
                s.add(row)
            else:
                row.amount = amount
                row.counterparty = counterparty
                row.biz_day = day
                row.biz_month = day[:7]
                row.note = note
                if row.status == finance_service.SETTLEMENT_VOID:
                    row.status = finance_service.SETTLEMENT_PENDING
            s.commit()
            return row.id
    except Exception as exc:  # noqa: BLE001  桥坏了不能影响采购流程
        logger.warning("采购单 %s 同步渔枢系统资金流水失败：%s", order_no, exc)
        return None


def void_purchase_settlement(order_no: str, *, reason: str = "") -> bool:
    """已支付的采购单取消 / 退款：把那条应付流水置为已作废。"""
    hub = _hub()
    if hub is None:
        return False
    select, SessionLocal, Settlement, finance_service = hub
    order_no = (order_no or "").strip()
    if not order_no:
        return False
    try:
        with SessionLocal() as s:
            row = s.scalar(
                select(Settlement).where(
                    Settlement.order_no == order_no,
                    Settlement.kind == finance_service.SETTLEMENT_SUPPLIER,
                )
            )
            if row is None:
                return False
            row.status = finance_service.SETTLEMENT_VOID
            row.note = (reason or "").strip() or _NOTE_VOID.format(order_no=order_no)
            s.commit()
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("采购单 %s 作废渔枢系统资金流水失败：%s", order_no, exc)
        return False
