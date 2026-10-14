"""物流轨迹查询（独立服务）：三端共用，查一次留一份。

为什么要这一层（2026-10-13 用户拍板）：
    渔货栈用户查订单、渔枢订单页查物流、渔集出库查物流，查的都是同一个菜鸟接口、
    同一批运单号。菜鸟按调用次数算钱 / 限流，同一单号被三个端各查一遍纯属浪费。

    所以把查询收成一个服务：**每次查完在本地留一份存档**，再有人来查先看存档，
    不够新鲜才真打菜鸟。规矩：
      · 在途单：``LOGISTICS_TRACE_TTL_SEC``（默认 1 小时）之内直接给存档；
      · 终态单（已签收 / 已退回 / 已拦截 / 异常）：轨迹不会再变，**永不再查**，
        除非页面上点「刷新」（force=1）；
      · 查不到轨迹的单（刚下单还没揽收）：按空结果短缓存（默认 30 分钟），
        别把额度刷在无效单上；
      · 查询失败：**不覆盖**上次查到的轨迹（宁可给人看旧的，也不能把轨迹清空），
        只记失败原因，并按短缓存退避。

谁在用：
    · 渔枢：``web/pages.py`` 订单「查物流」/「发货详情」
    · 渔货栈：``src/web/router.py`` 订单详情（DownstreamOrder.shipping_no）
    · 渔集：``wms/web/router.py`` 出库详情（WmsOutboundOrder.waybill_no）
    · 对外：``GET /api/logistics/trace?mail_no=``（将来要拆进程，调用方换 HTTP 即可）
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import LogisticsTrace
from ..timeutil import now
from . import error_log
from . import logistics as cainiao

logger = logging.getLogger(__name__)

# 终态：轨迹不会再变，存下来就不再自动刷新
FINAL_STATUSES = frozenset({"signed", "returned", "intercepted", "failed"})
FINAL_STATUS_LABEL = "已完成"

# 单号最长留档长度（超长当无效单号处理）
_MAX_MAIL_NO = 64


def _ttl_sec() -> int:
    return max(0, int(getattr(settings, "logistics_trace_ttl_sec", 3600) or 0))


def _empty_ttl_sec() -> int:
    return max(0, int(getattr(settings, "logistics_trace_empty_ttl_sec", 1800) or 0))


def _clean(mail_no: str) -> str:
    return (mail_no or "").strip()[:_MAX_MAIL_NO]


def _row_for(session: Session, mail_no: str) -> LogisticsTrace | None:
    return session.scalar(
        select(LogisticsTrace).where(LogisticsTrace.mail_no == mail_no)
    )


def _decorate(out: dict) -> dict:
    """补上页面/通知要用的衍生字段（状态中文名、是否异常、官方查询入口）。"""
    status = str(out.get("status") or "")
    cp_name, cp_url = cainiao.official_site(
        str(out.get("cp_code") or ""), str(out.get("cp_name") or "")
    )
    out["status_label"] = cainiao.status_label(status)
    out["abnormal"] = cainiao.is_abnormal(status)
    # 没查到轨迹时才给「去官网查」这条退路，查到了就不啰嗦
    out["official_name"] = cp_name if not out.get("nodes") else ""
    out["official_url"] = cp_url if not out.get("nodes") else ""
    out.setdefault("stale", False)
    return out


def _from_cache(row: LogisticsTrace) -> dict:
    """把存档行翻成调用方要的结构（不打菜鸟）。"""
    row.hit_count = int(row.hit_count or 0) + 1
    out = {
        "ok": bool(row.nodes) or not row.last_error,
        "msg": row.last_error or row.last_msg or "",
        "status": row.status or "",
        "nodes": list(row.nodes or []),
        "mail_no": row.mail_no,
        "cp_code": row.cp_code or "",
        "cp_name": row.cp_name or "",
        "is_final": bool(row.is_final),
        "from_cache": True,
        "queried_at": row.last_queried_at,
    }
    return _decorate(out)


def _is_fresh(row: LogisticsTrace) -> bool:
    """存档还新鲜吗：终态永不过期；其余看 next_refresh_at。"""
    if row.is_final:
        return True
    nxt = row.next_refresh_at
    return bool(nxt and nxt > now())


def _save(
    session: Session,
    row: LogisticsTrace | None,
    *,
    mail_no: str,
    cp_code: str,
    cp_name: str,
    phone_tail: str,
    res: dict,
) -> LogisticsTrace:
    """把一次真实查询的结果写进存档（成功更新轨迹，失败只记原因）。"""
    now_at = now()
    if row is None:
        row = LogisticsTrace(mail_no=mail_no, first_queried_at=now_at)
        session.add(row)

    row.cp_code = cp_code or row.cp_code or ""
    row.cp_name = cp_name or row.cp_name or ""
    row.phone_tail = phone_tail or row.phone_tail or ""
    row.query_count = int(row.query_count or 0) + 1
    row.last_queried_at = now_at

    ok = bool(res.get("ok"))
    nodes = res.get("nodes") or []
    if ok and nodes:
        # 查到了：更新轨迹，失败原因清空
        row.status = str(res.get("status") or "")
        row.nodes = list(nodes)
        row.is_final = row.status in FINAL_STATUSES
        row.last_error = ""
        row.last_msg = str(res.get("msg") or "")[:500]
        row.next_refresh_at = None if row.is_final else now_at + timedelta(
            seconds=_ttl_sec()
        )
    elif ok and not nodes:
        # 查得到单号但没轨迹（刚下单还没揽收）：空结果短缓存
        row.status = ""
        row.nodes = []
        row.is_final = False
        row.last_error = ""
        row.last_msg = str(res.get("msg") or "")[:500]
        row.next_refresh_at = now_at + timedelta(seconds=_empty_ttl_sec())
    else:
        # 失败：**保留**上次查到的轨迹，只记原因 + 短缓存退避
        row.last_error = str(res.get("msg") or "菜鸟查询失败")[:500]
        row.last_msg = ""
        row.is_final = False
        if not row.nodes:
            row.next_refresh_at = now_at + timedelta(seconds=_empty_ttl_sec())
        else:
            row.next_refresh_at = now_at + timedelta(seconds=_empty_ttl_sec())

    session.flush()
    return row


def lookup(
    session: Session,
    mail_no: str,
    *,
    cp_code: str = "",
    cp_name: str = "",
    phone: str = "",
    force: bool = False,
) -> dict:
    """查一条运单的轨迹（三端统一入口）。

    入参：
        mail_no   运单号（必填）
        cp_code   快递公司编码（SF / ZTO …）；空的话按单号现认一次
        cp_name   快递公司显示名（存档里记一笔，页面直接用）
        phone     收件人手机号（只取后 4 位发出去，完整号码不落库）
        force     True = 绕过存档强制实时查（页面的「刷新」按钮）

    返回（比菜鸟原始返回多了三个字段）：
        from_cache   这次是走存档（True）还是真打了菜鸟（False）
        queried_at   这批轨迹的查询时间（存档时间）
        is_final     是否终态（终态不会再自动刷新）
    """
    mail_no = _clean(mail_no)
    if not mail_no:
        return _decorate({
            "ok": False,
            "msg": "没有运单号，查不了",
            "status": "",
            "nodes": [],
            "mail_no": "",
            "cp_code": "",
            "cp_name": "",
            "is_final": False,
            "from_cache": False,
            "queried_at": None,
        })

    row = _row_for(session, mail_no)
    # 这次查询之前的状态（变了才通知，见下面的 notify）
    prev_status = (row.status or "") if row is not None else ""
    if row is not None and not force and _is_fresh(row):
        out = _from_cache(row)
        session.flush()
        return out

    if not cainiao.is_enabled():
        # 没配菜鸟：有旧存档就给旧的（至少还能看），没有就直接说明
        if row is not None and row.nodes:
            out = _from_cache(row)
            out["msg"] = "菜鸟物流查询没启用，显示的是上次存档的轨迹"
            session.flush()
            return out
        return _decorate({
            "ok": False,
            "msg": "没启用菜鸟物流查询：去「系统设置 → 菜鸟物流查询」配置",
            "status": "",
            "nodes": [],
            "mail_no": mail_no,
            "cp_code": cp_code,
            "cp_name": cp_name,
            "is_final": False,
            "from_cache": False,
            "queried_at": None,
        })

    # 认快递公司：调用方给了就用，没给按单号现认
    real_cp = (cp_code or "").strip() or cainiao.guess_cp_code(mail_no)
    real_name = (cp_name or "").strip() or cainiao.guess_company(mail_no)
    tail = cainiao.phone_tail(phone or "")

    try:
        res = cainiao._call(mail_no, cp_code=real_cp, phone=tail)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "msg": f"菜鸟查询异常：{exc}", "status": "", "nodes": []}
        error_log.record(
            kind="logistics_trace",
            scope="query",
            path=mail_no,
            exc=exc,
            message="菜鸟轨迹查询抛异常",
            session=session,
        )

    row = _save(
        session,
        row,
        mail_no=mail_no,
        cp_code=real_cp,
        cp_name=real_name,
        phone_tail=tail,
        res=res,
    )
    new_status = (row.status or "").strip()
    if res.get("ok") and new_status and new_status != prev_status:
        # 状态变了 → 通知订单发送人（发不出去不影响这一次的查询返回）
        try:
            from . import logistics_notify

            logistics_notify.notify_change(
                session,
                mail_no,
                prev_status,
                new_status,
                nodes=list(row.nodes or []),
                cp_name=real_name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("物流状态通知出错（%s）：%s", mail_no, exc)

    out = _from_cache(row)
    out["from_cache"] = False  # 这次是真打了菜鸟
    if not res.get("ok"):
        # 失败时保留旧轨迹给页面看（_save 已经没清 nodes），但 ok/msg 要如实反映这次失败
        out["ok"] = False
        out["msg"] = str(res.get("msg") or "菜鸟查询失败")
        out["stale"] = bool(out["nodes"])  # 有旧轨迹 → 页面提示「显示上次存档」
    session.flush()
    return out


def stats(session: Session) -> dict:
    """存档省了多少次调用：query_count 是真实调菜鸟的次数，hit_count 是走存档的次数。"""
    rows = list(session.scalars(select(LogisticsTrace)).all())
    queried = sum(int(r.query_count or 0) for r in rows)
    hit = sum(int(r.hit_count or 0) for r in rows)
    return {
        "tracked": len(rows),
        "api_calls": queried,
        "cache_hits": hit,
        "saved": max(0, hit),
        "final": sum(1 for r in rows if r.is_final),
    }


def refresh_overdue(session: Session, limit: int = 20) -> list[str]:
    """取「该刷新了」的在途单号（给定时任务用；本期只提供方法，还没接定时）。"""
    rows = session.scalars(
        select(LogisticsTrace)
        .where(LogisticsTrace.is_final.is_(False))
        .where(LogisticsTrace.next_refresh_at.isnot(None))
        .where(LogisticsTrace.next_refresh_at <= now())
        .limit(max(1, limit))
    ).all()
    return [r.mail_no for r in rows]


def last_traced_at(row: LogisticsTrace | None) -> datetime | None:
    return row.last_queried_at if row else None
