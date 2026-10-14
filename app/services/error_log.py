"""系统异常日志（2026-10-09）：把线上报错抓下来，让「用户说打不开」变成「第几行抛了什么」。

**为什么要有这一层**：

· 以前线上报错只有容器日志里那行 ``GET /xxx 500``，滚几屏就没了；用户在页面上
  只看到「上传失败，请重试」，截图发过来是一句 ``Unexpected token '<'`` ——
  那是后端 500 返回了 HTML 错误页、前端却按 JSON 解析，谁都读不出原因。
· 现在两头都记：**服务端**未捕获异常落一条（含完整堆栈 + 请求现场）；
  **前端**拿到非 JSON 响应 / 脚本报错也上报一条（含 HTTP 状态码 + 响应前 200 字）。
· 三个子系统（渔枢 / 检测仓 / 渔货栈）共用 ``error_logs`` 一张表，靠 ``scope`` 区分 ——
  它们本来就跑在同一个进程里，所以一处兜底就全覆盖。

**写日志绝不能再把请求搞死**：所有落库都包在自己的 try/except 里，用独立 Session。
"""
from __future__ import annotations

import logging
import traceback as tb_mod
from typing import Any

from sqlalchemy import func, select

from ..db import SessionLocal
from ..models import ErrorLog
from ..timeutil import now as _now

logger = logging.getLogger("order-hub.errors")

# 子系统代号 → 页面上的名字
SCOPES: dict[str, str] = {"app": "渔枢系统", "wms": "检测仓", "hub": "渔货栈"}
KIND_LABELS: dict[str, str] = {"server": "服务端异常", "client": "前端上报"}

MAX_MESSAGE = 2000      # 异常描述
MAX_TRACEBACK = 8000    # 堆栈：够看到最里层的调用点
MAX_DETAIL = 2000       # 请求现场摘要
MAX_BODY_SNIPPET = 600  # 前端报上来的响应原文（截断，防把整页 HTML 灌进来）


def scope_of(path: str) -> str:
    """按路径判断属于哪个子系统（列表页的标签、筛选都用它）。"""
    p = str(path or "")
    if p.startswith("/wms"):
        return "wms"
    if p.startswith("/self") or p.startswith("/api/v1"):
        return "hub"
    return "app"


def scope_label(scope: str) -> str:
    return SCOPES.get(str(scope or ""), str(scope or "") or "渔枢系统")


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(str(kind or ""), str(kind or ""))


def wants_json(request: Any) -> bool:
    """这个请求该拿到 JSON 还是 HTML 错误页。

    规则：接口路径一律 JSON；其余看 ``Accept`` —— 浏览器地址栏导航会带 ``text/html``，
    而 fetch / XHR 带的是 ``*/*``。**关键是不能让 fetch 拿到 HTML**，前端 ``r.json()``
    一解析就报 ``Unexpected token '<'``，把真正的原因盖掉。
    """
    path = str(getattr(getattr(request, "url", None), "path", "") or "")
    if path.startswith(("/api", "/wms/api", "/self/api", "/api/v1")):
        return True
    accept = str(request.headers.get("accept") or "")
    return "text/html" not in accept


def _human_size(raw: Any) -> str:
    try:
        size = int(str(raw).strip())
    except (TypeError, ValueError):
        return str(raw or "")
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _write(session: Any, data: dict) -> int:
    row = ErrorLog(**data)
    session.add(row)
    session.commit()
    return int(row.id)


def record(
    *,
    kind: str = "server",
    scope: str = "",
    method: str = "",
    path: str = "",
    status_code: int = 500,
    exc: BaseException | None = None,
    message: str = "",
    traceback_text: str = "",
    detail: str = "",
    user_id: int | None = None,
    username: str = "",
    ip: str = "",
    ua: str = "",
    session: Any = None,
) -> int | None:
    """落一条异常日志，返回记录 id（失败返回 None，**绝不抛出**）。

    给了 ``session`` 就用它写（调用方自己的事务里），否则自己开一个独立 Session ——
    异常处理路径上原请求的 Session 很可能已经脏了，不能指望它。
    """
    data = {
        "kind": str(kind or "server")[:16],
        "scope": str(scope or scope_of(path))[:16],
        "method": str(method or "")[:8],
        "path": str(path or "")[:255],
        "status_code": int(status_code or 500),
        "exc_type": type(exc).__name__ if exc is not None else "",
        "message": (str(message or "") or (str(exc) if exc is not None else ""))[:MAX_MESSAGE],
        "traceback": str(traceback_text or "")[:MAX_TRACEBACK],
        "detail": str(detail or "")[:MAX_DETAIL],
        "user_id": user_id,
        "username": str(username or "")[:32],
        "ip": str(ip or "")[:64],
        "ua": str(ua or "")[:255],
    }
    try:
        if session is not None:
            return _write(session, data)
        with SessionLocal() as own:
            return _write(own, data)
    except Exception as exc:  # noqa: BLE001 记日志失败绝不能盖掉原始错误
        logger.warning("异常日志落库失败：%s", exc)
        return None


def _context_of(request: Any) -> str:
    """请求现场摘要：查询串 / 内容类型 / 体积。

    **只记元信息、不展开内容** —— 上传接口的 body 是几 MB 图片，塞进日志毫无意义，
    还会把日志表撑大（要的是「多大、什么类型、哪个路径」，不是图片本身）。
    """
    parts: list[str] = []
    query = str(getattr(getattr(request, "url", None), "query", "") or "")
    if query:
        parts.append(f"?{query}"[:300])
    ctype = str(request.headers.get("content-type") or "")
    if ctype:
        parts.append(f"Content-Type: {ctype}"[:120])
    length = request.headers.get("content-length")
    if length:
        parts.append(f"请求体积 {_human_size(length)}")
    if request.headers.get("referer"):
        parts.append(f"来自 {str(request.headers.get('referer'))[:160]}")
    return " · ".join(parts)


