"""重复订单识别：业务指纹比对 + 变更检测。

业务口径（2026-09）：
  群里可能把同一笔订单重复发一遍（误发 / 补发）。系统按「业务指纹」认出同一单：
  - 内容完全一致 -> 不重复归档、不重复推单、不重复播报，只回群提醒「已收到，内容无变化」；
  - 内容有变更   -> 按正常流程处理（更新订单字段 + 正常回执 + 播报）。

指纹 = 产品 / 规格 / 收货人 / 手机 / 地址 / 售价 / 成本 / 供应链 / 闲鱼号 / 回款人 的归一化哈希。
同一买家同款同价再来一单也会命中，可用 ORDER_DEDUPE_DAYS=0 关闭（只按订单号比对）。
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from ..config import settings
from ..models import Order
from ..timeutil import now

logger = logging.getLogger(__name__)

# 参与指纹与变更比对的字段：解析字段 key -> (订单属性, 页面名称)
FIELD_MAP: dict[str, tuple[str, str]] = {
    "product_name": ("product_name", "产品"),
    "spec": ("spec", "规格"),
    "receiver_name": ("receiver_name", "收货人"),
    "receiver_phone": ("receiver_phone", "手机号"),
    "receiver_address": ("receiver_address", "地址"),
    "sale_amount": ("sale_amount", "售价"),
    "cost_amount": ("cost_amount", "成本"),
    "supplier_name": ("supplier_name", "供应链"),
    "xianyu_account": ("xianyu_account", "出货闲鱼号"),
    "payee_name": ("payee_name", "回款归属"),
}

_NOISE_RE = re.compile(r"[\s\-_·•、,，。.:：/\\()（）\[\]【】\"'`]")


def norm_text(value: Any) -> str:
    """归一化文本：去空白与常见分隔符、转小写（用于指纹与比对）。"""
    return _NOISE_RE.sub("", str(value or "")).lower()


def norm_digits(value: Any) -> str:
    """只保留数字（手机号比对用）。"""
    return re.sub(r"\D", "", str(value or ""))


def norm_money(value: Any) -> str:
    """金额归一化：两位小数字符串。"""
    try:
        return f"{float(str(value or 0)):.2f}"
    except (TypeError, ValueError):
        return "0.00"


# 判定「是不是同一笔订单」的指纹：产品 + 收货人 + 手机号。
# 金额/地址这些可能变更的字段不参与，否则「重复发但改了售价」就认不出是同一单了。
IDENTITY_KEYS = ("product_name", "receiver_name", "receiver_phone")

# 宽松认单（手机号被改）时，最多回头扫多少条近期订单
_NEAR_SCAN_LIMIT = 300


def identity_fingerprint(fields: dict | None = None, order: Order | None = None) -> str:
    """同一单的身份指纹：产品 + 收货人 + 手机号（缺手机号时靠产品 + 收货人）。"""
    if order is not None:
        values = {key: getattr(order, key, "") for key in IDENTITY_KEYS}
    else:
        values = dict(fields or {})
    parts: list[str] = []
    for key in IDENTITY_KEYS:
        raw = values.get(key, "")
        parts.append(norm_digits(raw) if key == "receiver_phone" else norm_text(raw))
    if not parts[0] and not parts[1]:
        return ""  # 连产品和收货人都没有，无法判定身份
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


def payload_fingerprint(fields: dict | None = None, order: Order | None = None) -> str:
    """算订单全字段指纹（fields 为解析结果，order 可直接传订单对象）。"""
    if order is not None:
        values = {key: getattr(order, attr) for key, (attr, _label) in FIELD_MAP.items()}
    else:
        values = dict(fields or {})
    parts: list[str] = []
    for key, (_attr, _label) in FIELD_MAP.items():
        raw = values.get(key, "")
        if key == "receiver_phone":
            parts.append(norm_digits(raw))
        elif key in ("sale_amount", "cost_amount"):
            parts.append(norm_money(raw))
        else:
            parts.append(norm_text(raw))
    seed = "|".join(parts)
    if not seed.replace("|", ""):
        return ""
    return hashlib.md5(seed.encode("utf-8")).hexdigest()


def order_fingerprint(order: Order) -> str:
    return identity_fingerprint(order=order)


def find_duplicate(
    session,
    platform: str,
    order_no: str = "",
    fingerprint: str = "",
    days: int | None = None,
    exclude_id: int | None = None,
) -> Order | None:
    """找「同一笔订单」：先按订单号，再按身份指纹。

    days=0 表示关闭指纹比对（只按订单号认单）。
    """
    order_no = (order_no or "").strip()
    if order_no:
        row = session.scalar(
            select(Order).where(Order.platform == platform, Order.order_no == order_no)
        )
        if row is not None and (not exclude_id or row.id != exclude_id):
            return row
    days = settings.order_dedupe_days if days is None else days
    if not fingerprint or days == 0:
        return None
    stmt = select(Order).where(Order.fingerprint == fingerprint)
    if days and days > 0:
        stmt = stmt.where(Order.created_at >= now() - timedelta(days=days))
    stmt = stmt.order_by(Order.id.desc())
    for row in session.scalars(stmt).all():
        if exclude_id and row.id == exclude_id:
            continue
        return row
    return None


def find_by_identity_change(
    session,
    platform: str,
    fields: dict | None,
    days: int | None = None,
    exclude_id: int | None = None,
) -> Order | None:
    """宽松认单：产品和收货人都一致、只有手机号变了 -> 判为「同一笔订单改了手机号」。

    背景（2026-10-05 线上）：群里先发了订单，随后有人**回复那条消息**说
    「这个改一下手机号码」，再贴一遍完整订单（只有手机号从 138… 改成 139…）。
    身份指纹含手机号（IDENTITY_KEYS），改了号就认不出是同一单 -> 白建第二笔订单，
    运营还得手工合并（而手工合并那条路当时还会 500）。

    这里在「精确指纹没命中」之后兜底：只认**产品 + 收货人**一致、且**手机号确实不同**
    的近期订单（同号的走精确指纹那条路）。手机号差异交给 diff_fields 当「变更」写回原单。

    仍然保留「同买家同款两笔」的防护：调用方命中后照旧过
    ingest._looks_like_separate_order / _is_independent_resend，
    截图内容或订单编号对不上（说明真是两笔单）就拆成独立订单。
    """
    values = dict(fields or {})
    product = norm_text(values.get("product_name"))
    receiver = norm_text(values.get("receiver_name"))
    if not product or not receiver:
        return None  # 连产品 + 收货人都没有，不敢认单
    days = settings.order_dedupe_days if days is None else days
    if days == 0:
        return None  # 运营把指纹认单关了（ORDER_DEDUPE_DAYS=0），只按订单号认单
    phone = norm_digits(values.get("receiver_phone"))
    stmt = select(Order).where(Order.platform == platform)
    if days and days > 0:
        stmt = stmt.where(Order.created_at >= now() - timedelta(days=days))
    stmt = stmt.order_by(Order.id.desc()).limit(_NEAR_SCAN_LIMIT)
    for row in session.scalars(stmt).all():
        if exclude_id and row.id == exclude_id:
            continue
        if norm_text(row.product_name) != product or norm_text(row.receiver_name) != receiver:
            continue
        if phone and norm_digits(row.receiver_phone) == phone:
            continue  # 手机号一样：该由精确指纹命中，这里不抢
        return row
    return None


def diff_fields(order: Order, fields: dict) -> list[dict]:
    """比对解析结果和订单当前值，返回变更列表（空列表 = 内容无变化）。"""
    changes: list[dict] = []
    for key, (attr, label) in FIELD_MAP.items():
        if key not in fields:
            continue
        new_raw = fields.get(key)
        old_raw = getattr(order, attr, "")
        if key == "receiver_phone":
            new_v, old_v = norm_digits(new_raw), norm_digits(old_raw)
        elif key in ("sale_amount", "cost_amount"):
            new_v, old_v = norm_money(new_raw), norm_money(old_raw)
        else:
            new_v, old_v = norm_text(new_raw), norm_text(old_raw)
        if not new_v or new_v == old_v:
            continue
        # 老订单该字段本来是空的 -> 属于补全，不算「内容变更」
        if not old_v:
            continue
        changes.append(
            {
                "field": attr,
                "label": label,
                "old": str(old_raw if old_raw not in (None, "") else ""),
                "new": str(new_raw if new_raw not in (None, "") else ""),
            }
        )
    return changes


def apply_changes(order: Order, changes: list[dict], fields: dict) -> list[str]:
    """把变更应用到订单上（同时把新解析里的空值字段补全），返回变更描述。"""
    texts: list[str] = []
    for change in changes:
        attr = change["field"]
        if attr in ("sale_amount", "cost_amount"):
            try:
                value = round(float(change["new"]), 2)
            except (TypeError, ValueError):
                continue
            # 成本看着不对（比售价还高）就不写：标「待核对」等人工确认，见 services/cost_guard
            if attr == "cost_amount":
                from .cost_guard import guard_cost

                ok, _reason = guard_cost(None, order, value, source="去重合并")
                if not ok:
                    continue
            setattr(order, attr, value)
        else:
            setattr(order, attr, str(change["new"]).strip())
        texts.append(f"{change['label']}：{change['old']} → {change['new']}")
    # 只补全空字段，不覆盖已有信息
    for key, (attr, _label) in FIELD_MAP.items():
        value = fields.get(key)
        if value in (None, "", 0):
            continue
        if getattr(order, attr, "") in (None, "", 0):
            if attr == "cost_amount":
                # 补空同样要过护栏：成本比售价还高就留空等人工，见 services/cost_guard
                from .cost_guard import guard_cost

                ok, _reason = guard_cost(None, order, value, source="去重合并补空")
                if not ok:
                    continue
            setattr(order, attr, value)
    return texts
