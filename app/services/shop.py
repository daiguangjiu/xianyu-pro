"""店铺档案（闲鱼自营账号）：台账基础信息 + 历史订单补充的经营信息。

业务口径（2026-09）：
  · 店铺 = 闲鱼自营账号，基础信息来自运营的「闲鱼自营账号台账」
    （账号唯一编号 / 账号名称 / 账号运营人 / 设备 / 类目 / 支付宝 / 信用分…）；
  · 账号名称对应订单的「出货闲鱼号」``xianyu_account``，账号运营人对应飞书发送人
    ``sender_name`` —— 订单就是靠这两个字段认店的；
  · **店铺不存在就自动建档**：群里出单、登记表导入、订单页维护时，账号在店铺管理里
    没有的话自动补一条（来源记在 note 里），运营后面再补资料；
  · **按历史数据补信息**：订单里出现过的回款归属人、主营供应链等会回填到档案里，
    订单数 / 销售额 / 毛利 / 最近出单时间按订单实时统计（见 shop_stat / backfill）。
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    SELF_RUN_ROLE_NAME,
    SHOP_AUDIT_APPROVED,
    SHOP_AUDIT_CHOICES,
    SHOP_AUDIT_PENDING,
    SHOP_STATUS_RUNNING,
    SERVICE_FEE_RATE_DEFAULT,
    SERVICE_FEE_RATE_MAX,
    SERVICE_FEE_RATE_MIN,
    SHOP_NATURE_CHOICES,
    SHOP_NATURE_DEFAULT,
    SHOP_NATURE_STUDENT,
    SHOP_TYPE_CHOICES,
    SHOP_TYPE_DEFAULT,
    Order,
    Role,
    Shop,
    User,
)
from ..timeutil import now

logger = logging.getLogger(__name__)

# 账号唯一编号：XYSK + 4 位序号（台账里的写法，自动建档时接着往后编）
CODE_PREFIX = "XYSK"
CODE_WIDTH = 4

# 台账表头别名：列名换说法也能认（键=系统字段，值=可能出现的列名）
_LEDGER_ALIASES: dict[str, tuple[str, ...]] = {
    "code": ("账号唯一编号", "唯一编号", "账号编号", "编号"),
    "name": ("账号名称", "闲鱼账号", "账号", "店铺名称", "店铺"),
    "operator": ("账号运营人", "运营人", "运营人员", "运营"),
    "owner": ("账号所属人", "所属人", "挂靠人"),
    "device": ("绑定手机设备", "手机设备", "设备", "绑定设备"),
    "category": ("经营细分类目", "经营类目", "细分类目", "类目"),
    "phone": ("绑定手机号", "手机号", "绑定手机"),
    "status": ("账号状态", "状态"),
    "supply": ("类目/供应链", "供应链", "主营供应链"),
    "alipay": ("支付宝绑定账号", "支付宝账号", "支付宝"),
    "credit_score": ("芝麻信用分", "信用分", "芝麻分"),
    "real_name": ("实名认证", "实名", "认证人"),
    "xianqi": ("闲气值",),
    "yuli": ("鱼力值",),
    "violation": ("有无违规", "违规"),
    "wechat": ("微信账号", "微信号"),
    "wechat_password": ("微信密码",),
    "wechat_pay_password": ("微信支付密码",),
    "account_age": ("新老账号区分", "新老账号", "账号区分"),
    "note": ("描述", "备注", "说明"),
    # 店铺性质 / 所属带教（2026-10-11 学员场景）：台账里也能带这两列
    "shop_nature": ("店铺性质", "店铺归属", "性质"),
    "coach": ("所属带教", "带教老师", "带教"),
}

_TEXT_FIELDS = (
    "code", "name", "operator", "owner", "device", "category", "phone", "status",
    "supply", "alipay", "credit_score", "real_name", "xianqi", "yuli",
    "violation", "wechat", "wechat_password", "wechat_pay_password",
    "account_age", "note", "payee_name",
    # 学员在渔货栈建档时填的：闲鱼会员名、联系电话（本人联系用，区别于闲鱼绑定手机号）
    "member_name", "contact_phone",
    # 所属带教：学员店铺才有（店铺性质走 _apply_shop_nature，不在这里）
    "coach",
)

# 实名认证是否完成：表单里是勾选框，值可能是 on / 1 / true / yes
_TRUTHY = {"1", "on", "true", "yes", "y", "是", "已完成"}


def _clean(value: object) -> str:
    """单元格 -> 字符串（换行压成空格，去空白）。"""
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def _map_columns(header: list[str]) -> dict[str, int]:
    """台账表头 -> {系统字段: 列下标}。

    先精确匹配再前缀匹配（前缀要求别名至少 3 个字）：否则「账号运营人」会被
    「账号」这类短别名抢走，账号名称与运营人就串了位。
    """
    mapping: dict[str, int] = {}
    keys = [_clean(raw) for raw in header]
    for idx, key in enumerate(keys):
        if not key:
            continue
        for field, aliases in _LEDGER_ALIASES.items():
            if field in mapping:
                continue
            if key in aliases:
                mapping[field] = idx
                break
    for idx, key in enumerate(keys):
        if not key:
            continue
        for field, aliases in _LEDGER_ALIASES.items():
            if field in mapping:
                continue
            if any(len(a) >= 3 and key.startswith(a) for a in aliases):
                mapping[field] = idx
                break
    return mapping


def read_ledger(path: str | Path, sheet: str = "") -> tuple[list[dict], str]:
    """读自营账号台账，返回 (记录列表, 实际用的表名)；跳过没有账号名称的空行。"""
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True, read_only=True)
    name = sheet or (wb.sheetnames[0] if wb.sheetnames else "")
    ws = wb[name] if name in wb.sheetnames else wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return [], name

    mapping = _map_columns([_clean(c) for c in rows[0]])
    if "name" not in mapping:
        return [], name

    out: list[dict] = []
    for row in rows[1:]:
        if row is None or not any(c is not None and str(c).strip() for c in row):
            continue

        def cell(field: str) -> object:
            idx = mapping.get(field)
            return row[idx] if idx is not None and idx < len(row) else None

        account = _clean(cell("name"))
        if not account:
            # 台账里预留的编号行（只有编号没有账号名）不建档
            continue
        out.append({field: _clean(cell(field)) for field in _TEXT_FIELDS})
    return out, name


# --------------------------------------------------------------------- 档案
def list_shops(
    session: Session,
    q: str = "",
    operator: str = "",
    status: str = "",
    audit: str = "",
    shop_type: str = "",
    owner_user_id: int | None = None,
    nature: str = "",
) -> list[Shop]:
    stmt = select(Shop)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            Shop.name.like(like)
            | Shop.code.like(like)
            | Shop.operator.like(like)
            | Shop.owner.like(like)
            | Shop.category.like(like)
            | Shop.supply.like(like)
            | Shop.phone.like(like)
            | Shop.alipay.like(like)
        )
    if operator:
        stmt = stmt.where(Shop.operator == operator)
    if status:
        stmt = stmt.where(Shop.status == status)
    if audit:
        stmt = stmt.where(Shop.audit_status == audit)
    if shop_type:
        stmt = stmt.where(Shop.shop_type == shop_type)
    if owner_user_id is not None:
        # 渔货栈账号能看自己提交的 + 从渔枢系统带入（已关联）的店铺
        stmt = stmt.where(_mine_clause(owner_user_id))
    if nature:
        # 店铺性质（自营 / 学员）：空值（老数据补列前建的行）按「自营店铺」算
        stmt = stmt.where(
            (Shop.shop_nature == nature)
            if nature != SHOP_NATURE_DEFAULT
            else or_(Shop.shop_nature == SHOP_NATURE_DEFAULT, Shop.shop_nature.is_(None), Shop.shop_nature == "")
        )
    return list(
        session.scalars(
            stmt.order_by(
                case_status(),
                Shop.code.asc(),
                Shop.name.asc(),
            )
        ).all()
    )


def _mine_clause(owner_user_id: int):
    """「我的店铺」的口径：自己提交的（owner_user_id）+ 从渔枢系统带入的（self_linked_user_id）。

    两者的区别：认领渔枢端无主档案时归属也转过来，两个字段都有值；
    只同步不改归属（自营账号组运营人名下的店）时只记 self_linked_user_id。
    """
    return or_(
        Shop.owner_user_id == owner_user_id,
        Shop.self_linked_user_id == owner_user_id,
    )


def case_status():
    """正常运营的排前面，其余按状态排。"""
    from sqlalchemy import case

    return case((Shop.status == SHOP_STATUS_RUNNING, 0), else_=1)


def get_shop(session: Session, shop_id: int) -> Shop | None:
    return session.get(Shop, shop_id)


def find_by_name(session: Session, name: str) -> Shop | None:
    """按账号名称找店铺（忽略前后空格与大小写）。"""
    text = (name or "").strip()
    if not text:
        return None
    row = session.scalar(select(Shop).where(Shop.name == text))
    if row is not None:
        return row
    return session.scalar(select(Shop).where(func.lower(Shop.name) == text.lower()))


# -------------------------------------------- 订单侧：按出货闲鱼号看店铺性质
def nature_of_accounts(session: Session) -> dict[str, dict]:
    """出货闲鱼号 -> 店铺性质信息，供订单列表 / 详情按行打「自营订单 / 学员订单」标。

    返回 ``{账号名: {"nature": 性质, "coach": 带教}}``。订单里出现、但店铺档案里没有的
    账号不在这里 —— 页面上按默认「自营店铺」显示（与店铺档案的默认口径一致）。
    """
    rows = session.execute(
        select(Shop.name, Shop.shop_nature, Shop.coach).where(Shop.name != "")
    ).all()
    out: dict[str, dict] = {}
    for name, nature, coach in rows:
        value = (nature or "").strip()
        out[str(name)] = {
            "nature": value if value in SHOP_NATURE_CHOICES else SHOP_NATURE_DEFAULT,
            "coach": (coach or "").strip(),
        }
    return out


def student_shop_names(session: Session) -> list[str]:
    """学员店铺的出货闲鱼号名单（订单按「订单性质」筛选时用）。"""
    rows = session.scalars(
        select(Shop.name).where(Shop.shop_nature == SHOP_NATURE_STUDENT, Shop.name != "")
    ).all()
    return [str(n) for n in rows if (n or "").strip()]


def account_nature(accounts: dict[str, dict], account: str) -> str:
    """某个出货闲鱼号的订单性质：查不到档案按「自营店铺」算（默认口径）。"""
    info = accounts.get((account or "").strip()) or {}
    return info.get("nature") or SHOP_NATURE_DEFAULT


def _next_code(session: Session) -> str:
    """自动建档时接着台账编号往后编（XYSK0001…）。"""
    used = {
        (x or "").strip().upper()
        for x in session.scalars(select(Shop.code)).all()
        if (x or "").strip()
    }
    seq = 1
    while f"{CODE_PREFIX}{seq:0{CODE_WIDTH}d}" in used:
        seq += 1
    return f"{CODE_PREFIX}{seq:0{CODE_WIDTH}d}"


def create_shop(
    session: Session,
    data,
    source: str = "",
    *,
    owner_user_id: int = 0,
    audit_status: str = SHOP_AUDIT_APPROVED,
) -> Shop:
    """新增店铺档案。

    `owner_user_id` / `audit_status` 供渔货栈账号自助提交用：记下提交人，状态先给「待审核」；
    渔枢系统端自己建档不传，默认无归属 + 已通过（台账本来就有人维护，不用自己审自己）。
    """
    name = _clean(data.get("name") if hasattr(data, "get") else "")
    if not name:
        raise ValueError("账号名称不能为空")
    if find_by_name(session, name):
        raise ValueError(f"店铺「{name}」已存在")
    row = Shop(name=name, owner_user_id=owner_user_id, audit_status=audit_status)
    _apply_fields(row, data)
    _apply_real_name_verified(row, data)
    _apply_shop_type(row, data)
    _apply_shop_nature(row, data)
    _apply_service_fee_rate(row, data)
    if not row.shop_type:  # 没选（接口 / 自动建档）就按默认：个人闲置店铺
        row.shop_type = SHOP_TYPE_DEFAULT
    if not row.shop_nature:  # 没选就按默认：自营店铺（学员店铺只由学员模板自动标）
        row.shop_nature = SHOP_NATURE_DEFAULT
    if not row.service_fee_rate:  # 没填就按默认 0.6%（历史口径）
        row.service_fee_rate = SERVICE_FEE_RATE_DEFAULT
    if not row.code:
        row.code = _next_code(session)
    if not row.status:
        row.status = SHOP_STATUS_RUNNING
    if source:
        row.note = (f"{row.note}\n{source}" if row.note else source)[:2000]
    session.add(row)
    session.flush()
    session.commit()
    _notify_self_sync(row.id)  # 建档即同步：对应的人进渔货栈就能看到，不用再手填一遍
    return row


def update_shop(
    session: Session, shop_id: int, data, *, audit_status: str | None = None
) -> Shop:
    row = session.get(Shop, shop_id)
    if row is None:
        raise ValueError("店铺不存在")
    name = _clean(data.get("name") if hasattr(data, "get") else "")
    if not name:
        raise ValueError("账号名称不能为空")
    dup = find_by_name(session, name)
    if dup is not None and dup.id != shop_id:
        raise ValueError(f"店铺「{name}」已存在")
    _apply_fields(row, data)
    _apply_real_name_verified(row, data)
    _apply_shop_type(row, data)
    _apply_shop_nature(row, data)
    _apply_service_fee_rate(row, data)
    row.name = name
    if audit_status is not None:
        row.audit_status = audit_status
    row.updated_at = now()
    session.flush()
    session.commit()
    if _touches_self_sync(data):  # 改了运营人 / 手机号才需要重新认一遍归属
        _notify_self_sync(shop_id)
    return row


def set_audit(session: Session, shop_id: int, status: str, note: str = "") -> Shop:
    """渔枢系统端审核店铺：通过 / 驳回（驳回原因记在 audit_note）。"""
    if status not in SHOP_AUDIT_CHOICES:
        raise ValueError("审核状态不合法")
    row = session.get(Shop, shop_id)
    if row is None:
        raise ValueError("店铺不存在")
    row.audit_status = status
    # 通过时清掉上一次的驳回原因，驳回时记下原因
    row.audit_note = "" if status == SHOP_AUDIT_APPROVED else _clean(note)[:255]
    row.updated_at = now()
    session.flush()
    session.commit()
    return row


def count_shops(session: Session, *, owner_user_id: int | None = None, audit: str = "") -> int:
    """按口径数店铺：owner_user_id 传了就只数这个账号名下的（含带入的），再按审核状态过滤。"""
    stmt = select(func.count(Shop.id))
    if owner_user_id is not None:
        stmt = stmt.where(_mine_clause(owner_user_id))
    if audit:
        stmt = stmt.where(Shop.audit_status == audit)
    return session.scalar(stmt) or 0


def owned_shop(session: Session, shop_id: int, owner_user_id: int) -> Shop | None:
    """取自己能维护的店铺：自己提交的，或从渔枢系统带入（已关联）的。

    不是自己的一律当不存在（渔货栈不能碰别人的店）。
    """
    row = session.get(Shop, shop_id)
    if row is None:
        return None
    if row.owner_user_id != owner_user_id and row.self_linked_user_id != owner_user_id:
        return None
    return row


def find_self_run_member(session: Session, name: str) -> User | None:
    """按姓名找渔枢系统后台「自营账号组」里的成员（显示名 / 登录名都认）。

    学员在渔货栈填的「实际运营人姓名」对上组里的人时，组里那人名下的店铺档案
    可以直接同步过来 —— 不用学员再手填一遍，也不用等渔枢系统重新建档。
    """
    text = (name or "").strip()
    if not text:
        return None
    role_ids = [
        r.id
        for r in session.scalars(select(Role).where(Role.name == SELF_RUN_ROLE_NAME)).all()
    ]
    if not role_ids:
        return None
    rows = session.scalars(select(User).where(User.role_id.in_(role_ids))).all()
    low = text.lower()
    for row in rows:
        if (row.display_name or "").strip().lower() == low:
            return row
    for row in rows:
        if (row.username or "").strip().lower() == low:
            return row
    return None


def claim_shop(session: Session, shop: Shop, user_id: int) -> Shop:
    """认领渔枢系统里已有的店铺档案：归属转到当前渔货栈账号，并记「渔货栈店铺已关联」。

    只认领渔枢系统端建档、还没归属任何渔货栈账号的档案（owner_user_id = 0）；
    已经被别人领走的由调用方拦掉，这里不再判断。
    """
    shop.owner_user_id = user_id
    shop.self_linked_user_id = user_id
    shop.self_linked_at = now()
    shop.updated_at = now()
    session.flush()
    session.commit()
    return shop


def link_self_shop(session: Session, shop: Shop, user_id: int) -> Shop:
    """只把店铺「同步」给渔货栈账号：记关联，不改归属（档案仍挂在渔枢系统那边的账号名下）。"""
    shop.self_linked_user_id = user_id
    shop.self_linked_at = now()
    shop.updated_at = now()
    session.flush()
    session.commit()
    return shop


def linked_user_names(session: Session, rows: list[Shop]) -> dict[int, str]:
    """渔货栈已关联的店铺 → 关联账号的显示名（渔枢系统端列表要显示是谁关联的）。"""
    ids = {int(r.self_linked_user_id or 0) for r in rows if r.self_linked_user_id}
    ids.discard(0)
    if not ids:
        return {}
    out: dict[int, str] = {}
    for row in session.scalars(select(User).where(User.id.in_(ids))).all():
        out[row.id] = (row.display_name or "").strip() or row.username
    return out


def unlink_self_shop(session: Session, shop: Shop, user_id: int) -> Shop:
    """解除渔货栈关联（档案保留在渔枢系统里，只是这个账号不再看到它）。"""
    if shop.self_linked_user_id == user_id:
        shop.self_linked_user_id = 0
        shop.self_linked_at = None
        shop.updated_at = now()
        session.flush()
        session.commit()
    return shop


def sync_member_shops(session: Session, member: User, user_id: int) -> list[Shop]:
    """把自营账号组成员名下的店铺档案同步给渔货栈账号（只记关联，不改归属）。

    已经关联过的跳过（同一条不重复记时间），返回这次真正同步上的店铺。
    """
    rows = session.scalars(
        select(Shop).where(Shop.owner_user_id == member.id).order_by(Shop.name.asc())
    ).all()
    synced: list[Shop] = []
    for row in rows:
        # 已经被别的渔货栈账号关联了，不动 —— 但「成员自己认领的」不算（那是他自己的店，
        # 学员填了他的名字照样该同步过来）。claim_shop 认领时 owner 与 self_linked 都写
        # 同一个 id，所以这里要把 owner 本人排除掉，不然会误判成「已被别人关联」。
        if row.self_linked_user_id and row.self_linked_user_id not in (user_id, member.id):
            continue
        row.self_linked_user_id = user_id
        row.self_linked_at = now()
        row.updated_at = now()
        synced.append(row)
    if synced:
        session.flush()
        session.commit()
    return synced


# 渔货栈「自动关联」认的字段：改了这几个才需要反向同步一次（见 src/web/shop_sync.py）
_SELF_SYNC_FIELDS = ("operator", "member_name", "phone", "contact_phone")


def _notify_self_sync(shop_id: int) -> None:
    """渔枢端建了 / 改了店铺后，反向同步给该看到它的渔货栈账号。

    按手机号 / 姓名找出对应的渔货栈账号，给他建关联 —— **他不用再进渔货栈点一次同步**。
    两端合并部署才调得到（分端部署时渔货栈那边进页面会自己同步一次，这里静默跳过）；
    异常一律吞掉：建档是主流程，不该被渔货栈那边的关联台账拖住。
    """
    if not shop_id:
        return
    try:
        from src.web.shop_sync import sync_shop  # noqa: PLC0415
    except Exception:  # noqa: BLE001  分端部署：进程里没有渔货栈那套
        return
    try:
        hit = sync_shop(int(shop_id))
        if hit:
            logger.info("渔货栈店铺反向同步：店铺 %s 关联给 %s 个账号", shop_id, hit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("渔货栈店铺反向同步失败（不影响建档）：%s", exc)


def _touches_self_sync(data) -> bool:
    """这次改动有没有碰到「自动关联」认的那几个字段（运营人 / 会员名 / 手机号）。"""
    if not hasattr(data, "get"):
        return False
    return any(str(data.get(f) or "").strip() for f in _SELF_SYNC_FIELDS)


def _apply_shop_type(row: Shop, data) -> None:
    """店铺类型：只能取 SHOP_TYPE_CHOICES 里的三个值。

    不对的值（老数据、渔货栈手填、接口乱传）一律回落默认「个人闲置店铺」，
    不抛错 —— 这是档案里的分类字段，不该把一次保存整体打断。
    表单里压根没带这个字段时（渔货栈表单）保持原值不动。
    """
    if not hasattr(data, "get") or "shop_type" not in data:
        return
    value = _clean(data.get("shop_type"))
    row.shop_type = value if value in SHOP_TYPE_CHOICES else SHOP_TYPE_DEFAULT


def _apply_shop_nature(row: Shop, data) -> None:
    """店铺性质（自营店铺 / 学员店铺，2026-10-11 学员场景）：与「店铺类型」是两个维度。

    不对的值一律回落默认「自营店铺」，不抛错；表单里没带这个字段时保持原值不动
    （多表单共用一个保存路由时的规矩）。
    """
    if not hasattr(data, "get") or "shop_nature" not in data:
        return
    value = _clean(data.get("shop_nature"))
    row.shop_nature = value if value in SHOP_NATURE_CHOICES else SHOP_NATURE_DEFAULT


def _apply_service_fee_rate(row: Shop, data) -> None:
    """平台技术服务费率（%）：填了就按填的记，乱填（负数 / 汉字 / 超过 100）一律回默认 0.6。

    跟店铺类型一样：这是档案里的配置字段，不该因为填错就把整次保存打断。
    表单里没带这个字段时（渔货栈表单）保持原值不动。
    """
    if not hasattr(data, "get") or "service_fee_rate" not in data:
        return
    raw = _clean(data.get("service_fee_rate"))
    if not raw:
        return
    try:
        rate = float(raw)
    except ValueError:
        rate = SERVICE_FEE_RATE_DEFAULT
    if rate < SERVICE_FEE_RATE_MIN or rate > SERVICE_FEE_RATE_MAX:
        rate = SERVICE_FEE_RATE_DEFAULT
    row.service_fee_rate = round(rate, 4)


def _apply_real_name_verified(row: Shop, data) -> None:
    """实名认证是否完成（勾选框）：表单没带这个字段时保持原值不动。"""
    if not hasattr(data, "get") or "real_name_verified" not in data:
        return
    row.real_name_verified = str(data.get("real_name_verified") or "").strip().lower() in _TRUTHY


def _apply_fields(row: Shop, data) -> None:
    for field in _TEXT_FIELDS:
        if field == "name":
            continue
        if hasattr(data, "get") and field not in data:
            continue
        setattr(row, field, _clean(data.get(field) if hasattr(data, "get") else ""))


def delete_shop(session: Session, shop_id: int) -> None:
    row = session.get(Shop, shop_id)
    if row is None:
        return
    session.delete(row)
    session.commit()


# ------------------------------------------------------- 自动建档 / 历史补齐
def ensure_shop(
    session: Session,
    name: str,
    operator: str = "",
    source: str = "",
    nature: str = "",
    coach: str = "",
) -> Shop | None:
    """订单里出现的出货账号：店铺档案里有就复用，没有就自动建档。

    群里出单 / 登记表导入 / 订单页维护都会走这里 —— 发送人的店铺在店铺管理里没有时
    自动补一条记录（只填账号名称与运营人，其余等运营补）。不提交事务，调用方统一提交。

    nature / coach（2026-10-11 学员模板）：群里「学员姓名 / 学员店铺名称 / 带教」这套
    模板出的单，店铺性质标「学员店铺」（默认都是「自营店铺」），带教写进「所属带教」。
    """
    text = (name or "").strip()
    if not text or text in ("-", "无", "未知"):
        return None
    want_nature = nature if nature in SHOP_NATURE_CHOICES else ""
    want_coach = (coach or "").strip()
    row = find_by_name(session, text)
    if row is not None:
        # 运营人（= 飞书发送人）后来才明确的：补进档案，下次就能按人对上店
        if operator and not (row.operator or "").strip():
            row.operator = operator.strip()
        if want_nature:
            row.shop_nature = want_nature
        if want_coach and not (row.coach or "").strip():
            row.coach = want_coach
        return row
    row = Shop(name=text, code=_next_code(session), status=SHOP_STATUS_RUNNING)
    if operator:
        row.operator = operator.strip()
    if want_nature:
        row.shop_nature = want_nature
    if want_coach:
        row.coach = want_coach
    row.note = f"订单自动建档（{source or '出单账号识别'}）：请补全设备、类目与支付宝等资料"
    session.add(row)
    session.flush()
    logger.info(
        "店铺自动建档：%s（运营人：%s，性质：%s）",
        text, operator or "-", want_nature or SHOP_NATURE_DEFAULT,
    )
    return row


def ensure_student_shop(
    session: Session,
    shop_name: str,
    student_name: str = "",
    coach: str = "",
    fallback_operator: str = "",
    source: str = "群消息学员订单识别",
) -> Shop | None:
    """学员模板出的单 -> 学员店铺建档（2026-10-11）。

    · 店铺名 = 学员店铺名称（这单没有「出货闲鱼号」，学员的店名就是出货账号）
    · 账号运营人 = 学员姓名（运营人空着时才填，不覆盖人工维护的）
    · 店铺性质 = 学员店铺；带教 = 所属带教
    """
    name = (shop_name or "").strip()
    if not name:
        return None
    return ensure_shop(
        session,
        name,
        operator=(student_name or "").strip() or fallback_operator,
        source=source,
        nature=SHOP_NATURE_STUDENT,
        coach=coach,
    )


def shop_stat(session: Session, shop: Shop) -> dict:
    """按历史订单统计该店铺的经营情况（实时算，不落库）。"""
    rows = session.scalars(
        select(Order).where(Order.xianyu_account == shop.name)
    ).all()
    sale = round(sum(float(o.sale_amount or 0) for o in rows), 2)
    cost = round(sum(float(o.cost_amount or 0) for o in rows), 2)
    last = max((o.ordered_at for o in rows if o.ordered_at), default=None)
    return {
        "orders": len(rows),
        "sale": sale,
        "cost": cost,
        "profit": round(sale - cost, 2),
        "last_order_at": last,
    }


def enrich_from_orders(session: Session, shop: Shop) -> list[str]:
    """用历史订单里已有的信息补档案缺的字段（回款归属人、主营供应链、运营人）。

    只补档案里空着的，不覆盖台账维护好的资料；返回补了哪些字段（展示用）。
    """
    rows = session.scalars(
        select(Order).where(Order.xianyu_account == shop.name)
    ).all()
    if not rows:
        return []
    filled: list[str] = []
    payees = Counter((o.payee_name or "").strip() for o in rows if (o.payee_name or "").strip())
    suppliers = Counter(
        (o.supplier_name or "").strip() for o in rows if (o.supplier_name or "").strip()
    )
    senders = Counter((o.sender_name or "").strip() for o in rows if (o.sender_name or "").strip())
    if payees and not (shop.payee_name or "").strip():
        shop.payee_name = payees.most_common(1)[0][0]
        filled.append("回款归属人")
    if suppliers and not (shop.supply or "").strip():
        shop.supply = suppliers.most_common(1)[0][0]
        filled.append("主营供应链")
    if senders and not (shop.operator or "").strip():
        shop.operator = senders.most_common(1)[0][0]
        filled.append("账号运营人")
    if filled:
        shop.updated_at = now()
    return filled


def backfill_from_orders(session: Session, commit: bool = True) -> dict:
    """按历史订单补齐店铺档案：没建档的自动建档，建档了的补历史信息。

    可反复执行（幂等）：已经有的不动，只补缺的。返回统计。
    """
    before = session.scalar(select(func.count(Shop.id))) or 0
    stats = {"shops_before": before, "created": 0, "enriched": 0, "names": []}
    accounts: dict[str, list[Order]] = {}
    for order in session.scalars(select(Order)).all():
        account = (order.xianyu_account or "").strip()
        if not account:
            continue
        accounts.setdefault(account, []).append(order)

    for account, orders in accounts.items():
        sender = next(
            ((o.sender_name or "").strip() for o in orders if (o.sender_name or "").strip()),
            "",
        )
        existed = find_by_name(session, account) is not None
        shop = ensure_shop(session, account, operator=sender, source="历史订单补齐")
        if shop is None:
            continue
        filled = enrich_from_orders(session, shop)
        if not existed:
            stats["created"] += 1
            stats["names"].append(f"+ {account}（运营人：{shop.operator or '-'}）")
        elif filled:
            stats["enriched"] += 1
            stats["names"].append(f"~ {account}（补：{'、'.join(filled)}）")
    if commit:
        session.commit()
    logger.info(
        "店铺档案补齐：新建 %s 家、补充信息 %s 家", stats["created"], stats["enriched"]
    )
    return stats


def import_ledger(
    session: Session,
    rows: list[dict],
    apply: bool = True,
) -> dict:
    """把台账记录导入成店铺档案（已存在的按台账补全缺失字段，不覆盖已维护的资料）。

    返回 {total, created, updated, skipped, items}，apply=False 时只预览不写库。
    """
    summary = {"total": len(rows), "created": 0, "updated": 0, "skipped": 0, "items": []}
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            summary["skipped"] += 1
            summary["items"].append({"action": "skip", "name": "", "reason": "没有账号名称"})
            continue
        existing = find_by_name(session, name)
        if existing is None:
            summary["created"] += 1
            summary["items"].append(
                {"action": "create", "name": name, "code": (row.get("code") or "").strip()}
            )
            if not apply:
                continue
            try:
                create_shop(session, row, source="台账导入")
            except ValueError as exc:
                summary["created"] -= 1
                summary["skipped"] += 1
                summary["items"][-1] = {"action": "error", "name": name, "reason": str(exc)}
            continue
        # 已存在：只补空字段，台账与手工维护的资料不被覆盖
        changed: list[str] = []
        for field in _TEXT_FIELDS:
            value = (row.get(field) or "").strip()
            if not value or field == "name":
                continue
            if not (getattr(existing, field) or "").strip():
                setattr(existing, field, value)
                changed.append(field)
        if changed:
            existing.updated_at = now()
            summary["updated"] += 1
            summary["items"].append(
                {"action": "update", "name": name, "reason": "、".join(changed)}
            )
        else:
            summary["skipped"] += 1
            summary["items"].append({"action": "skip", "name": name, "reason": "已存在且无新信息"})
        if apply:
            session.flush()
    if apply:
        session.commit()
    return summary


def stats_by_account(session: Session) -> dict[str, dict]:
    """按出货闲鱼号聚合订单统计（列表页一行一店展示）。"""
    out: dict[str, dict] = {}
    rows = session.execute(
        select(
            Order.xianyu_account,
            func.count(Order.id),
            func.sum(Order.sale_amount),
            func.sum(Order.cost_amount),
            func.max(Order.ordered_at),
        )
        .where(Order.xianyu_account.is_not(None), Order.xianyu_account != "")
        .group_by(Order.xianyu_account)
    ).all()
    for account, count, sale, cost, last in rows:
        sale = float(sale or 0)
        cost = float(cost or 0)
        out[(account or "").strip()] = {
            "orders": int(count or 0),
            "sale": round(sale, 2),
            "profit": round(sale - cost, 2),
            "last_order_at": last,
        }
    return out


def shop_orders(session: Session, shop: Shop, limit: int = 10) -> list[Order]:
    return list(
        session.scalars(
            select(Order)
            .where(Order.xianyu_account == shop.name)
            .order_by(Order.ordered_at.desc(), Order.id.desc())
            .limit(limit)
        ).all()
    )


def distinct_values(session: Session, column) -> list[str]:
    return [
        r[0]
        for r in session.execute(
            select(column).where(column != "").group_by(column).order_by(column)
        ).all()
    ]


def shop_label(shop: Shop) -> str:
    """统一展示名：有编号时显示「账号名称（XYSK0001）」。"""
    name = (shop.name or "").strip()
    code = (shop.code or "").strip()
    return f"{name}（{code}）" if name and code else (name or code)


def last_order_text(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else "—"
