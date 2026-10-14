"""操作日志：后台写操作统一留痕，便于事后追查问题。"""
from __future__ import annotations

import re
from urllib.parse import parse_qs

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import OperationLog, User

# 路径 -> 动作标签（按 method + 正则匹配，第一个命中生效）
ACTION_RULES: list[tuple[str, str, str]] = [
    ("POST", r"^/orders/(\d+)/edit$", "编辑订单"),
    ("POST", r"^/orders/(\d+)/delete$", "删除订单"),
    ("POST", r"^/orders/(\d+)/reparse$", "重新解析订单"),
    ("POST", r"^/orders/(\d+)/screenshot$", "上传订单截图"),
    ("POST", r"^/orders/(\d+)/push$", "推单给供应商"),
    ("POST", r"^/orders/(\d+)/refund$", "退单拦截处理"),
    ("POST", r"^/orders/batch-create$", "生成推单批次"),
    ("POST", r"^/orders/mark-reviewed$", "标记订单已确认"),
    ("POST", r"^/orders/auto-batch$", "按供应商自动组批"),
    ("POST", r"^/batches/(\d+)/status$", "更新批次状态"),
    ("POST", r"^/batches/(\d+)/items/(\d+)$", "回填供应商单号"),
    ("POST", r"^/suppliers$", "新增供应商"),
    ("POST", r"^/suppliers/(\d+)/edit$", "编辑供应商"),
    ("POST", r"^/suppliers/(\d+)/delete$", "删除供应商"),
    ("POST", r"^/users$", "新增账号"),
    ("POST", r"^/users/(\d+)/edit$", "修改账号"),
    ("POST", r"^/users/(\d+)/password$", "重置账号密码"),
    ("POST", r"^/users/(\d+)/status$", "启用/停用账号"),
    ("POST", r"^/users/(\d+)/delete$", "删除账号"),
    ("POST", r"^/rules$", "保存解析规则"),
    ("POST", r"^/rules/reset$", "恢复默认规则"),
    ("POST", r"^/settings/save$", "保存飞书配置"),
    ("POST", r"^/settings/notify$", "保存回执策略"),
    ("POST", r"^/settings/notify/flush$", "补发挂起回执"),
    ("POST", r"^/settings/chats$", "添加同步群"),
    ("POST", r"^/settings/chats/([^/]+)/delete$", "移除同步群"),
    ("POST", r"^/messages/sync$", "同步群消息"),
    ("POST", r"^/messages/reparse$", "重解析消息"),
    ("POST", r"^/messages/([^/]+)/notify$", "补发飞书回执"),
    ("POST", r"^/api/sync$", "同步群消息(API)"),
]

# 记进 detail 的表单字段（密码类字段一律不记录）
DETAIL_KEYS: tuple[str, ...] = (
    "name", "username", "role", "status", "display_name",
    "order_no", "supplier_name", "supplier_status", "order_status", "quantity",
    "sale_amount", "cost_amount", "shipping_fee", "platform_fee", "needs_review",
    "city", "category", "contact_name", "contact_info", "group_name", "has_group",
    "batch_no", "supplier_order_no", "tracking_no", "chat_id", "days",
    "transfer_status", "remark",
    "notify_mode", "notify_quiet_action", "notify_active_hours", "notify_min_interval_minutes",
    "notify_max_per_hour", "notify_utc_offset", "message_id", "yaml_text",
)
SKIP_KEYS = {"password", "new_password", "confirm_password", "feishu_app_secret"}
MAX_DETAIL = 500


def describe(method: str, path: str) -> tuple[str, str]:
    """把请求翻译成 (动作, 目标)；没匹配上返回空串（无需记日志）。"""
    for rule_method, pattern, label in ACTION_RULES:
        if rule_method != method:
            continue
        m = re.match(pattern, path)
        if m:
            return label, " ".join(g for g in m.groups() if g)
    return "", ""


def detail_from_body(content_type: str, body: bytes) -> str:
    """从表单体里挑关键字段作为日志明细（密码类字段一律跳过）。

    只解析 urlencoded 表单；multipart 只记动作与目标，不展开内容。
    """
    if not body or "application/x-www-form-urlencoded" not in (content_type or ""):
        return ""
    try:
        parsed = parse_qs(body.decode("utf-8", "ignore"), keep_blank_values=False)
    except Exception:  # noqa: BLE001
        return ""
    parts: list[str] = []
    for key in DETAIL_KEYS:
        if key in SKIP_KEYS or key not in parsed:
            continue
        values = [v.strip() for v in parsed[key] if v.strip()]
        if not values:
            continue
        if key == "yaml_text":
            parts.append(f"规则 {sum(len(v) for v in values)} 字符")
        elif len(values) > 3:
            parts.append(f"{key}={len(values)} 项")
        else:
            parts.append(f"{key}={'，'.join(v[:60] for v in values)}")
    # 批量勾选的订单：只记条数
    ids = parsed.get("order_ids") or []
    if ids:
        parts.append(f"订单 {len(ids)} 条")
    return " · ".join(parts)[:MAX_DETAIL]


def client_ip(request: Request | None) -> str:
    if request is None:
        return ""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def log_action(
    session: Session,
    user: User | None,
    action: str,
    target: str = "",
    detail: str = "",
    request: Request | None = None,
) -> OperationLog:
    row = OperationLog(
        user_id=user.id if user else None,
        username=user.username if user else "",
        role=user.role if user else "",
        action=action,
        target=str(target or "")[:128],
        detail=str(detail or "")[:MAX_DETAIL],
        method=request.method if request else "",
        path=request.url.path if request else "",
        ip=client_ip(request),
    )
    session.add(row)
    session.commit()
    return row


def list_logs(
    session: Session,
    q: str = "",
    username: str = "",
    action: str = "",
    page: int = 1,
    size: int = 30,
) -> tuple[list[OperationLog], int, int, int]:
    """按条件分页取操作日志，返回 (当前页记录, 总条数, 总页数, 当前页码)。

    页码越界一律夹到 1..pages（地址栏手改 page=9999 时直接回到最后一页，而不是空列表）。
    总数与明细用同一组条件分别统计：老写法 `select_from(子查询)` 会和外层表做笛卡尔积，
    把总条数放大成「本页条数 × 全表条数」，页数虚高（几百页里翻不到东西）。
    """
    conditions = []
    if username:
        conditions.append(OperationLog.username == username)
    if action:
        conditions.append(OperationLog.action == action)
    if q:
        like = f"%{q}%"
        conditions.append(
            OperationLog.action.like(like)
            | OperationLog.target.like(like)
            | OperationLog.detail.like(like)
            | OperationLog.username.like(like)
        )
    total = int(session.scalar(select(func.count(OperationLog.id)).where(*conditions)) or 0)
    pages = max((total + size - 1) // size, 1)
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    current = min(max(current, 1), pages)
    items = list(
        session.scalars(
            select(OperationLog)
            .where(*conditions)
            .order_by(OperationLog.id.desc())
            .offset((current - 1) * size)
            .limit(size)
        ).all()
    )
    return items, total, pages, current


def distinct_actions(session: Session) -> list[str]:
    return [
        r[0]
        for r in session.execute(
            select(OperationLog.action)
            .where(OperationLog.action != "")
            .group_by(OperationLog.action)
            .order_by(OperationLog.action)
        ).all()
    ]


def distinct_usernames(session: Session) -> list[str]:
    return [
        r[0]
        for r in session.execute(
            select(OperationLog.username)
            .where(OperationLog.username != "")
            .group_by(OperationLog.username)
            .order_by(OperationLog.username)
        ).all()
    ]
