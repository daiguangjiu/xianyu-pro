"""订单处理日志：人工标记 / 改动订单状态的动作留痕。

业务口径（2026-09）：订单上「手动标记状态」的地方都要留一条日志 ——
标记撤回 / 撤销撤回、标记退单 / 标记拦截、维护退单处理、标记已推单、
改「是否转账」、改订单状态、回填物流单号；订单详情页在「推单信息」下面
按时间倒序列出来，后续查订单问题时能直接看到谁在什么时候把单子怎么了。

自动流程（群里撤单 / 退单、供应商回单）也走同一张表，操作人记为空，
页面上显示为「系统自动」。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Order, OrderLog
from ..timeutil import now

logger = logging.getLogger(__name__)

# 动作名（页面上直接展示，也是筛选 / 统计的口径）
ACTION_RECALL = "标记撤回"
ACTION_RECALL_UNDO = "撤销撤回"
ACTION_REFUND = "标记退单"
ACTION_INTERCEPT = "标记拦截"
ACTION_REFUND_DETECT = "识别退单"
ACTION_REFUND_HANDLE = "维护退单处理"
# 逆向退单流程：同步供应商 / 补理由 / 退款 / 恢复正常订单
ACTION_REFUND_SYNC = "同步供应商退单"
ACTION_REFUND_REASON = "维护退单理由"
ACTION_REFUND_PAID = "修改是否退款"
ACTION_RESTORE = "恢复正常订单"
ACTION_PUSH = "标记已推单"
ACTION_TRANSFER = "修改是否转账"
ACTION_STATE = "修改订单状态"
ACTION_SENDER = "修改发送人"
ACTION_MERGE = "合并订单"
# 同一买家同款的两笔订单：指纹相同但不是重复发送（截图 / 订单编号不同），拆成两单留痕
ACTION_SPLIT_DUPLICATE = "识别为另一笔订单"
# 群里回一条「这个改一下手机号码」再贴一遍：手机号等身份信息变了，按同一笔订单更新留痕
# （2026-10-05）指纹里含手机号，改号本来会被当成新订单，这里记一笔便于事后追溯
ACTION_IDENTITY_CHANGE = "按身份信息变更更新"
ACTION_IMPORT = "导入历史订单"
ACTION_SUPPLIER_STATUS = "修改推单状态"
ACTION_TRACKING = "回填物流单号"
# 物流单号同步到发货群的结果（成功 / 没勾选 / 被关掉）：跟「回填物流单号」配对看，
# 一眼能分出「单号存了」和「群里通知了」是两件事
ACTION_TRACKING_NOTIFY = "通知发货群"
# 2026-09-29 逆向订单口径：退款金额 / 退单物流单号 / 已收货 / 供应商已退款 / 关闭完成
ACTION_REFUND_AMOUNT = "维护退款金额"
# 自动流程（解析 / 截图 OCR / 重新解析 / 去重合并）写进来的成本看着不对：拦下未写入
ACTION_COST_BLOCKED = "成本异常已拦下"
ACTION_RETURN_TRACKING = "维护退单物流单号"
ACTION_RECEIVED = "标记已收货"
ACTION_SUPPLIER_REFUNDED = "标记供应商已退款"
ACTION_CLOSE = "关闭完成"
# 2026-10 退单步骤的事实时间：已叫停（不推 / 撤回发货 / 拦截）、已收到退回件
ACTION_STOPPED = "标记已叫停"
ACTION_RETURN_RECEIVED = "标记收到退回件"
# 物流轨迹状态回写（接入物流查询后由接口调用，来源写「物流轨迹」）
ACTION_TRACKING_STATUS = "物流状态更新"
# 2026-10-06 退单登记口径：群里可能分多次发「原单号 + 退回单号 + 备注」，
# 备注是累加的（历史备注不清），每次追加留一条，页面上能看到谁什么时候说了什么
ACTION_NOTE_APPEND = "追加订单备注"

# 没填操作人时的展示文案（群里自动触发 / 系统同步）
SYSTEM_OPERATOR = "系统自动"


def log(
    session: Session,
    order: Order,
    action: str,
    detail: str = "",
    operator: str = "",
    commit: bool = False,
) -> OrderLog:
    """给订单追加一条处理日志（不 commit，由调用方决定提交时机）。"""
    row = OrderLog(
        order_id=order.id,
        action=(action or "").strip()[:32],
        detail=(detail or "").strip()[:500],
        operator=(operator or "").strip()[:64],
        created_at=now(),
    )
    session.add(row)
    if commit:
        session.commit()
    logger.info("订单 %s 处理日志：%s%s", order.order_no, action, f"（{detail}）" if detail else "")
    return row


def list_for_order(session: Session, order_id: int, limit: int = 100) -> list[OrderLog]:
    """某笔订单的处理日志（最新的在最前面）。"""
    return list(
        session.scalars(
            select(OrderLog)
            .where(OrderLog.order_id == order_id)
            .order_by(OrderLog.created_at.desc(), OrderLog.id.desc())
            .limit(limit)
        ).all()
    )


def operator_label(row: OrderLog) -> str:
    """操作人展示文案：没记人的算系统自动。"""
    return (row.operator or "").strip() or SYSTEM_OPERATOR


# 发货这条线上的日志：推单 / 回填或修改单号 / 通知发货群。
# 列表页的「保存记录、通知记录」和发货详情的操作日志都用这一份口径。
SHIPPING_ACTIONS: tuple[str, ...] = (ACTION_PUSH, ACTION_TRACKING, ACTION_TRACKING_NOTIFY)


def shipping_logs_for_orders(
    session: Session, order_ids: list[int], limit_per_order: int = 4
) -> dict[int, list[OrderLog]]:
    """批量取多笔订单的发货日志（列表页一次查完，不要一单一条 SQL）。

    返回 {order_id: [最新在前、最多 limit_per_order 条]}。
    """
    ids = [int(i) for i in order_ids if i]
    if not ids:
        return {}
    rows = session.scalars(
        select(OrderLog)
        .where(OrderLog.order_id.in_(ids), OrderLog.action.in_(SHIPPING_ACTIONS))
        .order_by(OrderLog.created_at.desc(), OrderLog.id.desc())
    ).all()
    out: dict[int, list[OrderLog]] = {}
    for row in rows:
        bucket = out.setdefault(row.order_id or 0, [])
        if len(bucket) < limit_per_order:
            bucket.append(row)
    return out


def shipping_logs_for_order(
    session: Session, order_id: int, limit: int = 100
) -> list[OrderLog]:
    """某笔订单的发货日志（发货详情页用）：推单 / 回填单号 / 通知发货群。"""
    return [
        row
        for row in list_for_order(session, order_id, limit=limit)
        if row.action in SHIPPING_ACTIONS
    ]



