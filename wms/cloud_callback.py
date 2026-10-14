"""芯烨云打印回调：收下推送、原样留痕、尽量把结果落到本地打印任务上（2026-10-09）。

官方《回调对接文档》有两条硬要求，本模块全部围着它们写：

1. **收到后必须回 ``{"data": "ok"}``** —— 回别的、或超时，平台就当这个地址无效；
2. **响应必须 3 秒内回来** —— 所以这里只做本地落库（一条 insert + 一条 update），
   **一个外部请求都不发**，不回查云端、不发通知。

字段为什么这么宽容：回调里「订单号 / 机器号 / 状态」没有标准写法（orderId、
orderNo、taskId、sn、printerSn、state、printState…），我们按常见写法全认一遍；
**认不出来也照样存原始报文**，事后照着真实报文补一条键名就能对上，不用等下一次
打印来试。这是「先拿到数据、再迭代解析」的取巧：回调地址一旦被平台判无效，
后面就再也收不到东西了。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import WmsCloudPrintEvent, WmsLabelPrint
from .timeutil import now as _now

logger = logging.getLogger("wms.cloud_callback")

# 回调路径（打印设置页展示、平台后台填写的都是它）
CALLBACK_PATH = "/wms/api/cloudprint/callback"

# 同一个意思的字段在不同文档里叫法不同，全列一遍逐个试
_ORDER_KEYS = ("orderId", "orderID", "orderNo", "order_no", "order_id", "orderNum", "orderNumber",
               "taskId", "taskID", "printOrderId", "printOrderNo", "tradeNo", "oid", "id")
_SN_KEYS = ("sn", "SN", "printerSn", "printerSN", "deviceSn", "machineCode", "printerCode",
            "deviceId", "deviceID", "machineSn")
_STATE_KEYS = ("state", "status", "printState", "printStatus", "orderState", "orderStatus",
               "result", "resultCode", "printResult", "code")
_MSG_KEYS = ("msg", "message", "errMsg", "errorMsg", "remark", "desc", "description")

# 归一化后的状态（页面徽标用）
STATE_PRINTED = "printed"
STATE_FAILED = "failed"
STATE_QUEUED = "queued"
STATE_UNKNOWN = "unknown"

# 状态值 → 归一化：数字按芯烨 queryOrderState 的口径（1 = 已打印），其余按字面认
_PRINTED_WORDS = ("1", "printed", "print", "success", "ok", "done", "complete", "completed",
                  "finish", "finished", "已打印", "打印完成", "成功")
_FAILED_WORDS = ("-1", "fail", "failed", "error", "err", "timeout", "cancel", "cancelled",
                 "canceled", "expire", "expired", "失败", "打印失败", "已取消", "超时")
_QUEUED_WORDS = ("0", "2", "wait", "waiting", "pending", "queued", "unprint", "unprinted",
                 "notprint", "未打印", "待打印", "打印中", "排队中")


def _pick(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    """按候选键名逐个试，取第一个非空值（键名大小写不敏感）。"""
    lower = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        val = lower.get(key.lower())
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return ""


def normalize_state(raw: str) -> str:
    """把回调里的状态值归一成 printed / failed / queued / unknown。"""
    s = str(raw or "").strip().lower()
    if not s:
        return STATE_UNKNOWN
    if s in _PRINTED_WORDS:
        return STATE_PRINTED
    if s in _FAILED_WORDS:
        return STATE_FAILED
    if s in _QUEUED_WORDS:
        return STATE_QUEUED
    return STATE_UNKNOWN


def parse(payload: dict[str, Any]) -> dict[str, Any]:
    """从回调报文里尽量抠出「订单号 / 机器号 / 状态 / 说明」。

    报文可能是平铺的 ``{"orderId": ...}``，也可能裹一层 ``{"data": {...}}`` ——
    两种都试，取信息量大的那份。
    """
    data = payload if isinstance(payload, dict) else {}
    inner = data.get("data")
    # 裹了一层且里面是字典：用内层（外层通常只有 code / msg 这类信封字段）
    source = inner if isinstance(inner, dict) and inner else data
    merged = {**data, **source} if source is not data else dict(data)

    order_no = _pick(merged, _ORDER_KEYS)
    printer_sn = _pick(merged, _SN_KEYS)
    raw_state = _pick(merged, _STATE_KEYS)
    message = _pick(merged, _MSG_KEYS)
    state = normalize_state(raw_state)
    # 有机器号但没订单号 → 多半是「打印机在线 / 缺纸」这类机器状态推送
    event_type = "order" if order_no else ("printer" if printer_sn else "unknown")
    return {
        "event_type": event_type,
        "order_no": order_no,
        "printer_sn": printer_sn,
        "state": state,
        "raw_state": raw_state,
        "message": message,
    }


def _apply(session: Session, event: WmsCloudPrintEvent) -> None:
    """把回调结果落到本地打印任务上（只改状态，不发任何外部请求）。"""
    row = None
    if event.order_no:
        row = session.scalar(
            select(WmsLabelPrint)
            .where(WmsLabelPrint.order_no == event.order_no)
            .order_by(WmsLabelPrint.id.desc())
        )
    if row is None:
        event.applied = False
        return
    event.matched_print_id = row.id
    if event.printer_sn:
        row.printer_sn = row.printer_sn or event.printer_sn
    if event.state == STATE_PRINTED:
        row.status = "printed"
        row.printed_at = row.printed_at or _now()
        row.error = ""
    elif event.state == STATE_FAILED:
        # 失败不改状态（还挂在「已下发云端」，页面能继续补打），把原因写到 error 上
        row.error = event.message or f"云端回执：打印失败（{event.raw_state or '未知'}）"
    event.applied = True


def receive(session: Session, payload: dict[str, Any], *, source_ip: str = "") -> WmsCloudPrintEvent:
    """收一条回调：落库 + 尽量回写打印任务。**不抛异常**（接口要无条件回 ok）。"""
    info = parse(payload)
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str)[:8000]
    except Exception:  # noqa: BLE001 报文再怪也得存下来
        raw = str(payload)[:8000]
    event = WmsCloudPrintEvent(
        event_type=info["event_type"],
        order_no=info["order_no"],
        printer_sn=info["printer_sn"],
        state=info["state"],
        raw_state=info["raw_state"],
        message=info["message"],
        payload=raw,
        source_ip=str(source_ip or "")[:64],
        created_at=_now(),
    )
    try:
        session.add(event)
        session.flush()  # 先拿 id，下面 _apply 要写 matched_print_id
        _apply(session, event)
        session.commit()
    except Exception as exc:  # noqa: BLE001 落库失败也不能让响应变 500
        session.rollback()
        logger.warning("云打印回调落库失败（响应仍回 ok）：%s", exc)
        event.applied = False
    return event


def recent(session: Session, limit: int = 12) -> list[WmsCloudPrintEvent]:
    """最近几条回调（打印设置页上看「平台到底推过来没有」）。"""
    return list(
        session.scalars(
            select(WmsCloudPrintEvent).order_by(WmsCloudPrintEvent.id.desc()).limit(limit)
        ).all()
    )


def callback_url(base: str) -> str:
    """回调地址 = 公网域名 + 固定路径（给打印设置页展示、给芯烨后台填）。"""
    root = str(base or "").strip().rstrip("/")
    return f"{root}{CALLBACK_PATH}" if root else CALLBACK_PATH


def state_label(state: str) -> str:
    return {
        STATE_PRINTED: "已打印",
        STATE_FAILED: "失败",
        STATE_QUEUED: "待打印",
        STATE_UNKNOWN: "未识别",
    }.get(state, state)
