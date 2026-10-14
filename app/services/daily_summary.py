"""每日出单情况汇总：按「人」统计当天出单/退单，定时推送到飞书群。

业务口径（2026-09）：
  1 每天定时（默认 24:00，即次日 00:00）把当天的出单情况汇总成一条消息推到群里；
  2 按「人」分组统计：出单店铺数（出货闲鱼号去重）、出单数、出单金额、退单数、退单金额；
  3 「人」= 订单的上行消息发送人（群里谁发的这笔订单；取不到姓名时用 open_id 短串 / 未知）；
  4 出单金额优先取售价 sale_amount，缺失时退回成交价 deal_amount；
  5 出单归属日期 = 下单时间 ordered_at → 消息时间 msg_time → 归档时间 created_at 的第一有效值；
    退单归属日期 = 识别到退单的时间 refund_at；
  6 同一天只推一次（推送记录落在 daily_summary_logs 表，随数据库持久），
    可在系统设置页按日期手动推送 / 补推。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import String, case, cast, func, select, text

from ..config import settings
from ..db import dialect_name
from ..models import DailySummaryLog, Order, SyncState, not_recalled, not_refund_order
from ..timeutil import beijing_now, fmt, now
from . import message_rules
from .notify import render_notify

logger = logging.getLogger(__name__)

# 出单金额：没有维护售价时退回成交价（避免补录前金额显示为 0）
_AMOUNT_EXPR = case((Order.sale_amount > 0, Order.sale_amount), else_=Order.deal_amount)

# 店铺（出货闲鱼号）去重计数表达式：留空的闲鱼号不计入店铺数
_SHOP_EXPR = case((Order.xianyu_account != "", Order.xianyu_account), else_=None)


def _day_expr(column) -> Any:
    """取**北京时间那一天**的日期字符串（与 stats._day_expr 同一口径）。

    为什么要 +8：库里存的是 UTC（2026-10-03 时间口径），直接截前 10 位会得到 UTC 日期，
    北京凌晨 0–8 点出的单就被算到前一天 —— 日报、按天统计全都跟着错。

    两种库写法不同（以前统一写成 SQLite 那套，线上 PostgreSQL 直接报
    ``function date(timestamp without time zone, character varying) does not exist``
    → 首页 500）：
      · SQLite     → ``date(col, '+8 hours')`` 截前 10 位（SQLite 没有真正的 DATE 类型）
      · PostgreSQL → ``to_char(col + 8 小时, 'YYYY-MM-DD')``
    """
    if dialect_name() == "sqlite":
        return func.substr(cast(func.date(column, "+8 hours"), String), 1, 10)
    return func.to_char(column + text("INTERVAL '8 hours'"), "YYYY-MM-DD")


def _order_day_expr() -> Any:
    return _day_expr(func.coalesce(Order.ordered_at, Order.msg_time, Order.created_at))


def _refund_day_expr() -> Any:
    return _day_expr(Order.refund_at)


def _shop_count() -> Any:
    return func.count(func.distinct(_SHOP_EXPR))


def _person_label(name: str, sender_id: str) -> str:
    """汇总里的「人」：优先真实姓名，其次 open_id 短串。"""
    name = (name or "").strip()
    if name:
        return name
    sid = (sender_id or "").strip()
    if sid:
        return f"未知成员（{sid[-6:]}）"
    return "未知来源"


def _money(value: float) -> str:
    return f"{float(value or 0):,.2f}"


# ------------------------------------------------------------------ 统计
def collect(session, day: date) -> dict:
    """统计某一天按「人」维度的出单 / 退单情况。"""
    day_s = day.isoformat()

    order_rows = session.execute(
        select(
            Order.sender_name,
            Order.sender_id,
            func.count(Order.id).label("orders"),
            _shop_count().label("shops"),
            func.coalesce(func.sum(_AMOUNT_EXPR), 0.0).label("amount"),
            func.coalesce(func.sum(Order.quantity), 0).label("qty"),
        )
        .where(
            _order_day_expr() == day_s,
            not_refund_order(),
            not_recalled(),
        )
        .group_by(Order.sender_name, Order.sender_id)
    ).all()

    refund_rows = session.execute(
        select(
            Order.sender_name,
            Order.sender_id,
            func.count(Order.id).label("refunds"),
            func.coalesce(func.sum(_AMOUNT_EXPR), 0.0).label("refund_amount"),
        )
        .where(_refund_day_expr() == day_s, not_recalled())
        .group_by(Order.sender_name, Order.sender_id)
    ).all()

    refund_map = {
        (r[0] or "", r[1] or ""): (int(r.refunds or 0), round(float(r.refund_amount or 0), 2))
        for r in refund_rows
    }

    people: list[dict] = []
    for r in order_rows:
        key = (r[0] or "", r[1] or "")
        refunds, refund_amount = refund_map.pop(key, (0, 0.0))
        people.append(
            {
                "name": _person_label(key[0], key[1]),
                "open_id": key[1],
                "shops": int(r.shops or 0),
                "orders": int(r.orders or 0),
                "quantity": int(r.qty or 0),
                "amount": round(float(r.amount or 0), 2),
                "refunds": refunds,
                "refund_amount": round(float(refund_amount), 2),
            }
        )
    # 当天只有退单、没有出单的人也要出现在汇总里
    for key, (refunds, refund_amount) in refund_map.items():
        people.append(
            {
                "name": _person_label(key[0], key[1]),
                "open_id": key[1],
                "shops": 0,
                "orders": 0,
                "quantity": 0,
                "amount": 0.0,
                "refunds": refunds,
                "refund_amount": refund_amount,
            }
        )

    people.sort(key=lambda x: (-x["amount"], -x["orders"], -x["refunds"], x["name"]))

    total = {
        "people": len(people),
        "shops": _total_shops(session, day_s),
        "orders": sum(p["orders"] for p in people),
        "quantity": sum(p["quantity"] for p in people),
        "amount": round(sum(p["amount"] for p in people), 2),
        "refunds": sum(p["refunds"] for p in people),
        "refund_amount": round(sum(p["refund_amount"] for p in people), 2),
    }
    return {"date": day_s, "people": people, "total": total}


def _total_shops(session, day_s: str) -> int:
    """当天有出单的店铺总数（跨人去重）。"""
    rows = session.execute(
        select(Order.xianyu_account)
        .where(
            _order_day_expr() == day_s,
            Order.xianyu_account != "",
            not_refund_order(),
            not_recalled(),
        )
        .distinct()
    ).all()
    return len(rows)


# ------------------------------------------------------------------ 文案
ATOM_ALL = '<at user_id="all"></at>'  # 飞书「@所有人」：这条是发给全群的日报


def build_text(payload: dict, at_users: bool | None = None) -> str:
    """构造汇总消息文本（设置页可配模板，留空用内置格式）。

    模板变量：{{ 日期 }} {{ 明细 }}（每人一行）{{ 合计 }} {{ 人数 }} {{ 店铺数 }}
    {{ 出单数 }} {{ 出单金额 }} {{ 退单数 }} {{ 退单金额 }} {{ 无出单 }}。
    「@ 谁」这条是整群日报，勾了就在最前面加 @所有人，不按名单逐个 @。

    飞书 text 不支持 markdown，用空格 + 竖线排版即可。
    """
    if at_users is None:
        at_users = settings.daily_summary_at_users
    mention = f"{ATOM_ALL} " if at_users else ""

    people = payload["people"]
    empty_tip = "当天没有出单，也没有退单记录。"
    detail = "\n".join(
        f"{idx}. {p['name']}：店铺 {p['shops']} 家 ｜ "
        f"出单 {p['orders']} 单 ¥{_money(p['amount'])} ｜ "
        f"退单 {p['refunds']} 单 ¥{_money(p['refund_amount'])}"
        for idx, p in enumerate(people, 1)
    )
    t = payload["total"]
    total_line = (
        f"合计：店铺 {t['shops']} 家 ｜ 出单 {t['orders']} 单 ¥{_money(t['amount'])} ｜ "
        f"退单 {t['refunds']} 单 ¥{_money(t['refund_amount'])}"
    )

    text = render_notify(
        settings.daily_summary_template,
        {
            "日期": payload["date"],
            "明细": detail or empty_tip,
            "无出单": empty_tip if not people else "",
            "合计": total_line,
            "人数": str(len(people)),
            "店铺数": str(t["shops"]),
            "出单数": str(t["orders"]),
            "出单金额": _money(t["amount"]),
            "退单数": str(t["refunds"]),
            "退单金额": _money(t["refund_amount"]),
        },
    )
    if text:
        return mention + text

    lines = [f"📊 今日（{payload['date']}）出单情况汇总"]
    if not people:
        lines.append(empty_tip)
    if detail:
        lines.append(detail)
    lines.append(total_line)
    return mention + "\n".join(lines)


# ------------------------------------------------------------------ 推送
def target_chat_ids(session) -> list[str]:
    """推送目标群：专用配置 > FEISHU_CHAT_IDS > 已加入的同步群。"""
    ids = [x.strip() for x in settings.daily_summary_chat_ids if x.strip()]
    if not ids:
        ids = [x.strip() for x in settings.feishu_chat_ids if x.strip()]
    if not ids:
        ids = [c for c in session.scalars(select(SyncState.chat_id)).all() if c]
    # 去重保持顺序
    out: list[str] = []
    for cid in ids:
        if cid not in out:
            out.append(cid)
    return out


def send_summary(
    session=None,
    day: date | None = None,
    skip_empty: bool | None = None,
    client=None,
) -> dict:
    """推送某一天的汇总；返回推送结果（含文本，方便前台预览/落日志）。"""
    from ..db import SessionLocal  # 局部导入：避免 db 初始化顺序问题

    owns_session = session is None
    if owns_session:
        session = SessionLocal()
    try:
        target = day or default_target_date()
        payload = collect(session, target)
        result: dict[str, Any] = {
            "date": payload["date"],
            "people": payload["total"]["people"],
            "chat_ids": [],
            "sent": 0,
            "skipped": "",
            "errors": [],
            "text": "",
        }

        empty = not payload["people"]
        if empty and (settings.daily_summary_skip_empty if skip_empty is None else skip_empty):
            result["skipped"] = "当天没有出单与退单记录"
            result["text"] = build_text(payload)
            return result

        result["text"] = build_text(payload)
        chats = target_chat_ids(session)
        if not chats:
            result["errors"].append("没有可推送的群：请在系统设置填写汇总推送群")
            return result

        if client is None:
            from ..feishu.client import get_client

            client = get_client()
        if not client.enabled:
            result["errors"].append("未配置飞书凭证，无法推送")
            return result

        if not message_rules.group_only_anyway("daily_summary"):
            result["errors"].append("消息中心里已关掉每日汇总的群推送")
            return result

        sent: list[str] = []
        for cid in chats:
            try:
                client.send_text(cid, result["text"], receive_id_type="chat_id")
                sent.append(cid)
            except Exception as exc:  # noqa: BLE001  单群失败不影响其它群
                result["errors"].append(f"{cid}: {exc}"[:300])
        result["chat_ids"] = sent
        result["sent"] = len(sent)
        if sent:
            save_state(
                {
                    "last_date": payload["date"],
                    "sent_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                    "chat_ids": sent,
                    "people": payload["total"]["people"],
                    "orders": payload["total"]["orders"],
                    "amount": payload["total"]["amount"],
                    "refunds": payload["total"]["refunds"],
                    "refund_amount": payload["total"]["refund_amount"],
                }
            )
            logger.info(
                "每日出单汇总已推送 %s：%s 人 / %s 单 / ¥%s",
                payload["date"],
                payload["total"]["people"],
                payload["total"]["orders"],
                payload["total"]["amount"],
            )
        return result
    finally:
        if owns_session:
            session.close()


# ------------------------------------------------------------------ 定时与状态
def default_target_date() -> date:
    """默认统计日：24 点推送时统计「昨天」，其它整点推送当天。"""
    current = beijing_now()
    hour = max(min(int(settings.daily_summary_hour), 24), 0)
    return (current - timedelta(days=1)).date() if hour >= 24 else current.date()


def next_run_time() -> datetime:
    """下次推送时间（北京时间）。"""
    current = now()
    hour = max(min(int(settings.daily_summary_hour), 24), 0)
    if hour >= 24:
        target = (current + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:
        target = current.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= current:
            target += timedelta(days=1)
    return target


def load_state() -> dict:
    """读取最近一次推送记录（没推过返回空 dict）。"""
    from ..db import SessionLocal

    with SessionLocal() as session:
        row = session.scalar(
            select(DailySummaryLog).order_by(DailySummaryLog.day.desc()).limit(1)
        )
        if row is None:
            return {}
        return {
            "last_date": row.day,
            "sent_at": fmt(row.sent_at, "%Y-%m-%d %H:%M:%S") if row.sent_at else "",
            "chat_ids": [x for x in (row.chat_ids or "").split(",") if x],
            "people": row.people,
            "orders": row.orders,
            "amount": row.amount,
            "refunds": row.refunds,
            "refund_amount": row.refund_amount,
        }


def save_state(payload: dict) -> None:
    """记录一次成功推送；同一天重复推送时覆盖为最新结果。"""
    from ..db import SessionLocal

    day = str(payload.get("last_date") or "").strip()
    if not day:
        return
    with SessionLocal() as session:
        row = session.scalar(select(DailySummaryLog).where(DailySummaryLog.day == day))
        if row is None:
            row = DailySummaryLog(day=day)
            session.add(row)
        row.sent_at = now()
        row.chat_ids = ",".join(payload.get("chat_ids") or [])
        row.people = int(payload.get("people") or 0)
        row.orders = int(payload.get("orders") or 0)
        row.amount = float(payload.get("amount") or 0.0)
        row.refunds = int(payload.get("refunds") or 0)
        row.refund_amount = float(payload.get("refund_amount") or 0.0)
        session.commit()


def run_if_due(session=None, force: bool = False, client=None) -> dict | None:
    """到点就推送一次（同一天只推一次），返回结果或 None。"""
    if not settings.daily_summary_enabled and not force:
        return None
    # 定时规则看的是**北京时间**（库里存 UTC，别拿 UTC 的小时/日期判断到点没有）
    local = beijing_now()
    hour = max(min(int(settings.daily_summary_hour), 24), 0)
    if hour >= 24:
        target = (local - timedelta(days=1)).date()
    else:
        target = local.date()
        if not force and local.hour < hour:
            return None
    state = load_state()
    if not force and state.get("last_date") == target.isoformat():
        return None
    return send_summary(session=session, day=target, client=client)


def summary_loop(interval_seconds: int = 60) -> None:
    """后台定时线程：每分钟检查一次，到点推送当天汇总。"""
    while True:
        time.sleep(max(int(interval_seconds), 20))
        try:
            result = run_if_due()
            if result and result.get("sent"):
                logger.info(
                    "定时推送每日出单汇总 %s 到 %s 个群",
                    result["date"],
                    result["sent"],
                )
        except Exception as exc:  # noqa: BLE001  定时任务失败不影响主服务
            logger.warning("每日出单汇总异常: %s", exc)
