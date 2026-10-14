"""物流状态通知：状态一变，飞书通知**订单发送人**。

背景（2026-10-13 用户拍板）：
    物流状态要能看出来（待揽收 / 已揽收 / 运输中 / 派送中 / 待取件 / 已签收，
    外加派送失败 / 退回中 / 已退回三个异常分支），**状态一变就得通知到订单发送人**
    —— 就是当初在飞书群里报这单的那个人（``Order.sender_id``）。

    目前只发飞书（先支持一个渠道）。通知由「物流轨迹定时服务」触发，
    **发不出去绝不影响业务**：飞书没启用、订单没关联发送人、接口报错，一律记一笔
    失败原因就完事，谁也不会被拖住。

去重：
    同一运单号的同一个状态**只通知一次**（表上有唯一约束），刷新一百遍
    也不会把同一条「已签收」重复发一百遍。发送失败的记录下次会被覆盖重试。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import LogisticsNotify, Order
from . import logistics as cainiao

logger = logging.getLogger(__name__)

# 通知标题：异常分支要让人一眼看出「出事了」
TITLE_NORMAL = "物流更新"
TITLE_ABNORMAL = "物流异常"


def enabled() -> bool:
    """通知总开关（LOGISTICS_NOTIFY_ENABLED=0 可整体关掉）。"""
    return bool(getattr(settings, "logistics_notify_enabled", True))


def _feishu_ready() -> bool:
    return bool(getattr(settings, "feishu_enabled", False))


def _field_of(order: Order | None, name: str) -> str:
    """取订单上的某个字段（没有订单就是空）。"""
    return str(getattr(order, name, "") or "").strip() if order else ""


def _product_text(order: Order | None) -> str:
    """「产品：xxx」—— 有规格就带上（同名不同规格的两单才分得开）。"""
    name = _field_of(order, "product_name")
    spec = _field_of(order, "spec")
    if name and spec and spec not in name:
        return f"{name}（{spec}）"
    return name or spec


# 轨迹最多带几条（够看就行，别把人刷屏）
TRACE_LIMIT = 3


def _short_time(text: str) -> str:
    """「2026-10-13 15:20:00」→「10-13 15:20」—— 年份在现场没用，省下来给内容。"""
    raw = (text or "").strip()
    return raw[5:16] if len(raw) >= 16 else raw


def build_text(
    *,
    order_no: str = "",
    mail_no: str = "",
    cp_name: str = "",
    from_status: str = "",
    to_status: str = "",
    node: dict | None = None,
    nodes: list[dict] | None = None,
    product: str = "",
    receiver_name: str = "",
    receiver_phone: str = "",
    receiver_address: str = "",
) -> str:
    """通知文案（2026-10-13 改版）：**带上收件人信息，分块排版，别都堆一起**。

    三块，中间空行隔开：
      ① 这单是谁的：订单号 / 产品 / 收货人 / 手机号 / 地址 —— 发送人一眼认出是自己报的那单
      ② 快递那边：快递公司 + 运单号 + 状态怎么变的
      ③ 物流轨迹：最新几条，一行一条（时间 + 内容）
    """
    title = TITLE_ABNORMAL if cainiao.is_abnormal(to_status) else TITLE_NORMAL
    lines = [f"【{title}】"]

    # ① 订单本体
    if order_no:
        lines.append(f"订单 {order_no}")
    if product:
        lines.append(f"产品：{product}")
    if receiver_name:
        lines.append(f"收货人：{receiver_name}")
    if receiver_phone:
        lines.append(f"手机号码：{receiver_phone}")
    if receiver_address:
        lines.append(f"地址信息：{receiver_address}")

    # ② 快递：跟订单信息空一行隔开，两件事别糊成一团
    lines.append("")
    company = f"{cp_name} " if cp_name else ""
    lines.append(f"{company}{mail_no}".strip())
    old = cainiao.status_label(from_status) if from_status else "未查询"
    new = cainiao.status_label(to_status)
    lines.append(f"状态：{old} → {new}")

    # ③ 轨迹：一行一条，时间排在前面对齐着看
    trace = [n for n in (nodes or []) if (n.get("desc") or "").strip()]
    if not trace and node and (node.get("desc") or "").strip():
        trace = [node]
    if trace:
        lines.append("")
        lines.append("物流轨迹：")
        for n in trace[:TRACE_LIMIT]:
            at = _short_time(n.get("time") or "")
            desc = (n.get("desc") or "").strip()
            lines.append(f"{at}  {desc}" if at else desc)
    return "\n".join(lines)


def _orders_of(session: Session, mail_no: str) -> list[Order]:
    """按运单号找订单（正向件 / 退回件都算）。"""
    if not mail_no:
        return []
    rows = list(
        session.scalars(
            select(Order)
            .where((Order.tracking_no == mail_no) | (Order.return_tracking_no == mail_no))
            .order_by(Order.id)
        ).all()
    )
    return rows


def _already_sent(session: Session, mail_no: str, to_status: str) -> bool:
    row = session.scalar(
        select(LogisticsNotify).where(
            LogisticsNotify.mail_no == mail_no,
            LogisticsNotify.to_status == to_status,
            LogisticsNotify.ok.is_(True),
        )
    )
    return row is not None


def _clear_failed(session: Session, mail_no: str, to_status: str) -> None:
    """清掉上一次的失败记录（唯一约束下要给重试腾位置）。"""
    for row in session.scalars(
        select(LogisticsNotify).where(
            LogisticsNotify.mail_no == mail_no,
            LogisticsNotify.to_status == to_status,
        )
    ).all():
        session.delete(row)
    session.flush()


def _send(receiver_id: str, text: str) -> tuple[bool, str]:
    """飞书私聊发一条文本（收件人用 open_id）。"""
    try:
        from ..feishu.client import get_client
    except Exception as exc:  # noqa: BLE001
        return False, f"飞书客户端不可用：{exc}"
    try:
        get_client().send_text(receiver_id, text, receive_id_type="open_id")
    except Exception as exc:  # noqa: BLE001  发不出去不能拖住物流刷新
        return False, str(exc)[:400]
    return True, ""


def notify_change(
    session: Session,
    mail_no: str,
    from_status: str,
    to_status: str,
    *,
    nodes: list[dict] | None = None,
    cp_name: str = "",
) -> dict[str, Any]:
    """状态变了 → 通知订单发送人（幂等：同一单同一状态只发一次）。

    返回 {"sent": bool, "reason": str, "text": str, "receivers": [名字]}
    不抛异常：所有失败都落进 logistics_notifies 的 error 里。
    """
    mail_no = (mail_no or "").strip()
    to_status = (to_status or "").strip()
    out: dict[str, Any] = {"sent": False, "reason": "", "text": "", "receivers": []}
    if not mail_no or not to_status:
        out["reason"] = "没有运单号或状态，不通知"
        return out
    if not enabled():
        out["reason"] = "物流通知已关闭（LOGISTICS_NOTIFY_ENABLED=0）"
        return out
    if _already_sent(session, mail_no, to_status):
        out["reason"] = "这个状态已经通知过了"
        return out
    if not from_status and not cainiao.is_abnormal(to_status):
        # 第一次跟踪到这个单号：正向状态（运输中 / 已签收…）只是建立基线，不打扰人；
        # **异常件例外**（派送失败 / 退回中 / 已退回）—— 那种必须立刻让人知道。
        # 不这么收口的话，定时任务第一次扫历史订单会瞬间刷出一堆「未查询 → 已签收」。
        out["reason"] = "首次跟踪到的是正常状态，只是建立基线不通知（异常件才首次就通知）"
        return out

    orders = _orders_of(session, mail_no)
    receivers: list[tuple[str, str]] = []  # (open_id, 姓名)
    for o in orders:
        rid = (o.sender_id or "").strip()
        if rid and rid not in {r[0] for r in receivers}:
            receivers.append((rid, (o.sender_name or "").strip()))
    order_no = (orders[0].order_no if orders else "").strip()

    head_order = orders[0] if orders else None
    text = build_text(
        order_no=order_no,
        mail_no=mail_no,
        cp_name=(cp_name or "").strip(),
        from_status=from_status,
        to_status=to_status,
        nodes=nodes,
        # 收件信息跟着订单走：发送人收到通知就知道是哪一单、寄给谁的
        product=_product_text(head_order),
        receiver_name=_field_of(head_order, "receiver_name"),
        receiver_phone=_field_of(head_order, "receiver_phone"),
        receiver_address=_field_of(head_order, "receiver_address"),
    )
    out["text"] = text

    if not receivers:
        _clear_failed(session, mail_no, to_status)
        session.add(
            LogisticsNotify(
                mail_no=mail_no,
                order_no=order_no,
                from_status=from_status,
                to_status=to_status,
                abnormal=cainiao.is_abnormal(to_status),
                ok=False,
                error="这单没有关联的订单发送人（可能是渔集出库单，或订单没记飞书发送人）",
                text=text,
            )
        )
        session.flush()
        out["reason"] = "没有发送人"
        return out
    if not _feishu_ready():
        _clear_failed(session, mail_no, to_status)
        session.add(
            LogisticsNotify(
                mail_no=mail_no,
                order_no=order_no,
                from_status=from_status,
                to_status=to_status,
                receiver_id=receivers[0][0],
                receiver_name=receivers[0][1],
                abnormal=cainiao.is_abnormal(to_status),
                ok=False,
                error="飞书没启用（系统设置 → 飞书）",
                text=text,
            )
        )
        session.flush()
        out["reason"] = "飞书没启用"
        return out

    # 只记一条汇总：给 N 个人发，任一个成功就算通知到（唯一约束是一单一状态一条）
    _clear_failed(session, mail_no, to_status)
    sent_names: list[str] = []
    last_error = ""
    for rid, name in receivers:
        ok, err = _send(rid, text)
        if ok:
            sent_names.append(name or rid)
        else:
            last_error = err
    session.add(
        LogisticsNotify(
            mail_no=mail_no,
            order_no=order_no,
            from_status=from_status,
            to_status=to_status,
            receiver_id=receivers[0][0],
            receiver_name=receivers[0][1] if len(receivers) == 1 else "、".join(
                n or r for r, n in receivers
            ),
            abnormal=cainiao.is_abnormal(to_status),
            ok=bool(sent_names),
            error="" if sent_names else last_error,
            text=text,
        )
    )
    session.flush()
    out["receivers"] = sent_names
    out["sent"] = bool(sent_names)
    out["reason"] = "" if sent_names else (last_error or "发送失败")
    if sent_names:
        logger.info("物流状态已通知（%s → %s）：%s", from_status, to_status, mail_no)
    else:
        logger.warning("物流状态通知失败（%s）：%s", mail_no, last_error)
    return out


def recent(session: Session, limit: int = 20) -> list[LogisticsNotify]:
    """最近的通知记录（页面 / 排查用）。"""
    return list(
        session.scalars(
            select(LogisticsNotify).order_by(LogisticsNotify.id.desc()).limit(max(1, limit))
        ).all()
    )