def _operator_of(request: Any) -> tuple[int | None, str]:
    """谁能对上就记谁；对不上（未登录 / 会话过期）就留空，不能因此再抛异常。"""
    try:
        from ..web.auth import current_user

        user = current_user(request)
        if user is not None:
            return getattr(user, "id", None), str(getattr(user, "username", "") or "")
    except Exception as exc:  # noqa: BLE001
        logger.debug("异常日志取账号失败：%s", exc)
    return None, ""


def record_request(
    request: Any,
    exc: BaseException,
    *,
    status_code: int = 500,
    detail: str = "",
) -> int | None:
    """把「某个请求炸了」记下来：方法 / 路径 / 账号 / IP / 现场 + 完整堆栈。"""
    path = str(getattr(getattr(request, "url", None), "path", "") or "")
    user_id, username = _operator_of(request)
    try:
        from .audit import client_ip

        ip = client_ip(request)
    except Exception:  # noqa: BLE001
        ip = ""
    stack = "".join(tb_mod.format_exception(type(exc), exc, exc.__traceback__))
    return record(
        kind="server",
        scope=scope_of(path),
        method=str(getattr(request, "method", "") or ""),
        path=path,
        status_code=status_code,
        exc=exc,
        traceback_text=stack,
        detail=" · ".join(p for p in (_context_of(request), str(detail or "")) if p),
        user_id=user_id,
        username=username,
        ip=ip,
        ua=str(request.headers.get("user-agent") or ""),
    )


def record_client(request: Any, payload: dict) -> int | None:
    """前端上报：页面拿到非 JSON 响应（或脚本崩了）时把现场报回来。

    前端能看到服务端看不到的东西 —— **那个非 JSON 响应的原文和状态码**，
    正是「上传失败」这类问题唯一有用的线索。
    """
    body = str(payload.get("body") or "")[:MAX_BODY_SNIPPET]
    url = str(payload.get("url") or "")[:255]
    status = payload.get("status") or 0
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        status_code = 0
    user_id, username = _operator_of(request)
    try:
        from .audit import client_ip

        ip = client_ip(request)
    except Exception:  # noqa: BLE001
        ip = ""
    detail_parts = [f"页面：{url}" if url else "", f"HTTP {status_code}" if status_code else ""]
    if body:
        detail_parts.append(f"响应原文：{body}")
    return record(
        kind="client",
        scope=scope_of(url or str(getattr(getattr(request, "url", None), "path", ""))),
        method="POST",
        path=str(getattr(getattr(request, "url", None), "path", "") or "")[:255],
        status_code=status_code or 200,
        message=str(payload.get("message") or "前端上报：收到了非 JSON 响应")[:MAX_MESSAGE],
        detail=" · ".join(p for p in detail_parts if p),
        user_id=user_id,
        username=username,
        ip=ip,
        ua=str(request.headers.get("user-agent") or ""),
    )


def list_logs(
    session: Any,
    q: str = "",
    scope: str = "",
    kind: str = "",
    page: int = 1,
    size: int = 30,
) -> tuple[list[ErrorLog], int, int, int]:
    """按条件分页取异常日志，返回 (当前页记录, 总条数, 总页数, 当前页码)。"""
    conditions = []
    if scope:
        conditions.append(ErrorLog.scope == scope)
    if kind:
        conditions.append(ErrorLog.kind == kind)
    if q:
        like = f"%{q}%"
        conditions.append(
            ErrorLog.path.like(like)
            | ErrorLog.message.like(like)
            | ErrorLog.detail.like(like)
            | ErrorLog.username.like(like)
            | ErrorLog.exc_type.like(like)
        )
    total = int(session.scalar(select(func.count(ErrorLog.id)).where(*conditions)) or 0)
    pages = max((total + size - 1) // size, 1)
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    current = min(max(current, 1), pages)
    items = list(
        session.scalars(
            select(ErrorLog)
            .where(*conditions)
            .order_by(ErrorLog.id.desc())
            .offset((current - 1) * size)
            .limit(size)
        ).all()
    )
    return items, total, pages, current


def summary(session: Any) -> dict:
    """列表页顶部那排数字：近 24 小时 / 服务端 / 前端 / 各子系统。"""
    from datetime import timedelta

    recent = _now() - timedelta(hours=24)
    def _count(*conds) -> int:
        return int(session.scalar(select(func.count(ErrorLog.id)).where(*conds)) or 0)

    by_scope = {
        row[0]: int(row[1])
        for row in session.execute(
            select(ErrorLog.scope, func.count(ErrorLog.id)).group_by(ErrorLog.scope)
        ).all()
    }
    return {
        "total": _count(),
        "recent": _count(ErrorLog.created_at >= recent),
        "server": _count(ErrorLog.kind == "server"),
        "client": _count(ErrorLog.kind == "client"),
        "by_scope": by_scope,
    }


def prune(session: Any, days: int = 30) -> int:
    """清掉 N 天前的异常日志（页面上的「清理旧记录」按钮）。"""
    from datetime import timedelta

    cutoff = _now() - timedelta(days=max(1, int(days or 30)))
    rows = list(session.scalars(select(ErrorLog).where(ErrorLog.created_at < cutoff)).all())
    for row in rows:
        session.delete(row)
    session.commit()
    return len(rows)


__all__ = [
    "SCOPES", "KIND_LABELS",
    "scope_of", "scope_label", "kind_label", "wants_json",
    "record", "record_request", "record_client",
    "list_logs", "summary", "prune",
]
