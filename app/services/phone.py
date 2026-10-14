"""公司资源 · 手机号：一个号是一条资源，记归属（谁实名）、套餐怎么付、开出了哪些平台账号。

页面在「系统 · 公司资源 · 手机号管理」（app/templates/phone_numbers.html，
新建 / 编辑用 phone_form.html），模型见 app/models.py::PhoneNumber。

为什么落表：手机号开出来的闲鱼 / 支付宝 / 微信 / 小红书账号是能直接干活的资产，
「这个号是谁实名的、还能不能用、开了哪些平台」以前散在 Excel 和聊天记录里，
号停用或换实名之后对不上账。这里只管**资源台账本身**，不存平台账号的登录凭据
（密码 / Cookie 一律不入库）。
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    PHONE_CARD_CHOICES,
    PHONE_CARD_LABELS,
    PHONE_CARD_MAIN,
    PHONE_CARD_SUB,
    PHONE_CARRIER_CHOICES,
    PHONE_CARRIER_LABELS,
    PHONE_NATURE_CHOICES,
    PHONE_NATURE_LABELS,
    PHONE_NATURE_PERSONAL,
    PHONE_PLATFORM_FIELDS,
    PHONE_PLATFORM_LABELS,
    PHONE_PLATFORMS,
    PHONE_PLAN_CHOICES,
    PHONE_PLAN_LABELS,
    PHONE_PLAN_POSTPAID,
    PhoneNumber,
)

logger = logging.getLogger("order-hub.phone")

# 页面下拉用的候选（与模型里的取值一一对应；模型只存值，文案统一在这里）
NATURE_CHOICES = PHONE_NATURE_CHOICES
NATURE_TEXT = PHONE_NATURE_LABELS
CARD_CHOICES = PHONE_CARD_CHOICES
CARD_TEXT = PHONE_CARD_LABELS
PLAN_CHOICES = PHONE_PLAN_CHOICES
PLAN_TEXT = PHONE_PLAN_LABELS
CARRIER_CHOICES = PHONE_CARRIER_CHOICES
CARRIER_TEXT = PHONE_CARRIER_LABELS
PLATFORMS = PHONE_PLATFORMS
PLATFORM_FIELDS = PHONE_PLATFORM_FIELDS
PLATFORM_TEXT = PHONE_PLATFORM_LABELS

# 筛选状态：启用中 / 已停用 / 全部
STATUS_ENABLED = "enabled"
STATUS_DISABLED = "disabled"
STATUS_CHOICES = (STATUS_ENABLED, STATUS_DISABLED)
STATUS_TEXT = {STATUS_ENABLED: "启用中", STATUS_DISABLED: "已停用"}


# ---------------------------------------------------------------- 工具
def normalize_number(value: str | None) -> str:
    """手机号归一化：去掉空格 / 横线 / 括号与 +86 前缀，认 11 位 1 开头；不合法返回空串。

    运营是从聊天记录、Excel 里复制的，写法有「138 0000 0000」「+8613800000000」
    「138-0000-0000」好几种 —— 统一成一个样子，重复录入才拦得住（表上有唯一约束）。
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0086"):
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) > 11:
        digits = digits[2:]
    if len(digits) != 11 or not digits.startswith("1"):
        return ""
    return digits


def format_number(value: str | None) -> str:
    """展示用：138 0000 0000（存的是纯数字）。"""
    num = str(value or "").strip()
    if len(num) != 11:
        return num
    return f"{num[:3]} {num[3:7]} {num[7:]}"


def to_float(value, default: float = 0.0) -> float:
    raw = str(value or "").strip().replace("¥", "").replace(",", "")
    if not raw:
        return default
    try:
        return round(float(raw), 2)
    except ValueError:
        return default


def truthy(value) -> bool:
    """表单里的「是否」：勾了的 checkbox 会带值回来（on / 1 / true），没勾的不出现。"""
    return str(value or "").strip().lower() not in ("", "0", "false", "off", "no")


# ---------------------------------------------------------------- 读
def get_phone(session: Session, phone_id: int) -> PhoneNumber | None:
    return session.get(PhoneNumber, phone_id)


