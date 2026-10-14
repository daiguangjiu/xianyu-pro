"""渔货栈「店铺管理」与渔枢系统店铺档案之间的桥。

店铺数据只有一个来源：**渔枢系统库里的 `shops` 表**（渔枢系统端「店铺管理」看到的就是同一份数据）。
渔货栈账号在这里维护自己名下的店铺，渔枢系统端审核，只有「已通过」的店铺才在渔货栈生效。

几个约定：

- 渔货栈账号 = 渔枢系统统一账号（登录名就是手机号，见 `app/hub_auth.py`），
  所以「我的店铺」= `shops.owner_user_id` 等于当前渔枢系统账号 id 的记录；
- 渔货栈只能看 / 改 / 删自己提交的店铺，别人的一律当不存在；
- 微信密码这类敏感信息不在渔货栈填（只在渔枢系统端维护），见 `FORM_FIELDS`；
- 渔货栈单独部署（进程里没有渔枢系统那套代码）时 `available()` 为假，页面给提示而不是报错。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 取决于部署形态（合并部署才有 app 包）
    from app.db import SessionLocal as _hub_session
    from app.hub_auth import unified_user as _hub_unified_user
    from app.models import (
        SELF_RUN_ROLE_NAME,
        SERVICE_FEE_RATE_DEFAULT,
        SHOP_AUDIT_PENDING,
        SHOP_AUDIT_TEXT,
        SHOP_NATURE_CHOICES,
        SHOP_NATURE_DEFAULT,
        SHOP_TYPE_CHOICES,
        SHOP_TYPE_DEFAULT,
        Role as _HubRole,
        Shop as _HubShop,
    )
    from app.services import shop as _hub_shop
except Exception:  # noqa: BLE001
    _hub_session = None
    _hub_unified_user = None
    _HubShop = None
    _HubRole = None
    _hub_shop = None
    SELF_RUN_ROLE_NAME = "自营账号组"
    SHOP_AUDIT_PENDING = "pending"
    SHOP_AUDIT_TEXT: dict[str, str] = {}
    # 渔枢系统那套没加载时也得有值：模板与路由会引用它们
    SERVICE_FEE_RATE_DEFAULT = 0.6
    SHOP_TYPE_DEFAULT = "个人闲置店铺"
    SHOP_TYPE_CHOICES = (
        "个人闲置店铺",
        "个人认证鱼小铺",
        "企业认证鱼小铺",
    )
    # 店铺性质（2026-10-11 学员场景）：自营店铺 / 学员店铺
    SHOP_NATURE_DEFAULT = "自营店铺"
    SHOP_NATURE_STUDENT = "学员店铺"
    SHOP_NATURE_CHOICES = (SHOP_NATURE_DEFAULT, SHOP_NATURE_STUDENT)

# 渔货栈「新建店铺」只给两个选项（2026-10-11 用户定的口径：个人店铺 / 鱼小铺），
# 存进渔枢系统时换成它那套固定值（见 to_hub_shop_type）。
# 两边都在：合并部署与独立部署读到的都是这一份。
SELF_SHOP_TYPES: tuple[tuple[str, str], ...] = (
    ("个人店铺", "个人闲置店铺"),
    ("鱼小铺", "个人认证鱼小铺"),
)

# 渔枢系统账号角色 → 中文（顶栏「身份」用）
HUB_ROLE_TEXT = {"admin": "平台管理员", "user": "子账号"}

# 渔货栈能填的字段：与渔枢系统端店铺保持一致，
# 不含 wechat_password / wechat_pay_password（敏感信息只在渔枢系统端维护）
# shop_type / service_fee_rate 也在这里：两端的店铺档案字段要求一致（渔枢系统加什么渔货栈就有什么）
FORM_FIELDS = (
    "name", "code", "operator", "owner", "device", "category", "phone", "status",
    "supply", "alipay", "payee_name", "credit_score", "real_name", "xianqi", "yuli",
    "violation", "wechat", "account_age", "note", "shop_type", "service_fee_rate",
    # 学员建档也要填的：闲鱼会员名、本人联系电话、实名认证是否完成
    "member_name", "contact_phone", "real_name_verified",
    # 店铺性质（自营 / 学员）与所属带教：学员店铺才有带教，由渔枢系统端的学员模板自动带出
    "shop_nature", "coach",
)

# 学员账号建档只填这 8 项（自营账号组的运营才填完整那份）：
# 店铺名称 / 会员名 / 手机号（闲鱼绑的）/ 实名认证是否完成 / 实际运营人姓名 /
# 联系电话 / 微信 / 希望经营类目
STUDENT_FIELDS = (
    "name", "member_name", "phone", "real_name_verified", "operator",
    "contact_phone", "wechat", "category",
)

# 展示用：一次性把店铺关键信息拍平成 dict，模板不直接摸渔枢系统的 ORM 对象
_VIEW_FIELDS = (
    "id", "name", "code", "operator", "owner", "device", "category", "phone", "status",
    "supply", "alipay", "payee_name", "credit_score", "real_name", "xianqi", "yuli",
    "violation", "wechat", "account_age", "note", "shop_type", "service_fee_rate",
    "audit_status", "audit_note",
    "member_name", "contact_phone", "real_name_verified",
    "shop_nature", "coach",
    "owner_user_id", "self_linked_user_id", "self_linked_at",
    "created_at", "updated_at",
)


def available() -> bool:
    """当前部署里有没有渔枢系统那套（合并部署才有店铺库可写）。"""
    return _hub_shop is not None and _hub_session is not None


def viewer(request) -> Any | None:
    """当前登录的渔枢系统统一账号；未登录 / 没接入渔枢系统时为 None。"""
    if _hub_unified_user is None:
        return None
    try:
        return _hub_unified_user(request)
    except Exception as exc:  # noqa: BLE001 取账号失败不该把页面打挂
        logger.warning("取渔枢系统账号失败：%s", exc)
        return None


def viewer_role_name(hub) -> str:
    """当前账号在渔枢系统里的权限角色名（roles.name）；没挂角色 / 取不到时返回空串。"""
    if hub is None or _hub_session is None or _HubRole is None:
        return ""
    role_id = getattr(hub, "role_id", None)
    if not role_id:
        return ""
    try:
        with _hub_session() as db:
            role = db.get(_HubRole, int(role_id))
            return str(getattr(role, "name", "") or "")
    except Exception as exc:  # noqa: BLE001 角色取不到不该把页面打挂
        logger.warning("取账号角色失败：%s", exc)
        return ""


def is_student_form(hub) -> bool:
    """建档表单走哪一套：学员账号只填 8 项，自营账号组（和管理员）填完整那一份。

    判定口径按渔枢系统的权限角色名：角色名是「自营账号组」的走完整表单，
    管理员也走完整表单（它在渔枢系统端维护档案），其余（含学员）走精简表单。
    """
    if hub is not None and str(getattr(hub, "role", "") or "") == "admin":
        return False
    return viewer_role_name(hub).strip() != SELF_RUN_ROLE_NAME


def _brief(row) -> dict[str, Any]:
    data = {field: getattr(row, field, None) for field in _VIEW_FIELDS}
    data["audit_text"] = SHOP_AUDIT_TEXT.get(data.get("audit_status") or "", data.get("audit_status") or "")
    data["is_self_submitted"] = bool(getattr(row, "owner_user_id", 0))
    # 两个「显示用」口径跟渔枢系统端一致：类型认不出的老数据按默认显示，费率没填按 0.6% 显示
    data["shop_type_label"] = (
        getattr(row, "shop_type_label", None)
        or (data.get("shop_type") if data.get("shop_type") in SHOP_TYPE_CHOICES else SHOP_TYPE_DEFAULT)
    )
    rate = getattr(row, "service_fee_rate_value", None)
    if rate is None:
        try:
            rate = float(data.get("service_fee_rate") or SERVICE_FEE_RATE_DEFAULT)
        except (TypeError, ValueError):
            rate = SERVICE_FEE_RATE_DEFAULT
    data["service_fee_rate_value"] = rate
    # 渔货栈店铺已关联：从渔枢系统带入 / 同步过来的店铺（不一定改了归属）
    data["self_linked"] = bool(getattr(row, "self_linked_user_id", 0))
    data["self_linked_only"] = bool(getattr(row, "self_linked_user_id", 0)) and not bool(
        getattr(row, "owner_user_id", 0)
    )
    data["real_name_verified_text"] = "已完成" if data.get("real_name_verified") else "未完成"
    return data


def my_shop_stats(owner_user_id: int) -> dict[str, int]:
    """我名下店铺的口径统计：总数 / 已通过（生效）/ 待审核 / 已驳回。"""
    empty = {"total": 0, "approved": 0, "pending": 0, "rejected": 0}
    if not available() or not owner_user_id:
        return empty
    with _hub_session() as db:
        return {
            "total": _hub_shop.count_shops(db, owner_user_id=owner_user_id),
            "approved": _hub_shop.count_shops(db, owner_user_id=owner_user_id, audit="approved"),
            "pending": _hub_shop.count_shops(db, owner_user_id=owner_user_id, audit="pending"),
            "rejected": _hub_shop.count_shops(db, owner_user_id=owner_user_id, audit="rejected"),
        }


def my_shops(owner_user_id: int, q: str = "", audit: str = "") -> list[dict[str, Any]]:
    """我提交的店铺（待审核的排前面，方便自己看到进度）。"""
    if not available() or not owner_user_id:
        return []
    with _hub_session() as db:
        rows = _hub_shop.list_shops(db, q=q, audit=audit, owner_user_id=owner_user_id)
    order = {"pending": 0, "rejected": 1, "approved": 2}
    rows.sort(key=lambda r: (order.get(r.audit_status or "", 3), -(r.id or 0)))
    out: list[dict[str, Any]] = []
    for row in rows:
        data = _brief(row)
        # 「渔枢带入」= 我看得到、但不是我提交的（档案挂在渔枢系统那边的账号名下）
        data["self_linked_only"] = bool(getattr(row, "self_linked_user_id", 0)) and (
            getattr(row, "owner_user_id", 0) != owner_user_id
        )
        out.append(data)
    return out


def get_mine(owner_user_id: int, shop_id: int) -> dict[str, Any] | None:
    """取自己能维护的店铺（自己提交的 / 从渔枢系统带入的）；别人的返回 None。"""
    if not available() or not owner_user_id:
        return None
    with _hub_session() as db:
        row = _hub_shop.owned_shop(db, shop_id, owner_user_id)
        if row is None:
            return None
        data = _brief(row)
        data["self_linked_only"] = bool(getattr(row, "self_linked_user_id", 0)) and (
            getattr(row, "owner_user_id", 0) != owner_user_id
        )
        return data


def to_hub_shop_type(value: str) -> str:
    """渔货栈的「个人店铺 / 鱼小铺」→ 渔枢系统那套固定值；认不出的按个人闲置店铺。"""
    text = str(value or "").strip()
    for mine, hub in SELF_SHOP_TYPES:
        if text == mine:
            return hub
    return text if text in SHOP_TYPE_CHOICES else SHOP_TYPE_DEFAULT


def _payload(form: Any, fields: tuple[str, ...] = FORM_FIELDS) -> dict[str, str]:
    """从表单里挑出渔货栈能写的字段（表单没带的字段一律不碰，免得清掉渔枢系统维护的资料）。"""
    data: dict[str, str] = {}
    for field in fields:
        if hasattr(form, "get") and form.get(field) is not None:
            data[field] = str(form.get(field) or "")
    # 店铺类型：页面上只有「个人店铺 / 鱼小铺」，转成渔枢系统认的值再存
    if "shop_type" in data:
        data["shop_type"] = to_hub_shop_type(data["shop_type"])
    return data


def _take_existing(db, row, user_id: int, data: dict[str, str]) -> tuple[dict, str]:
    """渔枢系统里已经有这条店铺档案时的处理：能带入就带入，带入不了就说明原因。

    - 已经在我名下的：不用重复加；
    - 渔枢系统端建档、还没归属任何渔货栈账号的（owner_user_id = 0）：直接认领过来；
    - 挂在别人名下、但那人就是我填的「实际运营人」且属于渔枢系统后台自营账号组的：
      同步过来（只记关联，不改归属，渔枢系统那边照样看得到）；
    - 其余：动不了，让学员去找管理员。
    """
    name = str(getattr(row, "name", "") or "")
    if row.owner_user_id == user_id or row.self_linked_user_id == user_id:
        raise ValueError(f"店铺「{name}」已经在你的店铺列表里了，不用重复添加")
    if not row.owner_user_id:
        _hub_shop.claim_shop(db, row, user_id)
        return _brief(row), "claimed"
    operator = str(data.get("operator") or "").strip()
    if operator:
        member = _hub_shop.find_self_run_member(db, operator)
        if member is not None and member.id == row.owner_user_id:
            _hub_shop.link_self_shop(db, row, user_id)
            return _brief(row), "linked"
    raise ValueError(f"店铺「{name}」已被其它账号占用：请联系管理员在渔枢系统里把它关联给你")


def create_mine(owner_user_id: int, owner_name: str, form: Any, student: bool = False):
    """新增我的店铺：进渔枢系统库，状态为「待审核」（渔枢系统端审核通过后才在渔货栈生效）。

    渔枢系统里已经有这条档案时不再报错「已存在」，而是按需带入（返回值的第二项是动作）：
      · claimed —— 认领渔枢系统端建档的无主档案（归属也转过来）；
      · linked  —— 按运营人姓名同步自营账号组那边的店铺（只记关联，不改归属）；
      · synced:N —— 填的运营人是自营账号组的人，把他名下的 N 家店铺一次性同步过来；
      · created —— 渔枢系统里没有，正常新建（走审核）。
    """
    if not available() or not owner_user_id:
        raise ValueError("当前部署未接入渔枢系统，店铺管理不可用")
    data = _payload(form, STUDENT_FIELDS if student else FORM_FIELDS)
    if not str(data.get("owner") or "").strip() and owner_name and not student:
        data["owner"] = owner_name  # 所属人默认填提交人，渔枢系统端一眼看出是谁提的
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("店铺名称不能为空")
    with _hub_session() as db:
        dup = _hub_shop.find_by_name(db, name)
        if dup is not None:
            return _take_existing(db, dup, owner_user_id, data)
        # 名字没撞上：填的「实际运营人」是渔枢系统后台自营账号组的人时，
        # 他那边的店铺档案就是现成的，直接同步过来，不用学员再填一遍
        member = _hub_shop.find_self_run_member(db, str(data.get("operator") or "").strip())
        if member is not None and member.id != owner_user_id:
            synced = _hub_shop.sync_member_shops(db, member, owner_user_id)
            if synced:
                return _brief(synced[0]), f"synced:{len(synced)}"
        row = _hub_shop.create_shop(
            db,
            data,
            source=f"渔货栈账号提交（{owner_name or owner_user_id}）",
            owner_user_id=owner_user_id,
            audit_status=SHOP_AUDIT_PENDING,
        )
        return _brief(row), "created"


def update_mine(owner_user_id: int, shop_id: int, form: Any, student: bool = False):
    """改我的店铺：改完回到「待审核」，避免审核通过后再换信息。"""
    if not available() or not owner_user_id:
        raise ValueError("当前部署未接入渔枢系统，店铺管理不可用")
    with _hub_session() as db:
        if _hub_shop.owned_shop(db, shop_id, owner_user_id) is None:
            raise ValueError("店铺不存在或不属于当前账号")
        row = _hub_shop.update_shop(
            db, shop_id, _payload(form, STUDENT_FIELDS if student else FORM_FIELDS),
            audit_status=SHOP_AUDIT_PENDING,
        )
        return _brief(row)


def delete_mine(owner_user_id: int, shop_id: int) -> str:
    """删我的店铺，返回给前端的提示语。

    自己提交的：删掉渔枢系统库里的档案（订单数据不受影响）；
    只是从渔枢系统同步过来的（档案挂在别人名下）：只解除关联，档案给人家留着。
    """
    if not available() or not owner_user_id:
        raise ValueError("当前部署未接入渔枢系统，店铺管理不可用")
    with _hub_session() as db:
        row = _hub_shop.owned_shop(db, shop_id, owner_user_id)
        if row is None:
            raise ValueError("店铺不存在或不属于当前账号")
        if row.owner_user_id != owner_user_id:
            _hub_shop.unlink_self_shop(db, row, owner_user_id)
            return f"已解除关联：店铺「{row.name}」的档案保留在渔枢系统里"
        name = str(row.name or "")
        _hub_shop.delete_shop(db, shop_id)
        return f"店铺「{name}」已删除"


def viewer_summary(request, current_user=None) -> dict[str, Any]:
    """顶栏右上角要显示的东西：用户名 / 身份 / 关联店铺（生效 + 待审核）。"""
    hub = viewer(request)
    name = ""
    role_text = ""
    if current_user is not None:
        # 显示名优先（渔枢系统「用户管理」里填的姓名），没有才退到登录名 ——
        # 页面上看到「刘宇辉」比看到「liuyuhui」有用得多（2026-10-11 用户提）
        name = str(getattr(current_user, "display_name", "") or "").strip()
        if not name:
            name = str(getattr(current_user, "nickname", "") or "").strip()
        role_text = str(getattr(current_user, "role_text", "") or "").strip()
        if not name:
            name = str(getattr(current_user, "phone", "") or "").strip()
    if hub is not None:
        # 统一账号那边也先看显示名（渔枢「用户管理」里填的姓名），最后才退到登录名
        name = name or str(getattr(hub, "display_name", "") or "").strip()
        name = name or str(getattr(hub, "username", "") or "")
        if not role_text:
            role_text = HUB_ROLE_TEXT.get(str(getattr(hub, "role", "") or ""), "")
    stats = my_shop_stats(getattr(hub, "id", 0) or 0) if hub is not None else {}
    return {
        "name": name or "—",
        "role_text": role_text or "—",
        "shops_total": int(stats.get("total", 0) or 0),
        "shops_approved": int(stats.get("approved", 0) or 0),
        "shops_pending": int(stats.get("pending", 0) or 0),
        "linked": hub is not None,
    }