def _conds(
    *,
    q: str = "",
    nature: str = "",
    card_type: str = "",
    plan: str = "",
    carrier: str = "",
    status: str = "",
    platform: str = "",
    idle: str = "",
) -> list:
    """列表与统计共用同一份条件：顶部数字与下面的条数永远对得上。"""
    conds = []
    word = str(q or "").strip()
    if word:
        # 关键词认号码（归一化后模糊匹配，粘进来带空格也能搜到）、实名人、备注、主卡号
        digits = re.sub(r"\D", "", word)
        like = f"%{word}%"
        conds.append(
            or_(
                PhoneNumber.number.like(f"%{digits}%") if digits else PhoneNumber.number.like(like),
                PhoneNumber.owner_name.like(like),
                PhoneNumber.parent_number.like(like),
                PhoneNumber.note.like(like),
            )
        )
    if nature in NATURE_CHOICES:
        conds.append(PhoneNumber.nature == nature)
    if card_type in CARD_CHOICES:
        conds.append(PhoneNumber.card_type == card_type)
    if plan in PLAN_CHOICES:
        conds.append(PhoneNumber.plan == plan)
    if carrier in CARRIER_CHOICES:
        conds.append(PhoneNumber.carrier == carrier)
    if status == STATUS_ENABLED:
        conds.append(PhoneNumber.enabled.is_(True))
    elif status == STATUS_DISABLED:
        conds.append(PhoneNumber.enabled.is_(False))
    if platform in PLATFORM_FIELDS:
        conds.append(getattr(PhoneNumber, platform).is_(True))
    if truthy(idle):
        # 一个平台账号都没开的号：还没变成资产（运营按它排活）
        conds.append(and_(*[getattr(PhoneNumber, key).is_(False) for key in PLATFORM_FIELDS]))
    return conds


def list_phones(session: Session, **filters) -> list[PhoneNumber]:
    """列表：停用的排在后面（还在用的号在上面），同组内按录入顺序。"""
    stmt = select(PhoneNumber)
    for cond in _conds(**filters):
        stmt = stmt.where(cond)
    return list(
        session.scalars(
            stmt.order_by(PhoneNumber.enabled.desc(), PhoneNumber.id.desc())
        )
    )


def stats(session: Session, **filters) -> dict:
    """顶部统计：与列表同一个筛选口径（没筛就是全量）。

    「月度套餐支出」只算**启用中**的号（停用的号不再花钱，算进去会把成本算高）；
    预充值余额同理只算启用中的，停用号里的余额当作已作废。
    """
    conds = _conds(**filters)
    base = select(func.count()).select_from(PhoneNumber)

    def count(extra: list | None = None) -> int:
        stmt = base
        for cond in conds:
            stmt = stmt.where(cond)
        for cond in extra or []:
            stmt = stmt.where(cond)
        return int(session.scalar(stmt) or 0)

    def money_sum(column, extra: list | None = None) -> float:
        stmt = select(func.coalesce(func.sum(column), 0.0)).select_from(PhoneNumber)
        for cond in conds:
            stmt = stmt.where(cond)
        for cond in extra or []:
            stmt = stmt.where(cond)
        return round(float(session.scalar(stmt) or 0.0), 2)

    total = count()
    platforms = {key: count([getattr(PhoneNumber, key).is_(True)]) for key in PLATFORM_FIELDS}
    return {
        "total": total,
        "enabled": count([PhoneNumber.enabled.is_(True)]),
        "disabled": count([PhoneNumber.enabled.is_(False)]),
        "personal": count([PhoneNumber.nature == PHONE_NATURE_PERSONAL]),
        "enterprise": total - count([PhoneNumber.nature == PHONE_NATURE_PERSONAL]),
        "main": count([PhoneNumber.card_type == PHONE_CARD_MAIN]),
        "sub": count([PhoneNumber.card_type == PHONE_CARD_SUB]),
        "postpaid": count([PhoneNumber.plan == PHONE_PLAN_POSTPAID]),
        "prepaid": total - count([PhoneNumber.plan == PHONE_PLAN_POSTPAID]),
        # 月度套餐支出 / 预充值余额：只算启用中的号
        "monthly_cost": money_sum(PhoneNumber.monthly_fee, [PhoneNumber.enabled.is_(True)]),
        "balance": money_sum(PhoneNumber.balance, [PhoneNumber.enabled.is_(True)]),
        "platforms": platforms,
        # 一个平台账号都没开的号：还没变成资产，运营最该先处理它
        "no_platform": count(
            [getattr(PhoneNumber, key).is_(False) for key in PLATFORM_FIELDS]
        ),
    }


# ---------------------------------------------------------------- 写
def _number_taken(session: Session, number: str, exclude_id: int | None = None) -> bool:
    row = session.scalar(select(PhoneNumber).where(PhoneNumber.number == number))
    return bool(row and row.id != exclude_id)


def create_phone(session: Session, form: dict, *, commit: bool = True) -> tuple[PhoneNumber | None, str]:
    """新增手机号资源；返回 (记录, 错误文案)。"""
    number = normalize_number(form.get("number"))
    if not number:
        return None, "手机号要填 11 位（138 0000 0000 这种写法也认）"
    if _number_taken(session, number):
        return None, f"手机号 {format_number(number)} 已经在库里了，别重复录"
    owner = str(form.get("owner_name") or "").strip()
    if not owner:
        return None, "所属人（实名人）不能为空：这是资源的归属，出了事要找得到人"

    nature = str(form.get("nature") or PHONE_NATURE_PERSONAL).strip()
    if nature not in NATURE_CHOICES:
        nature = PHONE_NATURE_PERSONAL
    card_type = str(form.get("card_type") or PHONE_CARD_MAIN).strip()
    if card_type not in CARD_CHOICES:
        card_type = PHONE_CARD_MAIN
    plan = str(form.get("plan") or PHONE_PLAN_POSTPAID).strip()
    if plan not in PLAN_CHOICES:
        plan = PHONE_PLAN_POSTPAID
    carrier = str(form.get("carrier") or "").strip()
    if carrier not in CARRIER_CHOICES:
        carrier = ""

    phone = PhoneNumber(
        number=number,
        carrier=carrier,
        nature=nature,
        owner_name=owner[:64],
        card_type=card_type,
        parent_number=normalize_number(form.get("parent_number")),
        plan=plan,
        monthly_fee=to_float(form.get("monthly_fee")),
        balance=to_float(form.get("balance")),
        note=str(form.get("note") or "").strip(),
        enabled=truthy(form.get("enabled")) if "enabled" in form else True,
    )
    for key in PLATFORM_FIELDS:
        setattr(phone, key, truthy(form.get(key)))
    session.add(phone)
    session.flush()
    if commit:
        session.commit()
    return phone, ""


def update_phone(
    session: Session, phone: PhoneNumber, form: dict, *, commit: bool = True
) -> tuple[PhoneNumber | None, str]:
    """按表单更新（表单里带哪些字段就改哪些）；返回 (记录, 错误文案)。"""
    if "number" in form:
        number = normalize_number(form.get("number"))
        if not number:
            return None, "手机号要填 11 位（138 0000 0000 这种写法也认）"
        if _number_taken(session, number, exclude_id=phone.id):
            return None, f"手机号 {format_number(number)} 已经被别的记录用了"
        phone.number = number
    if "owner_name" in form:
        owner = str(form.get("owner_name") or "").strip()
        if not owner:
            return None, "所属人（实名人）不能为空"
        phone.owner_name = owner[:64]
    if "carrier" in form:
        carrier = str(form.get("carrier") or "").strip()
        phone.carrier = carrier if carrier in CARRIER_CHOICES else ""
    if "nature" in form:
        nature = str(form.get("nature") or "").strip()
        if nature in NATURE_CHOICES:
            phone.nature = nature
    if "card_type" in form:
        card_type = str(form.get("card_type") or "").strip()
        if card_type in CARD_CHOICES:
            phone.card_type = card_type
    if "parent_number" in form:
        phone.parent_number = normalize_number(form.get("parent_number"))
    if "plan" in form:
        plan = str(form.get("plan") or "").strip()
        if plan in PLAN_CHOICES:
            phone.plan = plan
    if "monthly_fee" in form:
        phone.monthly_fee = to_float(form.get("monthly_fee"))
    if "balance" in form:
        phone.balance = to_float(form.get("balance"))
    if "note" in form:
        phone.note = str(form.get("note") or "").strip()
    if "enabled" in form:
        phone.enabled = truthy(form.get("enabled"))
    # 「是否注册」四个勾：表单里没带回来就是没勾（checkbox 不勾不提交），按 False 处理
    for key in PLATFORM_FIELDS:
        if key in form:
            setattr(phone, key, truthy(form.get(key)))
    if commit:
        session.commit()
    return phone, ""


def toggle_phone(session: Session, phone: PhoneNumber, *, commit: bool = True) -> PhoneNumber:
    """停用 / 重新启用：停用的号不删记录（它可能开过账号，删了就查不到是谁开的）。"""
    phone.enabled = not bool(phone.enabled)
    if commit:
        session.commit()
    return phone


def delete_phone(session: Session, phone: PhoneNumber, *, commit: bool = True) -> None:
    session.delete(phone)
    if commit:
        session.commit()


# ---------------------------------------------------------------- 展示
def rows(session: Session, **filters) -> list[dict]:
    """列表页的行数据：补上文案与「已开账号」那一行，模板不用再拼。"""
    out = []
    for phone in list_phones(session, **filters):
        out.append(
            {
                "phone": phone,
                "number_text": format_number(phone.number),
                "nature_text": phone.nature_text,
                "card_text": phone.card_text,
                "plan_text": phone.plan_text,
                "carrier_text": phone.carrier_text,
                "platform_text": "、".join(phone.registered_platforms) or "未开账号",
                "registered_count": len(phone.registered_platforms),
            }
        )
    return out


__all__ = [
    "NATURE_CHOICES",
    "NATURE_TEXT",
    "CARD_CHOICES",
    "CARD_TEXT",
    "PLAN_CHOICES",
    "PLAN_TEXT",
    "CARRIER_CHOICES",
    "CARRIER_TEXT",
    "PLATFORMS",
    "PLATFORM_FIELDS",
    "PLATFORM_TEXT",
    "STATUS_CHOICES",
    "STATUS_TEXT",
    "normalize_number",
    "format_number",
    "get_phone",
    "list_phones",
    "stats",
    "rows",
    "create_phone",
    "update_phone",
    "toggle_phone",
    "delete_phone",
]
