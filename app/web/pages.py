"""Web 管理界面（服务端渲染）。"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader, pass_context
from sqlalchemy import String, cast, false, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import BASE_DIR, settings
from ..db import get_db
from ..models import (
    IDENTITY_CHOICES,
    ORDER_STATE_CHOICES,
    ORDER_STATE_CLOSED,
    ORDER_STATE_LABELS,
    REFUND_STATUS_CHOICES,
    REFUND_STATUS_LABELS,
    SETTLEMENT_GRANULARITIES,
    SETTLEMENT_GRANULARITY_LABELS,
    SETTLEMENT_KINDS,
    SETTLEMENT_KIND_LABELS,
    SETTLEMENT_STATUSES,
    SELF_RUN_ROLE_NAME,
    SETTLEMENT_STATUS_LABELS,
    SHOP_AUDIT_CHOICES,
    SHOP_AUDIT_PENDING,
    SHOP_AUDIT_TEXT,
    SHOP_NATURE_CHOICES,
    SHOP_TYPE_CHOICES,
    Order,
    OrderImage,
    Product,
    ProductSku,
    PushBatch,
    PushBatchItem,
    RawMessage,
    Role,
    Settlement,
    ShareCard,
    Shop,
    Supplier,
    SyncState,
    User,
    is_refund_order,
    is_refund_pending,
    not_recalled,
    pending_push_order,
)
from ..parser.engine import parse_text
from ..portals import PORTALS, PORTAL_COOKIE, portal, portal_names, portal_of_path, switch_options
from ..parser.rules import get_rules, parse_rule_dict
from .. import timeutil
from ..timeutil import beijing_now, now
from ..services import audit as audit_service
from ..services import help as help_service
from ..services import purchase_node as node_service
from ..services import phone as phone_service
from ..services import sourcing_display as display_service
from ..services import push as push_service
from ..services.stats import day_expr
from ..services import recall as recall_service
from ..services import stats as stats_service
from ..services import backup as backup_service
from ..services import changelog as changelog_service
from ..services import supplier as supplier_service
from ..services import tag as tag_service
from ..services import users as users_service
from ..services import voice as voice_service
from ..services import wms_bridge            # 渔集（检测仓）只读桥：扫条码带出货品
from ..services import ingest as ingest_service
from ..services import dedupe as dedupe_service
from ..services import logistics as logistics_service
from ..services import logistics_trace
from ..services import merge as merge_service
from ..services import learn as learn_service
from ..services import notify as notify_service
from ..services import product as product_service
from ..services import category as category_service
from ..services import shipping as shipping_service
from ..services import return_addr as return_addr_service
from . import nav
from . import permissions
from ..services import shop as shop_service
from ..services import intent as intent_service
from ..services import daily_summary as daily_summary_service
from ..services import finance as finance_service
from ..services import self_sync as self_sync_service
from ..services import sourcing_publish as sourcing_service
from ..services import order_log as order_log_service
from ..services import order_state as order_state_service
from ..services import policy as policy_service
from ..services import refund as refund_service
from ..services import message_rules
from ..services import error_log as error_log_service
from ..services import share_card as share_card_service
from ..messages_registry import CHANNELS, KINDS
from ..services.notify import (
    DEFAULT_RECEIPT_TEMPLATE,
    DEFAULT_REFUND_DETAIL_TEMPLATE,
)
from ..services.ingest import process_pending, process_pending_images, sync_chat
from ..services.notify import (
    flush_held_notifications,
    is_active_time,
    notify_processed,
    sync_tracking_to_group,
)
from ..services.settings_store import ENV_PATH, is_writable, mask_secret, write_updates
from ..timeutil import now
from ..utils import human_time, media_url, money, thumb_url
from ..feishu.client import get_client, reset_client
from .auth import (
    ROLE_ADMIN,
    ROLE_USER,
    authenticate,
    clear_session_cookie,
    current_user,
    set_session_cookie,
)

router = APIRouter()

logger = logging.getLogger(__name__)

# 系统名称：导航品牌、浏览器标题、登录页统一取这里，避免多处写死
APP_NAME = "渔枢系统"
APP_BRAND = "渔枢系统"
APP_TAGLINE = "小渔数科内部商品运营管理平台"

def _asset_version() -> str:
    """静态资源版本号（取更新日志里的当前版本）：升级后 CSS/JS 的 URL 变化，
    浏览器会重新拉取，避免改了样式但页面还用着缓存里的旧样式。"""
    try:
        return changelog_service.current_version() or "1"
    except Exception:  # noqa: BLE001
        return "1"


templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
# 全站共用组件（如分页条 templates/_pager.html）放在仓库根的 templates/ 里，
# 自己目录找不到时回落到那儿 —— 渔枢、渔货栈、商家端共用同一个分页组件，不各写一份。
_SHARED_TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
if _SHARED_TEMPLATES.is_dir() and _SHARED_TEMPLATES != Path(templates.env.loader.searchpath[0]):
    templates.env.loader = ChoiceLoader([templates.env.loader, FileSystemLoader(str(_SHARED_TEMPLATES))])
templates.env.globals.update(
    {
        "media_url": media_url,
        # 缩略图（2026-10-15）：列表 / 购物车的小图用 /thumb/...?w=<宽度>，不拉原图
        "thumb_url": thumb_url,
        "money": money,
        "human_time": human_time,
        "app_name": APP_NAME,
        "app_brand": APP_BRAND,
        "app_tagline": APP_TAGLINE,
        "asset_v": _asset_version(),
        # 供应商统一展示为「供应商名称（供应商编码）」，加深对编码的记忆
        "sup_label": supplier_service.supplier_label,
        # 推单状态文案：撤单 / 退单的订单按场景显示（用户撤单，无需推单 …）
        "push_status_label": push_service.supplier_status_label,
        # 推单状态配色：逆向订单统一用退单色，不会跟「待推单 / 已推单」混
        "push_status_tone": push_service.supplier_status_tone,
        # 流程说明：页面弹窗与 /help 说明页同源（app/services/help.py），模板里 help_doc('push') 直接取
        "help_doc": help_service.topic,
        # 页面内区块权限（推单信息 / 合并订单）：模板里 can_block(current_user, 'order_push')
        "can_block": permissions.can_view_block,
    }
)

@pass_context
def _dt_filter(context, value, pattern: str = "%Y-%m-%d %H:%M"):
    """时间展示统一走它：**库里存 UTC，展示一律按服务端统一时区（默认北京）**。

    2026-10-13 起不跟浏览器时区 —— 时区统一由 `timeutil.tz_of()` 决定
    （`settings.display_tz`，开了 `TZ_FOLLOW_BROWSER` 才看 `?tz=` / cookie `tz`）。
    模板里别再直接 `.strftime()`（那样出来的是库里的 UTC 值，比北京早 8 小时）。
    """
    return timeutil.fmt(value, pattern, _user_tz(context.get("request")))


@pass_context
def _ts_filter(context, value) -> int:
    """UTC naive -> epoch 毫秒（前端 `<time data-ts>` 按浏览器本地时区显示）。"""
    dt = timeutil.to_local(value, _user_tz(context.get("request")))
    if dt is None:
        return 0
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _user_tz(request) -> str | None:
    try:
        tz = request.query_params.get("tz") if request is not None else None
        if not tz and request is not None:
            tz = request.cookies.get(timeutil.TZ_COOKIE)
        return tz or None
    except Exception:  # noqa: BLE001
        return None


templates.env.filters["dt"] = _dt_filter
templates.env.filters["ts"] = _ts_filter
# 商品详情展示：纯文本按填写格式渲染（换行 / 空行 / 图标都保留），含 HTML 才原样输出
templates.env.filters["detail_rich"] = product_service.detail_rich

PAGE_SIZE = 20
# 资金结算「按笔」明细的每页条数（流水比订单多，翻页太碎不好对账）
FINANCE_PAGE_SIZE = 50
# 商品列表每页条数：默认 50（导入一批货就有几十款，20 太碎、不分页又会拖垮页面）
PRODUCT_PAGE_SIZE = 50
PRODUCT_PAGE_SIZES = (20, 50, 100, 200)

# 现行上行模板（在系统设置页展示，用于对齐群里发送格式）
# 字段名的标准写法是「产品名称」，旧写法「产品 / 商品」依旧兼容
# 「所属供应链」填供应商编码（如 GYS001）或供应商名称，系统都会对上供应商档案，
# 显示与统计统一为「供应商名称（供应商编码）」
# 备注：记录下单备注等信息（留空也请保留这一行，系统会把「备注：」识别为字段）
STANDARD_TEMPLATE = """产品名称： AirPods Pro 3 新款苹果耳机
收货人： 基拉
手机号码： 17842144173
地址信息： 浙江省 宁波市 北仑区 梅山街道 极氪山川生活区
备注： 要黑色，尽快发货
售价：469
成本价：
所属供应链：
出货闲鱼号：
回款支付宝归属人："""


# 一次性提示（跳转后显示一条消息）：走短命 cookie，读一次即删，URL 保持干净
FLASH_COOKIE = "hub_flash"


def _flashes(request: Request) -> list[dict]:
    """一次性提示：优先读 cookie（URL 保持干净，不带中文提示语），老 ?msg= 链接继续兼容。"""
    out = []
    raw = request.cookies.get(FLASH_COOKIE)
    if raw:
        try:
            data = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
            if isinstance(data, dict) and data.get("msg"):
                out.append({"text": str(data.get("msg")), "kind": str(data.get("kind") or "")})
        except (ValueError, TypeError, UnicodeDecodeError):
            pass
    msg = request.query_params.get("msg")
    if msg and not out:
        out.append({"text": msg, "kind": request.query_params.get("kind", "")})
    return out


def render(name: str, request: Request, **ctx) -> HTMLResponse:
    ctx.setdefault("flashes", _flashes(request))
    ctx.setdefault("request", request)
    ctx.setdefault("settings", settings)
    # 商品详情页地址统一由它生成（用商品编码做流水号，不用中文商品名）
    ctx.setdefault("product_url", product_service.product_url)
    # 当前登录用户：导航栏按角色显示菜单（管理员才看到规则/设置/日志/账号）
    ctx.setdefault("current_user", current_user(request))
    # 侧边栏与面包屑：由当前路径在导航结构里反查（见 web/nav.py）
    # 可见菜单按角色权限算（见 web/permissions.py）：管理员 = 全部，其余按角色勾选
    nav_user = ctx.get("current_user")
    nav_allowed = permissions.menu_keys_for(nav_user)
    nav_path = request.url.path
    nav_query = dict(request.query_params)
    ctx.setdefault("nav_sections", nav.build(nav_path, nav_query, nav_allowed))
    # 面包屑默认按当前路径反查；挂在别的菜单下的细页可以指定 crumb_path + crumb_leaf
    # （如发货详情属于「供应链发货」，出生地在供应链板块，不该指到「订单管理」去）
    ctx.setdefault(
        "crumbs",
        nav.crumbs(
            str(ctx.pop("crumb_path", "") or nav_path),
            nav_query,
            nav_allowed,
            leaf=str(ctx.pop("crumb_leaf", "") or ""),
        ),
    )
    # 端切换下拉：只列出当前账号有权限的端，并标出当前所在端（见 app/portals.py）
    ctx.setdefault("portals", switch_options(nav_user, nav_path))
    ctx.setdefault("current_portal", portal(portal_of_path(nav_path)))
    # 页面内区块权限（推单信息 / 合并订单）：角色权限页勾选，模板里 can_block(current_user, key) 判断
    ctx.setdefault("block_tree", permissions.block_tree())
    # 列表序号：跨页连续（第 2 页接着上一页的号），没分页的列表按首页算，模板直接用 page/per_page
    ctx.setdefault("page", 1)
    ctx.setdefault("per_page", PAGE_SIZE)
    resp = templates.TemplateResponse(request=request, name=name, context=ctx)
    if request.cookies.get(FLASH_COOKIE):
        # 提示只展示一次：渲染过就删掉，刷新页面不再重复弹
        resp.delete_cookie(FLASH_COOKIE, path="/")
    return resp


def _product_back(db: Session, product_id: int) -> str:
    """商品类操作后的回跳地址：能取到商品就用编码流水号地址（与详情页 URL 口径一致）。"""
    row = db.get(Product, product_id)
    return product_service.product_url(row) if row else f"/products/{product_id}"


def _admin_or_redirect(request: Request) -> RedirectResponse | None:
    """管理员专属页面：子账号直接退回首页并提示。"""
    user = current_user(request)
    if user and user.role == ROLE_ADMIN:
        return None
    return _redirect("/", "仅管理员可见：解析规则、系统设置、操作日志与账号管理", "warn")


def _admin_user(request: Request):
    """管理员专属写操作：不是管理员返回 None（调用方直接重定向）。"""
    user = current_user(request)
    return user if user and user.role == ROLE_ADMIN else None


def _require_block(request: Request, block: str, label: str) -> None:
    """改数据的动作也要过区块权限。

    页面上把板块藏了只是「看不见」，直接 POST 照样能改 —— 编辑订单 / 推单 / 转账 / 合并
    都在这里拦一道（管理员与默认角色不受影响，见 permissions.can_view_block）。
    """
    if not permissions.can_view_block(current_user(request), block):
        raise HTTPException(403, f"没有{label}的权限（请联系管理员在「角色权限」里开通）")


def _redirect(url: str, msg: str = "", kind: str = "") -> RedirectResponse:
    """跳转 + 一次性提示。

    提示走短命 cookie（读一次即删），**不再拼进 URL** —— 否则商品名这类中文会进地址栏，
    分享 / 复制的链接又长又乱（商品详情页现在用商品编码做流水号）。
    """
    resp = RedirectResponse(url, status_code=303)
    if msg:
        payload = json.dumps({"msg": msg, "kind": kind}, ensure_ascii=False).encode("utf-8")
        resp.set_cookie(
            FLASH_COOKIE,
            base64.urlsafe_b64encode(payload).decode("ascii"),
            max_age=30,
            httponly=True,
            samesite="lax",
            path="/",
        )
    return resp


def _username(request: Request) -> str:
    """当前登录账号名（未登录返回空串），用于操作留痕。"""
    user = current_user(request)
    return user.username if user else ""


def _paginate(query, page: int, size: int = PAGE_SIZE):
    """分页取数，返回 (当前页数据, 总条数, 总页数, 当前页码)。

    页码越界（0 / 负数 / 超过最后一页，或地址栏里手改的值）一律夹到 1..pages，
    避免出现「空列表 + 分页条高亮到不存在的页码」这种看起来像坏掉的体验。
    """
    total = int(query.count() or 0)
    pages = max((total + size - 1) // size, 1)
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    current = min(max(current, 1), pages)
    items = query.offset((current - 1) * size).limit(size).all()
    return items, total, pages, current


# 图表配色：按排名循环取，饼图 / 柱状图共用
CHART_COLORS = (
    "#2563eb", "#f59e0b", "#10b981", "#8b5cf6",
    "#ef4444", "#06b6d4", "#84cc16", "#f472b6",
)


def _chart_items(
    rows,
    name_key: str,
    value_key: str,
    top: int = 8,
    money: bool = True,
    short_len: int = 5,
) -> list[dict]:
    """统计行 -> 图表数据（带颜色与展示文案），超出前 top 的合并成「其他」。"""
    items: list[dict] = []
    for i, row in enumerate(rows[:top]):
        name = str(row.get(name_key) or "未指定")
        value = float(row.get(value_key) or 0.0)
        items.append(
            {
                "name": name,
                "value": abs(value),
                "text": f"¥{value:,.2f}" if money else f"{value:,.0f}",
                "short": name[:short_len],
                "color": CHART_COLORS[i % len(CHART_COLORS)],
            }
        )
    rest = sum(float(r.get(value_key) or 0.0) for r in rows[top:])
    if abs(rest) > 0.0001:
        items.append(
            {
                "name": "其他",
                "value": abs(rest),
                "text": f"¥{rest:,.2f}" if money else f"{rest:,.0f}",
                "short": "其他",
                "color": "#94a3b8",
            }
        )
    return items


def _safe_next(target: str) -> str:
    """只接受站内相对路径，避免开放重定向。"""
    target = (target or "").strip()
    return target if target.startswith("/") and not target.startswith("//") else "/"


# ------------------------------------------------------------------ 登录 / 登出
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = ""):
    if current_user(request):
        return _redirect(_safe_next(next))
    return render("login.html", request, next=_safe_next(next), error="")


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    target = _safe_next(str(form.get("next", "")))

    user = authenticate(username, password)
    if user is not None and not target:
        # 没指定跳转目标时回到上次用的端（端切换会记在 cookie 里）
        last = portal(request.cookies.get(PORTAL_COOKIE, ""))
        if user.has_portal(last["key"]):
            target = last["home"]
    if user is None:
        audit_service.log_action(
            db, None, "登录失败", target=username, detail="账号或密码错误", request=request
        )
        return render("login.html", request, next=target, error="账号或密码错误，或账号已停用")

    audit_service.log_action(db, user, "登录成功", request=request)
    resp = _redirect(target)
    set_session_cookie(resp, user.id)
    return resp


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    user = current_user(request)
    if user:
        audit_service.log_action(db, user, "退出登录", request=request)
    resp = _redirect("/login", "已退出登录")
    clear_session_cookie(resp)
    return resp


# ------------------------------------------------------------------ 用户管理（管理员）
@router.get("/portal/{key}")
def portal_switch(request: Request, key: str):
    """端切换：有权限才让进，记住选择后跳到该端首页。"""
    user = current_user(request)
    item = portal(key)
    if not user or not user.has_portal(item["key"]):
        return _redirect("/", "当前账号没有该子系统的权限，请联系管理员开通", "warn")
    resp = _redirect(item["home"])
    resp.set_cookie(PORTAL_COOKIE, item["key"], max_age=90 * 24 * 3600, httponly=False)
    return resp


@router.get("/roles", response_class=HTMLResponse)
def roles(request: Request, db: Session = Depends(get_db)):
    """角色权限：一个角色能进哪些菜单（粒度到菜单；数据级范围先留位，不生效）。"""
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    return render(
        "roles.html",
        request,
        heading="角色权限",
        sub="角色勾选能进哪些菜单；账号在「用户管理」里挂角色（不挂 = 默认菜单）",
        rows=permissions.rows(db),
        menu_tree=permissions.menu_tree(),
        # 三套子系统的菜单级权限（渔枢 / 渔集 / 渔货栈），一个角色一次勾完
        permission_tree=permissions.permission_tree(),
        role_choices=permissions.role_choices(db),
    )


@router.post("/roles/create")
async def role_create(request: Request, db: Session = Depends(get_db)):
    admin = _admin_user(request)
    if not admin:
        return _admin_or_redirect(request)
    form = await request.form()
    name = str(form.get("name") or "").strip()
    if not name:
        return _redirect("/roles", "角色名称不能为空", "warn")
    try:
        role = permissions.save_role(
            db,
            name=name,
            description=str(form.get("description") or ""),
            menus=[str(key) for key in form.getlist("menus")],
            # 页面内区块（推单信息 / 合并订单）：表单里勾了才有，全不勾 = 都不给
            blocks=[str(key) for key in form.getlist("blocks")],
        )
    except ValueError as exc:
        return _redirect("/roles", str(exc), "warn")
    return _redirect("/roles", f"已新增角色「{role.name}」（{len(role.menus)} 个菜单）")


@router.post("/roles/{role_id}/edit")
async def role_edit(request: Request, role_id: int, db: Session = Depends(get_db)):
    admin = _admin_user(request)
    if not admin:
        return _admin_or_redirect(request)
    form = await request.form()
    role = db.get(Role, role_id)
    if role is None:
        return _redirect("/roles", "角色不存在", "warn")
    if role.is_system:
        return _redirect("/roles", f"「{role.name}」是内置角色，菜单由系统决定，不能改", "warn")
    try:
        permissions.save_role(
            db,
            role=role,
            name=str(form.get("name") or "").strip() or role.name,
            description=str(form.get("description") or ""),
            menus=[str(key) for key in form.getlist("menus")],
            blocks=[str(key) for key in form.getlist("blocks")],
        )
    except ValueError as exc:
        return _redirect("/roles", str(exc), "warn")
    return _redirect("/roles", f"已保存角色「{role.name}」（{len(role.menus)} 个菜单）")


@router.post("/roles/{role_id}/delete")
async def role_delete(request: Request, role_id: int, db: Session = Depends(get_db)):
    admin = _admin_user(request)
    if not admin:
        return _admin_or_redirect(request)
    role = db.get(Role, role_id)
    if role is None:
        return _redirect("/roles", "角色不存在", "warn")
    name = role.name
    try:
        permissions.delete_role(db, role)
    except ValueError as exc:
        return _redirect("/roles", str(exc), "warn")
    return _redirect("/roles", f"已删除角色「{name}」，原来挂它的账号回到默认菜单")


@router.get("/users", response_class=HTMLResponse)
def users(request: Request, db: Session = Depends(get_db)):
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    return render(
        "users.html",
        request,
        heading="用户管理",
        sub="管理员可创建子账号；子账号能进哪些菜单由「角色权限」里的角色决定",
        accounts=users_service.list_users(db),
        me=current_user(request),
        username_rule=users_service.USERNAME_RULE,
        password_rule=users_service.PASSWORD_RULE,
        # 端授权：管理员默认全端，子账号按勾选（见 app/portals.py）
        portals=PORTALS,
        # 身份：内部人员 / 外部学员（外部学员进渔货栈会先走一遍开店引导）
        identity_choices=IDENTITY_CHOICES,
        # 角色：子账号按角色决定可见菜单（见 web/permissions.py）
        role_choices=permissions.role_choices(db),
        role_names={int(r.id): r.name for r in permissions.role_choices(db)},
    )


@router.post("/users")
async def user_create(request: Request, db: Session = Depends(get_db)):
    admin = _admin_user(request)
    if not admin:
        return _admin_or_redirect(request)
    form = await request.form()
    try:
        row = users_service.create_user(
            db,
            str(form.get("username", "")),
            str(form.get("password", "")),
            str(form.get("role", ROLE_USER)),
            str(form.get("display_name", "")),
            # 可用端：多选框同 name，form.getlist 一次取全
            ",".join(form.getlist("portals")),
            # 身份：内部人员（默认）/ 外部学员（进渔货栈先走开店引导）
            identity=str(form.get("identity", "")),
        )
    except ValueError as exc:
        return _redirect("/users", str(exc), "warn")
    role_id = _form_int(form.get("role_id"))
    if role_id:
        row.role_id = role_id
        db.commit()
        permissions.invalidate_cache()
    role_text = f"，角色：{_role_name(db, role_id)}" if role_id else ""
    return _redirect(
        "/users", f"已创建账号「{row.username}」（{portal_names(row.portals)}{role_text}）"
    )


def _role_name(db: Session, role_id: int | None) -> str:
    if not role_id:
        return "默认（普通成员）"
    role = db.get(Role, int(role_id))
    return role.name if role else "默认（普通成员）"


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
def user_edit_page(request: Request, user_id: int, db: Session = Depends(get_db)):
    """编辑子页面：列表页只留「编辑 / 删除」两个入口，改字段都来这一页，不再挤在一行里。"""
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    row = users_service.get_user(db, user_id)
    if not row:
        return _redirect("/users", "账号不存在", "warn")
    return render(
        "user_edit.html",
        request,
        heading=f"编辑账号「{row.username}」",
        sub="改完点保存；密码单独重置，不影响账号本身",
        account=row,
        me=current_user(request),
        portals=PORTALS,
        identity_choices=IDENTITY_CHOICES,
        role_choices=permissions.role_choices(db),
        username_rule=users_service.USERNAME_RULE,
        password_rule=users_service.PASSWORD_RULE,
        phone_rule=users_service.PHONE_RULE,
    )


@router.post("/users/{user_id}/edit")
async def user_edit(request: Request, user_id: int, db: Session = Depends(get_db)):
    admin = _admin_user(request)
    if not admin:
        return _admin_or_redirect(request)
    form = await request.form()
    try:
        role = str(form.get("role", "")).strip()
        if role:
            users_service.set_role(db, user_id, role)
        row = users_service.get_user(db, user_id)
        if row:
            display_name = str(form.get("display_name", "")).strip()
            if display_name:
                row.display_name = display_name
            # 角色可清空（回到「默认菜单」）：表单里空 option 也会提交 role_id=""，
            # 所以用 in 判断「这次提交里有没有带这个字段」，避免只改名字时把角色清掉
            if "role_id" in form:
                row.role_id = _form_int(form.get("role_id")) or None
                permissions.invalidate_cache()
            # 登录账号 / 手机号也能改：两个都能登录，改完原来那个就登不进去了
            users_service.set_identity(
                db,
                user_id,
                username=str(form.get("username", "")) if "username" in form else None,
                phone=str(form.get("phone", "")) if "phone" in form else None,
            )
            # 身份（内部人员 / 外部学员）：改完他在渔货栈看到的东西跟着变
            if "identity" in form:
                users_service.set_identity_kind(db, user_id, str(form.get("identity", "")))
            db.commit()
    except ValueError as exc:
        return _redirect("/users", str(exc), "warn")
    return _redirect("/users", "账号已更新")


@router.post("/users/{user_id}/portals")
async def user_portals(request: Request, user_id: int, db: Session = Depends(get_db)):
    """调整子账号能进哪些子系统（渔枢系统 / 渔货栈 / 渔集）。"""
    admin = _admin_user(request)
    if not admin:
        return _admin_or_redirect(request)
    form = await request.form()
    try:
        row = users_service.set_portals(db, user_id, form.getlist("portals"))
    except ValueError as exc:
        return _redirect("/users", str(exc), "warn")
    return _redirect("/users", f"「{row.username}」的可用端已设为：{portal_names(row.portals)}")


@router.post("/users/{user_id}/password")
async def user_password(request: Request, user_id: int, db: Session = Depends(get_db)):
    admin = _admin_user(request)
    if not admin:
        return _admin_or_redirect(request)
    form = await request.form()
    try:
        users_service.set_password(db, user_id, str(form.get("password", "")))
    except ValueError as exc:
        return _redirect("/users", str(exc), "warn")
    return _redirect("/users", "密码已重置")


@router.post("/users/{user_id}/status")
async def user_status(request: Request, user_id: int, db: Session = Depends(get_db)):
    admin = _admin_user(request)
    if not admin:
        return _admin_or_redirect(request)
    form = await request.form()
    try:
        users_service.set_status(db, user_id, str(form.get("status", "")).strip())
    except ValueError as exc:
        return _redirect("/users", str(exc), "warn")
    return _redirect("/users", "账号状态已更新")


@router.post("/users/{user_id}/delete")
def user_delete(request: Request, user_id: int, db: Session = Depends(get_db)):
    admin = _admin_user(request)
    if not admin:
        return _admin_or_redirect(request)
    try:
        users_service.delete_user(db, user_id, operator_id=admin.id)
    except ValueError as exc:
        return _redirect("/users", str(exc), "warn")
    return _redirect("/users", "账号已删除", "warn")


# ------------------------------------------------------------------ 操作日志（管理员）
@router.get("/logs", response_class=HTMLResponse)
def logs(
    request: Request,
    q: str = "",
    username: str = "",
    action: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    items, total, pages, page = audit_service.list_logs(
        db, q=q, username=username, action=action, page=page
    )
    return render(
        "logs.html",
        request,
        heading="操作日志",
        sub=f"共 {total} 条记录",
        logs=items,
        total=total,
        page=page,
        pages=pages,
        actions=audit_service.distinct_actions(db),
        usernames=audit_service.distinct_usernames(db),
        filters={"q": q, "username": username, "action": action},
    )


# ------------------------------------------------------------------ 异常日志（管理员）
@router.get("/errors", response_class=HTMLResponse)
def errors_page(
    request: Request,
    q: str = "",
    scope: str = "",
    kind: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    """系统异常日志（2026-10-09）：线上报错都在这儿，看堆栈直接定位到行。

    三个子系统（渔枢 / 检测仓 / 渔货栈）共用这一张表，靠 ``scope`` 区分 ——
    谁挂了都记，不用分头翻容器日志。
    """
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    items, total, pages, page = error_log_service.list_logs(
        db, q=q, scope=scope, kind=kind, page=page
    )
    stat = error_log_service.summary(db)
    return render(
        "errors.html",
        request,
        heading="异常日志",
        sub=f"共 {total} 条 · 近 24 小时 {stat['recent']} 条 · 出错时页面上的编号就是这里的序号",
        logs=items,
        total=total,
        page=page,
        pages=pages,
        per_page=30,
        stat=stat,
        scopes=error_log_service.SCOPES,
        kinds=error_log_service.KIND_LABELS,
        scope_label=error_log_service.scope_label,
        kind_label=error_log_service.kind_label,
        filters={"q": q, "scope": scope, "kind": kind},
    )


@router.post("/errors/prune")
def errors_prune(request: Request, db: Session = Depends(get_db)):
    """清理 30 天前的异常日志（表会一直长，老记录留着也没人看）。"""
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    removed = error_log_service.prune(db, 30)
    return _redirect("/errors", f"已清理 30 天前的异常日志 {removed} 条", "warn")


@router.post("/api/client-error")
async def client_error_report(request: Request):
    """前端上报（2026-10-09）：页面拿到非 JSON 响应 / 脚本崩了，把现场报回来。

    前端能看到服务端看不到的东西 —— **那个非 JSON 响应的状态码与原文**，
    正是「上传失败」这类问题唯一有用的线索（以前只剩一句 ``Unexpected token '<'``）。
    这个接口**本身绝不能再出错**：录不进去也要回 200，否则前端的重试会把日志刷爆。
    """
    payload = _json_body(await request.body())
    log_id = error_log_service.record_client(
        request, payload if isinstance(payload, dict) else {}
    )
    return JSONResponse({"ok": True, "log_id": log_id})


# ------------------------------------------------------------------ 总览
@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    start: str = "",
    end: str = "",
    db: Session = Depends(get_db),
):
    s = stats_service.summary(db)
    # 趋势图的区间：?start=&end=（北京自然日）；不传就是近 30 天，认不出的日期回落默认
    trend_start, trend_end = stats_service.resolve_range(start, end, 30)
    trend = stats_service.daily_trend(db, start=trend_start, end=trend_end)
    max_sale = max((d["sale"] for d in trend), default=1) or 1
    # 区间汇总：累计金额 / 日均金额（按区间天数摊，含没有订单的那天）/ 件数
    trend_total = round(sum(d["sale"] for d in trend), 2)
    trend_qty = sum(d["qty"] for d in trend)
    trend_avg = round(trend_total / len(trend), 2) if trend else 0.0
    trend_unit = round(trend_total / trend_qty, 2) if trend_qty else 0.0
    # 首页四个维度各看前几名：热销 TOP 只露 5 条，完整榜单进「TOP 榜单」子页面
    suppliers = stats_service.by_supplier(db, 8)
    accounts = stats_service.by_account(db, 8)
    payees = stats_service.by_payee(db, 8)
    tops = stats_service.top_products(db, 5)
    return render(
        "dashboard.html",
        request,
        heading="数据总览",
        sub="订单管理、推单与资金概览",
        s=s,
        trend=trend,
        max_sale=max_sale,
        # 趋势图的区间与汇总：起止日期回填到筛选框，累计 / 日均 / 件单价给卡片头部
        trend_start=trend_start.isoformat(),
        trend_end=trend_end.isoformat(),
        trend_today=beijing_now().date().isoformat(),
        trend_total=trend_total,
        trend_avg=trend_avg,
        trend_qty=trend_qty,
        trend_unit=trend_unit,
        suppliers=suppliers,
        accounts=accounts,
        payees=payees,
        tops=tops,
        charts={
            "supplier_sale": _chart_items(suppliers, "supplier", "sale"),
            "account_sale": _chart_items(accounts, "account", "sale"),
            "payee_sale": _chart_items(payees, "payee", "sale"),
            "top_sale": _chart_items(tops, "product", "sale", top=5),
            "top_qty": _chart_items(tops, "product", "quantity", top=5, money=False),
        },
        counts=stats_service.status_counts(db),
        state_counts=stats_service.state_counts(db),
        # 订单性质统计（自营 / 学员）：与订单列表筛选项同一份口径
        nature_counts=stats_service.nature_counts(db),
        state_labels=ORDER_STATE_LABELS,
        state_choices=ORDER_STATE_CHOICES,
        finance=finance_service.summary(db),
        feishu_ready=settings.feishu_enabled,
    )


@router.get("/stats/products", response_class=HTMLResponse)
def stats_products(
    request: Request,
    sort: str = "count",
    dir: str = "desc",
    db: Session = Depends(get_db),
):
    """热销产品 TOP 榜单子页面：单品维度全量明细 + 图表 + 商品合并（只影响统计）。"""
    rows = stats_service.top_products(db, limit=0)
    sort_keys = {
        "product": lambda r: r["product"],
        "count": lambda r: r["count"],
        "quantity": lambda r: r["quantity"],
        "sale": lambda r: r["sale"],
        "cost": lambda r: r["cost"],
        "profit": lambda r: r["profit"],
        "margin": lambda r: r["margin"],
        "refund_count": lambda r: r["refund_count"],
        "refund_amount": lambda r: r["refund_amount"],
        # 占比排序就是按对应金额排序（单独给个键，表头箭头才不会和销售/毛利列一起亮）
        "sale_share": lambda r: r["sale"],
        "profit_share": lambda r: r["profit"],
    }
    key_fn = sort_keys.get(sort, sort_keys["count"])
    rows = sorted(rows, key=key_fn, reverse=(dir != "asc"))
    total_sale = sum(r["sale"] for r in rows) or 1
    return render(
        "stats_products.html",
        request,
        heading="热销产品 TOP 榜单",
        sub=f"共 {len(rows)} 个单品：按合并规则归并后统计，原始订单商品名不变",
        rows=rows,
        total=len(rows),
        total_sale=total_sale,
        total_profit=sum(r["profit"] for r in rows),
        charts={
            "sale": _chart_items(rows, "product", "sale", top=8),
            "profit": _chart_items(rows, "product", "profit", top=8),
            "sale_bar": _chart_items(rows, "product", "sale", top=10),
            "qty_bar": _chart_items(rows, "product", "quantity", top=10, money=False),
        },
        merges=stats_service.list_product_merges(db),
        suggests=stats_service.suggest_product_merges(db),
        sort=sort,
        dir=dir,
    )


@router.post("/stats/products/merge")
async def stats_products_merge(request: Request, db: Session = Depends(get_db)):
    """把取名不一致的多个商品名合并到同一个展示名（仅统计口径）。"""
    form = await request.form()
    canonical = str(form.get("canonical", "")).strip()
    members = [str(m).strip() for m in form.getlist("members") if str(m).strip()]
    if not canonical or not members:
        return _redirect(
            "/stats/products", "请先填合并后的商品名，并勾选要合并的商品", "warn"
        )
    count = stats_service.save_product_merge(
        db, canonical, members, actor=_username(request)
    )
    if not count:
        return _redirect(
            "/stats/products",
            f"没有可合并的：勾选的名字都等于「{canonical}」，或会造成两组互相合并",
            "warn",
        )
    leaves = stats_service.product_merge_leaf_count(db, canonical)
    more = f"，这一组现在共含 {leaves} 个写法" if leaves > count else ""
    return _redirect(
        "/stats/products",
        f"已把 {count} 个名字（含已合并的组）合并到「{canonical}」{more}；仅统计口径生效",
    )


@router.post("/stats/products/merge/delete")
async def stats_products_merge_delete(request: Request, db: Session = Depends(get_db)):
    """取消某个商品名的合并，恢复按原名单独统计。"""
    form = await request.form()
    member = str(form.get("member", "")).strip()
    if not member:
        return _redirect("/stats/products", "缺少要取消合并的商品名", "warn")
    if not stats_service.delete_product_merge(db, member):
        return _redirect("/stats/products", "没有找到该合并规则", "warn")
    return _redirect("/stats/products", f"已取消「{member}」的合并，恢复按原名统计")


# ------------------------------------------------------------------ 订单
# 自营账号组（常量见 app/models.py:SELF_RUN_ROLE_NAME）：
# 这个角色下的账号，订单筛选的「发送人」默认选中自己（显示名 = 飞书发送人），
# 名下的店铺档案也可以直接同步给渔货栈学员（见 app/services/shop.py）


def _default_sender(request: Request, db: Session) -> str:
    """订单筛选里「发送人」的默认值：自营账号组的账号默认只看自己名下的单。

    运营按人分工，自营账号组一次只看自己的单最省事；下拉里其他发送人照样能选，
    这里只是省一次点击。其他角色不默认（要看全量）。
    """
    user = current_user(request)
    if not user:
        return ""
    # session 里带过来的用户对象不带角色（role_id 是 None），回库里查一次拿最新角色
    row = db.get(User, user.id) or user
    if not row.role_id:
        return ""
    role = db.get(Role, int(row.role_id))
    if not role or role.name != SELF_RUN_ROLE_NAME:
        return ""
    # 显示名就是飞书里的发送人名（账号运营人）
    return (row.display_name or "").strip()


@router.get("/orders", response_class=HTMLResponse)
def orders(
    request: Request,
    q: str = "",
    supplier_status: str = "",
    state: str = "",
    supplier: str = "",
    account: str = "",
    # 订单性质（2026-10-11）：self=自营订单 / student=学员订单；按出货闲鱼号对应的店铺性质归
    nature: str = "",
    payee: str = "",
    # 发送人（飞书发送人）：不传 = 用默认值（自营账号组默认看自己）；传空 = 用户主动选了「全部」
    sender: str | None = None,
    review: str = "",
    refund: str = "",
    recalled: str = "",
    created: str = "",
    # closed=1：只看「关闭完成 / 已完成」的订单（订单归档下的子页面，见侧边栏）
    closed: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    # 预加载订单截图：列表里要显示缩略图
    stmt = db.query(Order).options(selectinload(Order.images))
    if q:
        like = f"%{q}%"
        stmt = stmt.filter(
            Order.order_no.like(like)
            | Order.product_name.like(like)
            | Order.buyer_nick.like(like)
            | Order.receiver_name.like(like)
            | Order.receiver_phone.like(like)
        )
    if supplier_status == "pending":
        # 待推单：撤单 / 退单的订单推单状态已经是「无需推单」，不该出现在待推单列表里
        stmt = stmt.filter(pending_push_order())
    elif supplier_status:
        stmt = stmt.filter(Order.supplier_status == supplier_status)
    if state:
        # 订单状态（生命周期）：新建 / 已推单 / 已发货 / 已撤单 / 已退单；
        # 支持逗号分隔的多值（首页「待发货」= new,pushed，一次筛出还没发货的单子）
        states = [v.strip() for v in state.split(",") if v.strip()]
        if states:
            stmt = stmt.filter(Order.order_state.in_(states))
    if supplier:
        # 订单里可能记的是编码、名称或「名称（编码）」：都能筛出同一家
        stmt = stmt.filter(
            Order.supplier_name.in_(supplier_service.build_index(db).keys(supplier))
        )
    if account:
        stmt = stmt.filter(Order.xianyu_account == account)
    if nature:
        # 订单性质：按**出货闲鱼号对应的店铺性质**归（学员店铺名单来自店铺档案，
        # 没有档案的账号含空号一律算自营 —— 与店铺档案的默认口径一致）
        student_accounts = shop_service.student_shop_names(db)
        if nature == "student":
            stmt = (
                stmt.filter(Order.xianyu_account.in_(student_accounts))
                if student_accounts
                else stmt.filter(false())
            )
        elif student_accounts:
            stmt = stmt.filter(Order.xianyu_account.notin_(student_accounts))
    if sender is None:
        # 没显式选过发送人：按账号角色给默认值（自营账号组默认看自己名下的单）
        sender = _default_sender(request, db)
    if sender:
        stmt = stmt.filter(Order.sender_name == sender)
    if payee:
        stmt = stmt.filter(Order.payee_name == payee)
    if review == "1":
        stmt = stmt.filter(Order.needs_review.is_(True))
    if refund:
        # 退单筛选统一走 models 里那一套口径（refund_status + order_state），页面不再各写各的：
        #   all      = 全部退单订单
        #   pending  = 待拦截（退单了但还没拦截 / 退款完，含老数据没记处理状态）
        #   其他值    = 按退单处理状态（已拦截 / 已发货无法拦截 / 已退款）筛
        # "1" 是老菜单链接（已退单 → ?refund=1）留下的写法，当 all 处理，免得老书签点进来空列表
        if refund in ("all", "1"):
            stmt = stmt.filter(is_refund_order())
        elif refund == "pending":
            stmt = stmt.filter(is_refund_pending())
        else:
            stmt = stmt.filter(Order.refund_status == refund)
    if recalled == "1":
        # 群消息已撤回的订单：只留档，页面置灰显示
        stmt = stmt.filter(Order.is_recalled.is_(True))
    elif recalled == "all":
        # 正常订单 + 撤单订单一起看（排查问题时用）
        pass
    else:
        # 默认只显示在途订单：撤单的都收进「撤单回收站」，订单列表保持干净
        stmt = stmt.filter(Order.is_recalled.is_(False))
    if created == "today":
        # 今日新增：口径与首页「今日新增」统计块一致。
        # 2026-10-12 修：以前直接截 created_at 的 UTC 日期，北京凌晨 0–8 点出的单
        # 会被算成前一天 —— 那段时间这一页会「今天一单都没有」。现在按北京自然日切。
        stmt = stmt.filter(day_expr(Order.created_at) == beijing_now().date().isoformat())
    if closed:
        # 「关闭完成 / 已完成」子页面：逆向流程走完的终态（order_state=closed）
        # + 推单已完成的（supplier_status=done）。这些单子钱货都处理完了，
        # 不再参与推单、待办与统计，单独一页便于事后回查对账。
        stmt = stmt.filter(
            or_(Order.order_state == ORDER_STATE_CLOSED, Order.supplier_status == "done")
        )
    # 成本没维护（=0）的订单数：列表顶部提醒推单人员及时补全（撤回订单不参与统计）
    zero_cost = stmt.filter(Order.cost_amount <= 0).filter(not_recalled()).count()
    # 当前筛选下有多少条是群里已撤回的留档单：首页统计不含这些，列表顶部说清楚避免两边数字对不上
    recalled_count = stmt.filter(Order.is_recalled.is_(True)).count()
    # 撤单回收站里的总数（右上角角标）：不受当前筛选影响
    recalled_total = (
        db.scalar(select(func.count(Order.id)).where(Order.is_recalled.is_(True))) or 0
    )
    # 待拦截的退单数：与「退单 = 待拦截」这个筛选项完全同一个口径，点进去条数必然对得上
    refund_pending = (
        db.scalar(select(func.count(Order.id)).where(not_recalled(), is_refund_pending())) or 0
    )
    # 退单各处理状态的数量：筛选项上直接标出来（退单订单总数 + 每种处理状态）
    refund_counts: dict[str, int] = {}
    for status_value in REFUND_STATUS_CHOICES:
        refund_counts[status_value] = (
            db.scalar(
                select(func.count(Order.id)).where(
                    not_recalled(), is_refund_order(), Order.refund_status == status_value
                )
            )
            or 0
        )
    refund_total = (
        db.scalar(select(func.count(Order.id)).where(not_recalled(), is_refund_order())) or 0
    )
    stmt = stmt.order_by(Order.id.desc())

    items, total, pages, page = _paginate(stmt, page)

    def _distinct(column) -> list[str]:
        return [
            r[0]
            for r in db.execute(
                select(column).where(column != "").group_by(column)
            ).all()
        ]

    # 筛选项 = 订单里出现过的供应商（统一成「名称（编码）」）+ 供应商档案里的供应商
    index = supplier_service.build_index(db)
    supplier_names: list[str] = []
    for raw in _distinct(Order.supplier_name):
        label = index.label(raw)
        if label and label not in supplier_names:
            supplier_names.append(label)
    for row in db.scalars(select(Supplier)).all():
        label = supplier_service.supplier_label(row)
        if label and label not in supplier_names:
            supplier_names.append(label)

    # 发送人筛选项：按单数从多到少排；默认选中的名字就算暂时没单也要留在选项里，
    # 否则下拉里找不到它，选中态就丢了
    sender_counts: dict[str, int] = {
        r[0]: r[1]
        for r in db.execute(
            select(Order.sender_name, func.count(Order.id))
            .where(Order.sender_name != "")
            .group_by(Order.sender_name)
        ).all()
    }
    if sender and sender not in sender_counts:
        sender_counts[sender] = 0

    return render(
        "orders.html",
        request,
        heading="订单管理 · 关闭完成" if closed else "订单管理",
        sub=(
            f"共 {total} 条已关闭 / 已完成的订单：钱货都处理完了，不再参与推单与统计"
            if closed
            else (
                f"共 {total} 条订单（含 {recalled_count} 条已撤回留档单，首页统计不含）"
                if recalled_count
                else f"共 {total} 条订单"
            )
        ),
        closed=bool(closed),
        orders=items,
        total=total,
        zero_cost=zero_cost,
        refund_pending=refund_pending,
        refund_total=refund_total,
        refund_counts=refund_counts,
        refund_status_choices=REFUND_STATUS_CHOICES,
        refund_status_labels=REFUND_STATUS_LABELS,
        recalled_total=recalled_total,
        page=page,
        pages=pages,
        per_page=PAGE_SIZE,
        suppliers=supplier_names,
        accounts=_distinct(Order.xianyu_account),
        # 订单性质：列表每行打标 + 筛选项计数，都按「出货闲鱼号 -> 店铺档案性质」这一份口径算
        natures=shop_service.nature_of_accounts(db),
        nature_counts=stats_service.nature_counts(db),
        payees=_distinct(Order.payee_name),
        senders=sorted(sender_counts, key=lambda name: (-sender_counts[name], name)),
        sender_counts=sender_counts,
        filters={"q": q, "supplier_status": supplier_status,
                 "supplier": supplier, "account": account, "payee": payee, "sender": sender,
                 "review": review, "nature": nature,
                 "refund": refund, "recalled": recalled, "created": created, "state": state,
                 "closed": closed},
        state_labels=ORDER_STATE_LABELS,
        state_choices=ORDER_STATE_CHOICES,
        state_counts=stats_service.state_counts(db),
    )


@router.get("/orders/recycle", response_class=HTMLResponse)
def order_recall_bin(request: Request, page: int = 1, db: Session = Depends(get_db)):
    """撤单回收站：群里撤回消息 / 撤掉的订单都收在这里，只留档、不参与统计，可一键恢复。"""
    stmt = (
        db.query(Order)
        .options(selectinload(Order.images))
        .filter(Order.is_recalled.is_(True))
        .order_by(Order.recalled_at.desc(), Order.id.desc())
    )
    items, total, pages, page = _paginate(stmt, page)
    return render(
        "recall_bin.html",
        request,
        heading="撤单回收站",
        sub=f"共 {total} 条撤单留档记录：只留档、不参与统计，确认没撤的可恢复成正常订单",
        orders=items,
        total=total,
        page=page,
        pages=pages,
        per_page=PAGE_SIZE,
        filters={},
    )


@router.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail(request: Request, order_id: int, db: Session = Depends(get_db)):
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    images = db.scalars(select(OrderImage).where(OrderImage.order_id == order_id)).all()
    batches = db.scalars(
        select(PushBatch).join(PushBatchItem).where(PushBatchItem.order_id == order_id)
    ).all()
    msg = db.scalar(
        select(RawMessage).where(RawMessage.message_id == order.source_message_id)
    )
    # 老订单缺发送人/发送时间时，按来源消息补一次（不改动已有值）
    source_msg = msg
    if source_msg and (not order.sender_id or order.msg_time is None):
        if not order.sender_id and source_msg.sender_id:
            order.sender_id = source_msg.sender_id
        if not order.sender_name and source_msg.sender_name:
            order.sender_name = source_msg.sender_name
        if order.msg_time is None and source_msg.msg_time is not None:
            order.msg_time = source_msg.msg_time
        if not order.chat_name and source_msg.chat_name:
            order.chat_name = source_msg.chat_name
        db.commit()
    # 订单性质（自营订单 / 学员订单）：按出货闲鱼号对应的店铺性质归；
    # 没有店铺档案的账号按默认「自营店铺」算，与店铺档案口径一致
    nature_map = shop_service.nature_of_accounts(db)
    shop_nature = shop_service.account_nature(nature_map, order.xianyu_account)
    shop_coach = (nature_map.get((order.xianyu_account or "").strip()) or {}).get("coach", "")
    # 关联渔货号（2026-10-15）：飞书订单模板里「YS + 日期 + 流水」那串 —— 报单的人
    # 把渔集的渔货号抄进模板，这里直接抠出来，点进去就是渔集的货品台账。
    # 可能写多个（一个订单发几款），去重保序。
    fish_codes: list[str] = []
    if msg and getattr(msg, "raw_text", ""):
        seen: set[str] = set()
        for code in re.findall(r"\bYS\d{8,12}\b", str(msg.raw_text)):
            if code not in seen:
                seen.add(code)
                fish_codes.append(code)
    # 渔集发货单 / 出库单（渔货栈自营商品同步发货，2026-10-15）：有没有同步过
    # 口径：origin=self 的渔货栈自营单，或供应链是渔货栈（YS001 / 名称带「渔货栈」）
    is_self_supply = push_service.is_self_supply_chain(db, order)
    wms_outbound: dict | None = None
    if is_self_supply:
        try:
            from wms.db import SessionLocal as _WmsSession
            from wms.models import (
                OUT_SALE as _WMS_OUT_SALE,
                OUTBOUND_STATUS_CANCELLED as _WMS_OUT_CANCELLED,
                WmsOutboundOrder as _WmsOutboundOrder,
            )

            with _WmsSession() as _ws:
                _wrow = _ws.scalar(
                    select(_WmsOutboundOrder)
                    .where(
                        _WmsOutboundOrder.type == _WMS_OUT_SALE,
                        _WmsOutboundOrder.target_no == order.order_no,
                        _WmsOutboundOrder.status != _WMS_OUT_CANCELLED,
                    )
                    .order_by(_WmsOutboundOrder.id.desc())
                )
            if _wrow:
                wms_outbound = {
                    "id": _wrow.id,
                    "order_no": _wrow.order_no,
                    "status": _wrow.status,
                }
        except Exception:
            logger.debug("读取渔集出库单失败（order=%s）", order.order_no, exc_info=True)
            wms_outbound = None
    return render(
        "order_detail.html",
        request,
        heading=f"订单 {order.order_no}",
        sub=f"{order.platform} · 置信度 {order.parse_confidence}",
        order=order,
        fish_codes=fish_codes,
        # 渔货栈自营商品（供应链 = 渔货栈YS001 / 自营单）：推单按钮换「同步到渔集发货」
        is_self_supply=is_self_supply,
        # 渔集发货单 / 出库单：同步过就显示出库单号与入口
        wms_outbound=wms_outbound,
        images=images,
        batches=batches,
        msg=msg,
        # 截图里识别到的疑似订单号（没认准时给运营一键采用）
        order_no_hint=ingest_service.order_no_hint_of(db, order),
        # 收货信息里待核对的字段（地址 / 收货人 / 手机号）：详情页逐字段标出来
        pending_check=notify_service.pending_check_labels(order),
        pending_check_names=notify_service.pending_check_names(order),
        push=push_service.build_push_payload(db, order),
        # 供应商只能从档案里选（订单页下拉的选项）
        suppliers=supplier_service.supplier_options(db),
        # 出货账号对应的店铺档案（店铺管理里维护）
        shop=shop_service.find_by_name(db, order.xianyu_account),
        # 订单性质：自营订单 / 学员订单（学员店铺把所属带教一并带出来）
        shop_nature=shop_nature,
        shop_coach=shop_coach,
        # 关联商品：自动匹配可能认错，这里可以看到并手工改（下拉选项是全部商品）
        product=db.get(Product, order.product_id) if order.product_id else None,
        products=product_service.list_products(db),
        product_label=product_service.product_label,
        # 订单处理日志：人工标记状态的动作，展示在推单信息下面、订单信息上面
        logs=order_log_service.list_for_order(db, order_id),
        log_operator_label=order_log_service.operator_label,
        state_labels=ORDER_STATE_LABELS,
        state_choices=ORDER_STATE_CHOICES,
        # 退单处理状态：下拉与列表筛选共用同一份定义，不再各自硬编码
        refund_status_choices=REFUND_STATUS_CHOICES,
        refund_status_labels=REFUND_STATUS_LABELS,
        # 逆向退单：按订单走到哪一步给运营的下一步动作提示（拦截 / 退回件 / 撤回发货 …）
        refund_hint=refund_service.intercept_hint(order),
        refund_push_status=push_service.refund_push_status(order),
        # 退单主路径 + 步骤清单：进度不再靠一个枚举猜，而是看这几件事做完了没
        refund_path=refund_service.refund_path(order),
        refund_path_label=refund_service.PATH_LABELS[refund_service.refund_path(order)],
        refund_path_action=refund_service.PATH_ACTION[refund_service.refund_path(order)],
        refund_steps=refund_service.refund_steps(order),
        refund_next=refund_service.next_step_hint(order),
        # 现在该做的那一步（页面只展开这一步，其余收成进度条）
        refund_now=refund_service.current_step(order),
        refund_blocks=refund_service.blocking_reasons(order),
        # 这单自己的体检问题（历史数据对不上的地方），有就在退单区块顶部提醒
        refund_issues=refund_service.order_issues(order),
        # 物流轨迹状态（预留：接入物流查询后由接口回写，未接入时显示「未查询」）
        tracking_labels=refund_service.TRACKING_STATUS_LABELS,
    )


@router.post("/orders/{order_id}/product")
async def order_product(request: Request, order_id: int, db: Session = Depends(get_db)):
    """把订单挂到某款商品（或清空关联）：商品与订单的联动入口。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    raw = str(form.get("product_id", "") or "").strip()
    try:
        product = product_service.set_order_product(db, order, int(raw) if raw.isdigit() else None)
    except ValueError as exc:
        return _redirect(f"/orders/{order_id}", str(exc), "warn")
    if product is None:
        return _redirect(f"/orders/{order_id}", "已解除商品关联")
    return _redirect(f"/orders/{order_id}", f"已关联商品「{product.name}」")


@router.post("/orders/{order_id}/push")
async def order_push(request: Request, order_id: int, db: Session = Depends(get_db)):
    """复制推单信息即视为已推单：订单标记为 pushed 并记录推单时间。

    详情页的复制按钮走 fetch（返回 JSON）；没有 JS 时表单提交也能用（回到详情页）。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    _require_block(request, "order_push", "推单信息")
    if order.is_recalled:
        return _redirect(
            f"/orders/{order_id}", "该订单已撤单（只留档）：不参与推单流程", "warn"
        )
    if order.is_refund:
        return _redirect(
            f"/orders/{order_id}",
            f"该订单已退单：推单状态「{push_service.refund_push_status(order)}」，不需要再推给供应商",
            "warn",
        )
    form = await request.form()
    transfer = str(form.get("transfer_status", "") or "").strip()
    already_pushed = bool(order.pushed_at or order.supplier_status == "pushed")
    push_service.mark_pushed(db, order, transfer or None)
    # 订单处理日志：谁在什么时候把单子推给了供应商
    order_log_service.log(
        db,
        order,
        order_log_service.ACTION_PUSH,
        detail=(
            f"{'重复复制' if already_pushed else '复制'}推单信息同步给供应商"
            f"；是否转账：{push_service.TRANSFER_LABELS.get(order.transfer_status, '未转账')}"
        ),
        operator=_username(request),
        commit=True,
    )
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse(
            {
                "ok": True,
                "supplier_status": order.supplier_status,
                "pushed_at": timeutil.fmt(order.pushed_at) if order.pushed_at else "",
            }
        )
    return _redirect(f"/orders/{order_id}", "已标记为已推单")


@router.post("/orders/{order_id}/push/transfer")
async def order_push_transfer(request: Request, order_id: int, db: Session = Depends(get_db)):
    """只切换「是否转账」并立即回最新推单文案（复制出去的就是最新的）。

    详情页的转账下拉框走 fetch（返回 JSON）；没有 JS 时普通表单提交也能用。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    _require_block(request, "order_push", "推单信息")
    form = await request.form()
    transfer = str(form.get("transfer_status", "") or "").strip()
    transfer_before = push_service.TRANSFER_LABELS.get(order.transfer_status, "未转账")
    try:
        push_service.set_transfer(db, order, transfer)
    except ValueError as exc:
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return _redirect(f"/orders/{order_id}", str(exc), "danger")
    transfer_after = push_service.TRANSFER_LABELS.get(order.transfer_status, "未转账")
    if transfer_after != transfer_before:
        order_log_service.log(
            db,
            order,
            order_log_service.ACTION_TRANSFER,
            detail=f"是否转账：{transfer_before} → {transfer_after}",
            operator=_username(request),
            commit=True,
        )

    payload = push_service.build_push_payload(db, order)
    if request.headers.get("x-requested-with") == "fetch":
        # 只回 JSON 安全字段（pushed_at 等 datetime 不能直接序列化）
        return JSONResponse(
            {
                "ok": True,
                "text": payload["text"],
                "transfer": payload["transfer"],
                "transfer_label": payload["transfer_label"],
                "pushed": payload["pushed"],
                "pushed_at": human_time(payload["pushed_at"]) if payload["pushed_at"] else "",
            }
        )
    return _redirect(f"/orders/{order_id}", f"已更新为「{payload['transfer_label']}」，推单文案已刷新")


@router.post("/orders/{order_id}/edit")
async def order_edit(request: Request, order_id: int, db: Session = Depends(get_db)):
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    _require_block(request, "order_edit", "编辑订单")
    form = await request.form()
    # 提交前的订单状态：用来区分「运营主动改了状态」与「只是顺手带回表单原值」
    original_state = order.order_state or order_state_service.ORDER_STATE_NEW
    # 提交前的其它状态字段：处理日志里要写清「什么变成了什么」
    before = {
        "order_state": original_state,
        "supplier_status": order.supplier_status or "",
        "transfer_status": order.transfer_status or "",
        "tracking_no": (order.tracking_no or "").strip(),
        "sender_name": (order.sender_name or "").strip(),
        "refund_amount": float(order.refund_amount or 0),
        "supplier_refund_amount": float(order.supplier_refund_amount or 0),
    }
    # 提交前各解析字段的值：改完比对差异，差出来的就是「机器取错、人工改对」，留作纠错样本
    before_parsed = learn_service.snapshot_parsed(order)

    def _flt(key: str) -> float:
        raw = str(form.get(key, "") or "").strip()
        raw = raw.replace(",", "").replace("¥", "").replace("￥", "").replace("元", "")
        try:
            return round(float(raw), 2)
        except ValueError:
            return 0.0

    # 只更新表单中实际出现的字段，避免部分提交把已有数据清空
    text_fields = (
        "buyer_nick", "product_name", "spec", "receiver_name", "receiver_phone",
        "receiver_address", "xianyu_account", "payee_name", "note",
        "tracking_no", "sender_name",
    )
    for name in text_fields:
        if name in form:
            setattr(order, name, str(form.get(name, "")).strip())
    if "supplier_name" in form:
        # 供应商从档案里选（值为「名称（编码）」）；老数据 / 手填的写法对不上档案时
        # 顺带补一条档案，保证订单与供应商档案始终对得上
        order.supplier_name = supplier_service.ensure_order_supplier(
            db, str(form.get("supplier_name", "")), source="订单页维护"
        )
    if "order_no" in form:
        new_no = str(form.get("order_no", "")).strip()
        if new_no:
            order.order_no = new_no
    if "xianyu_account" in form:
        # 出货账号（店铺）：店铺管理里还没有就自动补一条（运营人 = 订单的发送人）
        shop_service.ensure_shop(
            db,
            order.xianyu_account,
            operator=order.sender_name or "",
            source="订单页维护",
        )
    for name in ("sale_amount", "cost_amount", "shipping_fee"):
        if name in form:
            setattr(order, name, _flt(name))
    # 平台技术服务费：留空 → 按店铺费率按**当前售价**重算（改了售价手续费跟着变）；
    # 填了金额 → 以人工为准并打标记，下次改售价不再自动覆盖它。
    if "platform_fee" in form:
        raw_fee = str(form.get("platform_fee") or "").strip()
        if raw_fee:
            order.platform_fee = _flt("platform_fee")
            order.platform_fee_manual = True
        else:
            finance_service.sync_service_fee(db, order, force=True)
    if "refund_amount" in form:
        # 退给客户的钱：全额退填成交价，部分退就填实际退了多少（部分退款靠这里记）；不允许负数。
        # 符号口径（2026-10-03）：页面填正数，订单上存负值（钱出去），毛利公式里直接加。
        order.refund_amount = -abs(_flt("refund_amount") or 0.0)
    if "supplier_refund_amount" in form:
        # 供应商退给我们的钱：可能少于退给客户的（只退成本价），差多少就是这单自己亏多少
        order.supplier_refund_amount = max(_flt("supplier_refund_amount"), 0.0)
    if "quantity" in form:
        order.quantity = max(int(_flt("quantity") or 1), 1)
    if "needs_review" in form:
        order.needs_review = str(form.get("needs_review")) == "1"
        # 人工核对完了：把存疑提示一起清掉，列表与详情页不再显示
        if not order.needs_review:
            order.review_reason = ""
    # 只允许改推单状态：订单状态（order_state）走上面的状态机，退单处理走退单接口
    value = str(form.get("supplier_status", "")).strip()
    if value:
        order.supplier_status = value
    if "transfer_status" in form:
        # 走 push.apply_transfer：标记「已转账」时记下时间，资金结算按它算结算时间
        value = str(form.get("transfer_status", "")).strip()
        if value in push_service.TRANSFER_LABELS:
            push_service.apply_transfer(order, value)

    order.updated_at = now()
    db.commit()

    # 人工改对了什么：存成纠错样本，同样的写法下次解析直接按这次改（见 services/learn.py）
    learn_service.record_corrections(
        db, order, before_parsed, operator=_username(request), commit=True
    )

    # 订单状态（新建 / 已推单 / 已发货 / 已撤单 / 已退单）
    if "tracking_no" in form:
        # 回填了物流单号 -> 已发货；清空单号 -> 按当前事实回退（回到已推单 / 新建）
        if (order.tracking_no or "").strip():
            order_state_service.advance(db, order, order_state_service.ORDER_STATE_SHIPPED)
        else:
            order_state_service.refresh(db, order)
        db.commit()
    if "order_state" in form:
        target = str(form.get("order_state", "")).strip()
        # 只有用户主动改了状态（与提交前不同）才按手工改处理，避免覆盖上面的自动流转
        if target and target != original_state:
            ok, reason = order_state_service.set_state(
                db, order, target, actor=_username(request)
            )
            if not ok:
                return _redirect(f"/orders/{order_id}", reason, "danger")
            db.commit()
    # 金额 / 状态改了：同步资金结算流水（公司自营应收、公司自营应付）
    finance_service.sync_order(db, order, commit=True)

    # 这笔单是渔货栈推过来的（origin=self）：把发货 / 退款的结果回写给渔货栈，
    # 让那边跟着往下流转（已推单 → 已发货 → 已完成 / 退款完成）。
    # 只对渔货栈出的单生效，回写失败也不影响渔枢系统这边。
    self_sync_service.notify_self_fulfillment(order.order_no)

    # 订单处理日志：把这次编辑里真正改掉的状态 / 单号逐条记下来
    def _push_label(value: str) -> str:
        return push_service.SUPPLIER_STATUS_LABELS.get(value, value or "待推单发货")

    changes: list[tuple[str, str]] = []
    if (order.order_state or "") != before["order_state"]:
        changes.append(
            (
                order_log_service.ACTION_STATE,
                f"订单状态：{order_state_service.label(before['order_state'])}"
                f" → {order_state_service.label(order.order_state)}",
            )
        )
    if (order.supplier_status or "") != before["supplier_status"]:
        changes.append(
            (
                order_log_service.ACTION_SUPPLIER_STATUS,
                f"推单状态：{_push_label(before['supplier_status'])}"
                f" → {_push_label(order.supplier_status or '')}",
            )
        )
    if (order.transfer_status or "") != before["transfer_status"]:
        changes.append(
            (
                order_log_service.ACTION_TRANSFER,
                f"是否转账：{push_service.TRANSFER_LABELS.get(before['transfer_status'], '未转账')}"
                f" → {push_service.TRANSFER_LABELS.get(order.transfer_status, '未转账')}",
            )
        )
    new_sender = (order.sender_name or "").strip()
    if new_sender != before["sender_name"]:
        changes.append(
            (
                order_log_service.ACTION_SENDER,
                f"发送人：{before['sender_name'] or '（空）'} → {new_sender or '（清空）'}",
            )
        )
    new_tracking = (order.tracking_no or "").strip()
    if new_tracking != before["tracking_no"]:
        changes.append(
            (
                order_log_service.ACTION_TRACKING,
                f"物流单号：{before['tracking_no'] or '（空）'} → {new_tracking or '（清空）'}",
            )
        )
    if float(order.refund_amount or 0) != before["refund_amount"]:
        changes.append(
            (
                order_log_service.ACTION_REFUND_AMOUNT,
                f"退款给客户：{abs(before['refund_amount']):.2f} → "
                f"{abs(float(order.refund_amount or 0)):.2f}"
                f"（成交价 {float(order.deal_amount or 0):.2f}）",
            )
        )
    if float(order.supplier_refund_amount or 0) != before["supplier_refund_amount"]:
        changes.append(
            (
                order_log_service.ACTION_REFUND_AMOUNT,
                f"供应商退款：{before['supplier_refund_amount']:.2f} → "
                f"{float(order.supplier_refund_amount or 0):.2f}"
                f"（退给客户 {abs(float(order.refund_amount or 0)):.2f}）",
            )
        )
    if changes:
        for action, detail in changes:
            order_log_service.log(
                db, order, action, detail=detail, operator=_username(request)
            )
        db.commit()
    return _redirect(f"/orders/{order_id}", "已保存")


def _clear_refund_fields(order: Order) -> None:
    """取消退单标记时，把退单流程留下的痕迹一起清掉。

    以前只清了处理状态和关键词，退款金额 / 退回单号 / 是否退款这些都留着，
    恢复成正常订单后还挂着「已退款 800 元」，统计与展示看着像退单没取消干净。
    """
    order.refund_status = ""
    order.refund_keyword = ""
    order.refund_at = None
    order.refund_amount = 0.0
    order.return_tracking_no = ""
    order.is_received = False
    order.received_at = None
    order.refund_paid = "unpaid"
    order.refund_paid_at = None
    order.supplier_refunded = "unpaid"
    order.supplier_refunded_at = None
    order.refund_supplier_synced_at = None
    order.refund_handled_at = now()


def _apply_refund_status(
    db: Session, order: Order, status: str, note: str, actor: str
) -> str:
    """写入退单处理状态（手工维护表单与「一次保存」表单共用一套逻辑）。"""
    allowed = ("pending", "intercepted", "shipped", "refunded", "closed", "")
    if status not in allowed:
        status = "pending"
    refund_before = (
        REFUND_STATUS_LABELS.get((order.refund_status or "").strip(), "待拦截")
        if order.is_refund
        else "非退单"
    )
    # 已发货也受理退单（2026-09-29 口径）：没收到货的做物流拦截、收到货的维护退单物流单号，
    # 所以这里不再拦着「待拦截」—— 运营把处理进度如实记下来就行。
    shipped = order_state_service.is_shipped(order)

    if status == "":
        _clear_refund_fields(order)
    else:
        if not order.refund_at:
            order.refund_at = now()
        order.refund_status = status
        # 已发货但用户还没退回：记为「已发货无法拦截」，订单状态保持已发货
        if not order.refund_keyword:
            order.refund_keyword = "手工标记"
        if status != "pending":
            order.refund_handled_at = now()
    if note:
        order.refund_note = note
    if actor:
        order.refund_by = actor
    order.updated_at = now()
    db.commit()

    # 订单状态跟着退单处理走：已退款 / 已拦截 -> 已退单；取消标记 / 未受理 -> 按事实回算
    if status == "":
        order_state_service.refresh(db, order)
    elif status in ("refunded", "intercepted") or not shipped:
        order_state_service.advance(
            db, order, order_state_service.ORDER_STATE_REFUNDED, actor=actor
        )
    else:
        order_state_service.refresh(db, order)
    # 资金结算跟着退单走：已受理的退单不再结算，取消标记后恢复结算
    finance_service.sync_order(db, order, commit=True)
    refund_after = (
        REFUND_STATUS_LABELS.get((order.refund_status or "").strip(), "待拦截")
        if order.is_refund
        else "非退单"
    )
    if refund_after != refund_before:
        order_log_service.log(
            db,
            order,
            order_log_service.ACTION_REFUND_HANDLE,
            detail=f"退单处理：{refund_before} → {refund_after}" + (f"；备注：{note}" if note else ""),
            operator=actor,
            commit=True,
        )
    return REFUND_STATUS_LABELS.get(status, "已恢复")


@router.post("/orders/{order_id}/refund")
async def order_refund(request: Request, order_id: int, db: Session = Depends(get_db)):
    """逆向退单维护：标记退单 / 记录拦截处理结果（已拦截 / 已退款 / 已关闭）。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    status = str(form.get("refund_status", "")).strip()
    note = str(form.get("refund_note", "")).strip()
    label = _apply_refund_status(db, order, status, note, _username(request))
    return _redirect(f"/orders/{order_id}", f"退单状态已更新为「{label}」")


@router.post("/orders/{order_id}/refund/process")
async def order_refund_process(
    request: Request, order_id: int, db: Session = Depends(get_db)
):
    """退单处理「一次保存」：理由 / 处理状态 / 是否已收货 / 退回单号 / 退款金额 / 是否退款 / 备注。

    以前这七项散在 6 个表单里各自保存，改一项点一次、还容易漏掉一两项。
    现在一个表单一次提交：没改的项保持原样，改了的各记一条日志，改完按新的步骤重算下一步。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    if not (order.is_refund or order.is_recalled):
        return _redirect(f"/orders/{order_id}", "该订单不是退单，无需处理", "warn")
    form = await request.form()
    actor = _username(request)
    done: list[str] = []

    reason = str(form.get("reason", "")).strip()
    if reason and reason != (order.refund_reason or "").strip():
        ok, msg = refund_service.set_refund_reason(db, order, reason, actor=actor)
        if ok:
            done.append("退单理由")

    received_raw = str(form.get("received", "")).strip()
    if received_raw in ("0", "1"):
        want = received_raw == "1"
        if want != bool(order.is_received):
            ok, _ = refund_service.set_received(db, order, want, actor=actor)
            if ok:
                done.append("是否已收货")

    tracking = str(form.get("return_tracking_no", "")).strip()
    if tracking != (order.return_tracking_no or "").strip():
        ok, _ = refund_service.set_return_tracking_no(db, order, tracking, actor=actor)
        if ok:
            done.append("退单物流单号")

    amount_raw = str(form.get("refund_amount", "")).strip()
    if amount_raw != "":
        try:
            # 库里存的是负值，表单填的是正数：比绝对值才知道「有没有改」
            if abs(float(amount_raw) - abs(float(order.refund_amount or 0))) > 1e-6:
                ok, _ = refund_service.set_refund_amount(db, order, amount_raw, actor=actor)
                if ok:
                    done.append("退款金额")
        except ValueError:
            pass

    paid_raw = str(form.get("refund_paid", "")).strip()
    if paid_raw in ("0", "1"):
        want_paid = paid_raw == "1"
        if want_paid != ((order.refund_paid or "unpaid") == "paid"):
            ok, _ = refund_service.set_refund_paid(db, order, want_paid, actor=actor)
            if ok:
                done.append("是否退款")

    status = str(form.get("refund_status", "")).strip()
    note = str(form.get("refund_note", "")).strip()
    if status != (order.refund_status or "").strip() or note != (order.refund_note or "").strip():
        _apply_refund_status(db, order, status, note, actor)
        done.append("处理状态")

    if not done:
        return _redirect(f"/orders/{order_id}", "没有改动", "warn")
    nxt = refund_service.next_step_hint(order)
    return _redirect(f"/orders/{order_id}", f"已保存：{'、'.join(done)}。{nxt}")


@router.post("/orders/{order_id}/refund/stopped")
async def order_refund_stopped(
    request: Request, order_id: int, db: Session = Depends(get_db)
):
    """标记「已叫停」：不推给供应商 / 撤回发货 / 物流拦截（不指定动作就按当前路径自动选）。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    ok, msg = refund_service.mark_stopped(
        db,
        order,
        action=str(form.get("action", "")).strip(),
        actor=_username(request),
        note=str(form.get("note", "")).strip(),
    )
    return _redirect(f"/orders/{order_id}", msg, "" if ok else "warn")


@router.post("/orders/{order_id}/refund/return-received")
async def order_refund_return_received(
    order_id: int, request: Request, db: Session = Depends(get_db)
):
    """标记「已收到退回件」：客户寄回的货签收了（收到才可以退款给客户）。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    ok, msg = refund_service.mark_return_received(
        db, order, actor=_username(request)
    )
    return _redirect(f"/orders/{order_id}", msg, "" if ok else "warn")


@router.get("/orders/{order_id}/tracking", response_class=HTMLResponse)
def order_tracking(
    request: Request, order_id: int, force: str = "", db: Session = Depends(get_db)
):
    """物流轨迹：调菜鸟查这条运单的最新轨迹（订单详情「查物流」进来）。

    走的是三端共用的「物流轨迹服务」（services/logistics_trace）：查完在本地留一份，
    一小时之内再来查直接给存档，不再重复调菜鸟；``?force=1`` 才是强制实时查。

    查不到不会改订单任何东西 —— 轨迹只用来给人看，要不要落到订单状态上由人点确认。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    on_return = request.query_params.get("on_return") == "1"
    mail_no = ((order.return_tracking_no if on_return else order.tracking_no) or "").strip()
    if not mail_no:
        result = {"ok": False, "msg": "这个订单还没填运单号，先去「编辑订单」补上", "status": "", "nodes": []}
    else:
        result = logistics_trace.lookup(
            db,
            mail_no,
            phone=order.receiver_phone or "",
            force=force == "1",
        )
    return render(
        "order_tracking.html",
        request,
        order=order,
        on_return=on_return,
        mail_no=mail_no,
        result=result,
        tracking_labels=refund_service.TRACKING_STATUS_LABELS,
    )


@router.get("/orders/{order_id}/shipping", response_class=HTMLResponse)
def order_shipping(request: Request, order_id: int, db: Session = Depends(get_db)):
    """订单**发货**详情：只讲发货这一件事 —— 运单号 / 物流公司 / 物流轨迹 / 发货操作日志。

    跟订单详情分开：这里不堆订单的全量信息（价格、图片、推单文案都在订单详情里），
    想看订单本身点页面上的「订单详情」入口。供应链发货列表点进来的就是这一页。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    mail_no = (order.tracking_no or "").strip()
    # 公司名：库里存过就直接显示，老单子没存过的按单号现认一次
    company = (order.tracking_company or "").strip() or logistics_service.guess_company(
        mail_no
    )
    if mail_no:
        # 走存档服务：同一单号一小时之内不会重复调菜鸟（force=1 才是强制实时查）
        result = logistics_trace.lookup(
            db,
            mail_no,
            cp_name=company,
            phone=order.receiver_phone or "",
            force=request.query_params.get("force") == "1",
        )
    else:
        result = {
            "ok": False,
            "msg": "还没填物流单号：去「供应链发货」页填上，这里就能看到轨迹",
            "status": "",
            "nodes": [],
        }
    # 发货这条线上的操作日志：推单 + 回填/修改单号 + 通知发货群（退款、转账不在这儿凑热闹）
    logs = order_log_service.shipping_logs_for_order(db, order.id)
    can_edit = (not mail_no) or (
        (order.tracking_edit_count or 0) < push_service.TRACKING_MAX_EDIT
    )
    return render(
        "order_shipping.html",
        request,
        order=order,
        mail_no=mail_no,
        company=company,
        result=result,
        logs=logs,
        operator_label=order_log_service.operator_label,
        can_edit=can_edit,
        tracking_max_edit=push_service.TRACKING_MAX_EDIT,
        tracking_labels=refund_service.TRACKING_STATUS_LABELS,
        # 这页是从「供应链发货」列表点进来的，面包屑按那边算：供应链 / 供应链发货 / 发货详情
        crumb_path="/batches",
        crumb_leaf="发货详情",
    )


@router.post("/orders/{order_id}/tracking/apply")
async def order_tracking_apply(request: Request, order_id: int, db: Session = Depends(get_db)):
    """把菜鸟查到的状态同步到订单（要人点一下才写，自动改订单事实太危险）。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    on_return = str(form.get("on_return", "")).strip() == "1"
    ok, msg = refund_service.apply_tracking_status(
        db,
        order,
        status=str(form.get("status", "")).strip(),
        on_return=on_return,
        source=_username(request) or "菜鸟物流查询",
    )
    return _redirect(
        f"/orders/{order_id}/tracking{'?on_return=1' if on_return else ''}",
        msg,
        "" if ok else "warn",
    )


@router.post("/orders/{order_id}/tracking/status")
async def order_tracking_status(
    request: Request, order_id: int, db: Session = Depends(get_db)
):
    """物流轨迹状态回写（接入物流查询后由接口调用；现在手工也能改）。

    on_return=1 表示这是退回件的单号。轨迹只补事实（已收货 / 已收到退回件 / 已叫停），
    不改订单状态 —— 人工标记过的不会被覆盖。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    ok, msg = refund_service.apply_tracking_status(
        db,
        order,
        status=str(form.get("status", "")).strip(),
        on_return=str(form.get("on_return", "")).strip() == "1",
        source=_username(request) or "物流轨迹",
    )
    return _redirect(f"/orders/{order_id}", msg, "" if ok else "warn")


@router.post("/orders/{order_id}/refund/mark")
def order_mark_refund(order_id: int, request: Request, db: Session = Depends(get_db)):
    """「标记退单」：与群里同步退单走**同一条流程**，只是改成人工触发（系统没识别到时的补救入口）。

    标记后订单落到「买家申请退单」，之后走：补退单理由 -> 同步供应商 -> 退款 -> 关闭完成。
    已发货也受理：没收到货的同步物流拦截、已收到货的维护退单物流单号。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    if order.is_refund:
        return _redirect(f"/orders/{order_id}", "该订单已经是退单，无需重复标记", "warn")
    if order.is_recalled:
        return _redirect(
            f"/orders/{order_id}",
            "该订单已撤单（只留档）：不用再走退单流程",
            "warn",
        )
    if order.is_closed:
        # 已经走完流程的单再标退单，只会把已结算、已统计的口径搞乱（多半是点错了）
        return _redirect(
            f"/orders/{order_id}",
            "该订单已关闭完成：不用再走退单流程（确实要退请先恢复正常订单）",
            "warn",
        )
    refund_service.mark_refund(db, order, keyword="手工标记退单", actor=_username(request))
    return _redirect(
        f"/orders/{order_id}",
        "已标记为「买家申请退单」：请补退单理由，再点「同步供应商退单」推进",
        "",
    )


@router.post("/orders/{order_id}/restore")
def order_restore(order_id: int, request: Request, db: Session = Depends(get_db)):
    """「标记恢复正常订单」：系统误判（撤回 / 退单）时的人工补救入口。

    清掉撤单与退单标记，状态按订单上的事实重算，结算流水同步回滚。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    ok, msg = refund_service.restore_normal_order(
        db, order, actor=_username(request)
    )
    return _redirect(f"/orders/{order_id}", msg, "" if ok else "warn")


@router.post("/orders/{order_id}/refund/sync")
async def order_refund_sync_supplier(
    order_id: int, request: Request, db: Session = Depends(get_db)
):
    """运营把退单同步给供应商：状态 -> 「已同步供应商退单」。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    note = str(form.get("note", "")).strip()
    ok, msg = refund_service.mark_supplier_synced(
        db, order, actor=_username(request), note=note
    )
    return _redirect(f"/orders/{order_id}", msg, "" if ok else "warn")


@router.post("/orders/{order_id}/refund/reason")
async def order_refund_reason(
    order_id: int, request: Request, db: Session = Depends(get_db)
):
    """人工维护退单理由（群里没填 / 填错时补上）。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    reason = str(form.get("reason", "")).strip()
    ok, msg = refund_service.set_refund_reason(
        db, order, reason, actor=_username(request)
    )
    return _redirect(f"/orders/{order_id}", msg, "" if ok else "warn")


@router.post("/orders/{order_id}/refund/pay")
async def order_refund_pay(
    order_id: int, request: Request, db: Session = Depends(get_db)
):
    """「已退款给客户」一步到位：可以顺手改退款金额，点一次就记上退款。

    页面上这一步只让填金额、点确认（退款金额留空则按原值/成交价，不覆盖）。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    amount = str(form.get("refund_amount", "")).strip()
    if amount:
        ok, msg = refund_service.set_refund_amount(
            db, order, amount, actor=_username(request)
        )
        if not ok:
            return _redirect(f"/orders/{order_id}", msg, "warn")
    ok, msg = refund_service.set_refund_paid(db, order, True, actor=_username(request))
    if not ok:
        return _redirect(f"/orders/{order_id}", msg, "warn")
    return _redirect(f"/orders/{order_id}", f"{msg}。{refund_service.next_step_hint(order)}")


@router.post("/orders/{order_id}/refund/paid")
async def order_refund_paid(
    order_id: int, request: Request, db: Session = Depends(get_db)
):
    """维护「是否退款」（与「是否转账」同一套口径）。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    paid = str(form.get("paid", "")).strip() == "1"
    ok, msg = refund_service.set_refund_paid(db, order, paid, actor=_username(request))
    return _redirect(f"/orders/{order_id}", msg, "" if ok else "warn")


@router.post("/orders/{order_id}/refund/amount")
async def order_refund_amount(order_id: int, request: Request, db: Session = Depends(get_db)):
    """维护退款金额（默认按成交价，运营按实际退款额改）。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    ok, msg = refund_service.set_refund_amount(
        db, order, str(form.get("refund_amount", "")).strip(), actor=_username(request)
    )
    return _redirect(f"/orders/{order_id}", msg, "" if ok else "warn")


@router.post("/orders/{order_id}/refund/return-tracking")
async def order_refund_return_tracking(
    order_id: int, request: Request, db: Session = Depends(get_db)
):
    """维护**退单物流单号**：用户已收到货、把货寄回来的退回快递单号。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    ok, msg = refund_service.set_return_tracking_no(
        db, order, str(form.get("return_tracking_no", "")).strip(), actor=_username(request)
    )
    return _redirect(f"/orders/{order_id}", msg, "" if ok else "warn")


@router.post("/orders/{order_id}/refund/received")
async def order_refund_received(order_id: int, request: Request, db: Session = Depends(get_db)):
    """标记「用户已收到货」：决定这单退单是拦截快递还是等退回件。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    ok, msg = refund_service.set_received(
        db, order, str(form.get("received", "")).strip() == "1", actor=_username(request)
    )
    return _redirect(f"/orders/{order_id}", msg, "" if ok else "warn")


@router.post("/orders/{order_id}/refund/supplier-refunded")
async def order_supplier_refunded(
    order_id: int, request: Request, db: Session = Depends(get_db)
):
    """运营线下跟供应商对完账后「标记供应商已退款」：订单自动落到「关闭完成」。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    ok, msg = refund_service.mark_supplier_refunded(
        db,
        order,
        actor=_username(request),
        note=str(form.get("note", "")).strip(),
    )
    return _redirect(f"/orders/{order_id}", msg, "" if ok else "warn")


@router.post("/orders/{order_id}/refund/close")
async def order_refund_close(order_id: int, request: Request, db: Session = Depends(get_db)):
    """退单收尾「关闭完成」：按路径卡必需步骤（退款给客户 / 供应商退款 / 退回件）。

    缺哪一步就拦在哪一步；线下已经处理完只是系统里没记的，走 force=1 的「线下已退，
    强制关闭」—— 必须写明原因，留痕里记清楚是谁、因为什么强制关的。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    force = str(form.get("force", "")).strip() == "1"
    ok, msg = refund_service.close_refund_order(
        db,
        order,
        actor=_username(request),
        note=str(form.get("note", "")).strip(),
        force=force,
    )
    return _redirect(f"/orders/{order_id}", msg, "" if ok else "warn")


@router.get("/refund-todo", response_class=HTMLResponse)
def refund_todo_page(request: Request, db: Session = Depends(get_db)):
    """退单待办：还没关闭的退单，按「现在卡在哪一步」分组列出 —— 一组一个动作。

    与「退单数据体检」的分工：体检查的是历史数据对不对得上，
    这里是今天还欠哪些动作：打开就知道先干哪几票，能一键做的直接点按钮。
    """
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    groups = refund_service.refund_todo(db)
    total = sum(g["count"] for g in groups)
    total_amount = round(sum(g["amount"] for g in groups), 2)
    closable = next((g["count"] for g in groups if g["key"] == "closable"), 0)
    return render(
        "refund_todo.html",
        request,
        heading="退单待办",
        sub=f"未关闭的退单 {total} 笔：按「这一步还没做」分组，先做的排在前面",
        groups=groups,
        total=total,
        total_amount=total_amount,
        closable=closable,
    )


@router.get("/refund-health", response_class=HTMLResponse)
def refund_health(request: Request, db: Session = Depends(get_db)):
    """退单数据体检：把历史退单里「状态与事实对不上」的单列出来（只报不改）。

    新口径上线后老数据不动（改数据有风险），这里按单给出问题与建议动作，
    运营照着核对：真有问题的补录，本来线下就处理完的不用管。
    """
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    from ..services import cost_guard as cost_guard_service

    rows = refund_service.scan_anomalies(db)
    level_labels = {"high": "要处理", "mid": "要核对", "low": "可忽略"}
    for r in rows:
        r["level_label"] = level_labels.get(r["level"], r["level"])
    # 成本异常（比售价还高 / 是 0）：自动写入已经拦住了，历史遗留的在这里列出来人工核
    cost_rows = cost_guard_service.scan_cost_anomalies(db)
    return render(
        "refund_health.html",
        request,
        heading="数据体检",
        sub=f"退单对不上 {len(rows)} 条、成本异常 {len(cost_rows)} 条 —— 只提示，不自动改数据",
        rows=rows,
        high=sum(1 for r in rows if r["level"] == "high"),
        mid=sum(1 for r in rows if r["level"] == "mid"),
        low=sum(1 for r in rows if r["level"] == "low"),
        cost_rows=cost_rows,
    )


@router.post("/orders/{order_id}/refund/intercept")
def order_mark_intercept(order_id: int, request: Request, db: Session = Depends(get_db)):
    """一键「标记拦截」：供应商已发货、用户退款但货还没收到，把快递拦下退回。

    标记后订单落到「已退单」、退单处理状态记「已拦截」，结算流水同样作废。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    ok, msg = refund_service.mark_manual(
        db, order, refund_service.MARK_INTERCEPTED, actor=_username(request)
    )
    return _redirect(f"/orders/{order_id}", msg, "" if ok else "danger")


@router.post("/orders/{order_id}/merge")
async def order_merge(request: Request, order_id: int, db: Session = Depends(get_db)):
    """把另一笔订单并到本单上（典型场景：退单消息误建的那笔并回真正的订单）。

    以本单为主：对方订单上的收货信息 / 发送人 / 截图 / 退单标记会并过来，对方订单删除，
    主订单已有的成本、售价、供应商等口径保持不变。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    _require_block(request, "order_merge", "合并订单")
    form = await request.form()
    key = str(form.get("merge_order_no", "") or "").strip()
    src = merge_service.find_order(db, key)
    if src is None:
        return _redirect(f"/orders/{order_id}", f"没找到订单「{key}」", "danger")
    if src.id == order_id:
        return _redirect(f"/orders/{order_id}", "不能和自己合并", "danger")
    try:
        ok, msg = merge_service.merge_orders(db, src, order, actor=_username(request))
    except Exception as exc:  # noqa: BLE001  合并出错也给运营一句看得懂的话，别甩 500 空白页
        db.rollback()
        logger.exception("合并订单失败：%s 并入 #%s", key, order_id)
        return _redirect(f"/orders/{order_id}", f"合并失败：{exc}", "danger")
    if not ok:
        db.rollback()
        return _redirect(f"/orders/{order_id}", msg, "danger")
    db.commit()
    return _redirect(f"/orders/{order_id}", msg)


@router.post("/orders/{order_id}/delete")
def order_delete(order_id: int, db: Session = Depends(get_db)):
    order = db.get(Order, order_id)
    if order:
        db.delete(order)
        db.commit()
    return _redirect("/orders", "订单已删除", "warn")


@router.post("/orders/{order_id}/product-image")
def order_recrop_product_image(order_id: int, db: Session = Depends(get_db)):
    """按订单截图重裁商品图（裁剪算法升级后手动重跑单笔）。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    if order_recrop := ingest_service.recrop_product_image(db, order):
        return _redirect(
            f"/orders/{order_id}", "商品图已按订单截图重新裁取", ""
        )
    return _redirect(
        f"/orders/{order_id}",
        "没能裁出商品图：该订单还没有订单截图，或截图版式识别不到商品缩略图",
        "warn",
    )


@router.post("/orders/{order_id}/screenshot")
async def order_screenshot_upload(
    request: Request, order_id: int, db: Session = Depends(get_db)
):
    """订单详情页维护订单截图：上传 / 更新后重新解析补全字段。

    会先校验收货人一致性：截图解析出的收货人 / 手机号与订单对不上就拒绝，
    提示「订单截图上传不对，请重新上传」。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    upload = form.get("screenshot")
    name = getattr(upload, "filename", "") or ""
    if not name:
        return _redirect(f"/orders/{order_id}", "请先选择要上传的订单截图", "warn")
    data = await upload.read() if hasattr(upload, "read") else b""
    try:
        _ok, msg, kind = ingest_service.save_manual_screenshot(db, order, name, data)
    except Exception as exc:  # noqa: BLE001  上传失败不能让详情页 500
        db.rollback()
        return _redirect(f"/orders/{order_id}", f"订单截图上传失败：{exc}", "danger")
    return _redirect(f"/orders/{order_id}", msg, kind)


@router.post("/orders/{order_id}/recall")
def order_recall(order_id: int, request: Request, db: Session = Depends(get_db)):
    """手动把订单标记为「撤回 / 撤单」（群里撤单事件没收到时的兜底入口）。

    撤单订单只留档：不参与任何数据统计与后续流程，页面置灰显示。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    if order_state_service.is_shipped(order):
        return _redirect(
            f"/orders/{order_id}",
            f"该订单已发货（物流单号 {(order.tracking_no or '').strip()}）：不能撤单，"
            "请让用户收到货后走退货退款流程",
            "warn",
        )
    # 处理日志由 recall_service.mark_recalled 统一写（reason 里说明是运营手工标记）
    recall_service.mark_recalled(
        db, order, recaller=_username(request), reason="运营手工标记撤单"
    )
    return _redirect(
        f"/orders/{order_id}", "订单已标记为撤单：只留档，不参与统计与后续流程", ""
    )


@router.post("/orders/{order_id}/recall/undo")
def order_recall_undo(order_id: int, request: Request, db: Session = Depends(get_db)):
    """撤销撤回标记（群里重发 / 误撤回）：订单重新参与统计与后续流程。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    recall_service.undo_recalled(db, order)
    order_log_service.log(
        db,
        order,
        order_log_service.ACTION_RECALL_UNDO,
        detail="撤销撤回标记：订单重新参与统计与后续流程",
        operator=_username(request),
        commit=True,
    )
    return _redirect(f"/orders/{order_id}", "已撤销撤回标记，订单重新参与统计与流程", "")


@router.post("/orders/{order_id}/reparse")
def order_reparse(order_id: int, db: Session = Depends(get_db)):
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    msg = db.scalar(
        select(RawMessage).where(RawMessage.message_id == order.source_message_id)
    )
    db.delete(order)
    db.commit()
    if msg:
        msg.parse_status = "pending"
        msg.ignore_reason = ""
        db.commit()
        process_pending(db)
    return _redirect("/orders", "已重新解析", "")


@router.post("/orders/{order_id}/reocr")
def order_reocr(order_id: int, db: Session = Depends(get_db)):
    """详情页「重新识别截图」：重跑截图 OCR，读出订单号就把临时编号升级掉。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    upgraded, note = ingest_service.reocr_order(db, order)
    return _redirect(f"/orders/{order_id}", note, "" if upgraded else "warn")


@router.post("/orders/{order_id}/apply-order-no")
async def order_apply_order_no(request: Request, order_id: int, db: Session = Depends(get_db)):
    """采用截图里的疑似订单号（人工确认后一键升级临时编号）。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    real_no = str(form.get("order_no", "")).strip() or ingest_service.order_no_hint_of(db, order)
    if not real_no:
        return _redirect(f"/orders/{order_id}", "没有可采用的订单号：请先重新识别或手动填写", "warn")
    order = ingest_service.apply_real_order_no(db, order, real_no)
    order.needs_review = False
    order.review_reason = ""
    order.updated_at = now()
    order.fingerprint = dedupe_service.order_fingerprint(order)
    db.commit()
    return _redirect(f"/orders/{order_id}", f"已采用订单号 {order.order_no}，待确认标记已清除")


@router.post("/orders/batch-create")
async def orders_batch_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("order_ids")]
    supplier_name = str(form.get("supplier_name", "")).strip()
    if not ids:
        return _redirect("/orders", "请先选择订单", "warn")
    if not supplier_name:
        supplier_name = str(
            db.scalar(select(Order.supplier_name).where(Order.id == ids[0])) or "未指定供应商"
        )
    batch = supplier_service.create_batch(db, supplier_name, ids)
    return _redirect(f"/batches/{batch.id}", f"已生成批次 {batch.batch_no}")


@router.post("/orders/mark-reviewed")
async def orders_mark_reviewed(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(x) for x in form.getlist("order_ids")]
    if not ids:
        return _redirect("/orders", "请先选择订单", "warn")
    db.query(Order).filter(Order.id.in_(ids)).update(
        {"needs_review": False}, synchronize_session=False
    )
    db.commit()
    return _redirect("/orders", f"已将 {len(ids)} 个订单标记为已确认")


@router.post("/orders/auto-batch")
def orders_auto_batch(db: Session = Depends(get_db)):
    batches = supplier_service.auto_batch_by_supplier(db)
    if not batches:
        return _redirect("/batches", "没有待推单发货的订单", "warn")
    return _redirect("/batches", f"已按供应商生成 {len(batches)} 个批次")


# ------------------------------------------------------------------ 流程说明
@router.get("/help", response_class=HTMLResponse)
def help_index():
    """流程说明总入口：默认给推单流程（动作最多、最容易走偏的那个）。"""
    return RedirectResponse("/help/push")


@router.get("/help/{topic_key}", response_class=HTMLResponse)
def help_topic(request: Request, topic_key: str):
    """某个模块的「流程 + 状态」说明（推单 / 退单 / 订单）。

    文案统一放在 app/services/help.py，页面上的提示与说明页同源 —— 流程改了只改那一处。
    """
    doc = help_service.topic(topic_key)
    if not doc:
        raise HTTPException(404, "没有这份说明")
    return render(
        "help.html",
        request,
        heading=doc["title"],
        doc=doc,
        topics=help_service.all_topics(),
        topic_key=topic_key,
    )


# ------------------------------------------------------------------ 推单批次
def _same_query(request: Request, path: str = "", **overrides) -> str:
    """当前地址 + 只改几个参数（「按天 / 按月」切换用）：筛选条件不丢，换完还能接着筛。

    path 给了就换到那个地址（如从统计子页面跳回列表页），条件照样带着走。
    """
    params = {k: v for k, v in request.query_params.items() if str(v or "") != ""}
    params.update({k: v for k, v in overrides.items()})
    query = urlencode(params)
    base = path or request.url.path
    return f"{base}?{query}" if query else str(base)


def _stats_url(request: Request) -> str:
    """「发货统计」子页面地址：把当前筛选条件原样带过去（与列表同一份口径）。

    按天 / 按月的明细表单独成页（2026-10-11），供应链发货页只留「当前筛选统计」四个数，
    点这个按钮进去看走势 —— 带着当前条件走，不用重新筛一遍。
    """
    params = {k: v for k, v in request.query_params.items() if str(v or "") != ""}
    query = urlencode(params)
    return f"/batches/shipping-stats?{query}" if query else "/batches/shipping-stats"


@router.get("/batches", response_class=HTMLResponse)
def batches(
    request: Request,
    view: str = "pushed",
    status: str = "",
    q: str = "",
    supplier: str = "",
    transfer: str = "",
    tracking: str = "",
    show_closed: str = "",
    date_from: str = "",
    date_to: str = "",
    date_field: str = push_service.DEFAULT_TIME_FIELD,
    range_key: str = Query("", alias="range"),
    group: str = "day",
    db: Session = Depends(get_db),
):
    """供应链发货：默认展示「已推单记录」（可维护物流单号），view=history 看历史批次。

    单笔推单模式下批次已暂停，历史批次数据仍保留可查可导出。

    「已关闭完成」的订单默认不列 —— 它们钱货都处理完了，留在推单列表里只会让人以为
    还要催回单、填单号；页面顶部给出数量与「显示已关闭 / 已完成」开关，想翻旧账随时展开。

    时间维度（2026-10-06）：date_from / date_to 是**北京时间**的起止日期（YYYY-MM-DD），
    date_field 决定按哪个时间算（推单 / 发货 / 下单），range_key 是「今天 / 近 7 天」这类
    快捷区间 —— 快捷优先转成具体日期填回框里，用户还能手动改，不用记参数名。
    """
    if view == "history":
        stmt = db.query(PushBatch)
        if status:
            stmt = stmt.filter(PushBatch.status == status)
        items = stmt.order_by(PushBatch.id.desc()).all()
        return render(
            "batches.html",
            request,
            heading="供应链发货 · 历史批次",
            sub=f"共 {len(items)} 个批次",
            view="history",
            batches=items,
            status=status,
            pending=supplier_service.count_pending_orders(db),
        )

    # —— 时间维度：快捷区间先翻译成具体日期，页面上日期框里就能看到、也能手动改 ——
    if range_key in push_service.RANGE_PRESETS and not (date_from or date_to):
        date_from, date_to = push_service.range_days(range_key)
    if date_field not in push_service.TIME_FIELDS:
        date_field = push_service.DEFAULT_TIME_FIELD
    if group not in ("day", "month"):
        group = "day"
    has_date = bool(date_from or date_to)

    # 已关闭完成的单子默认收起（无需再操作），这里给出数量与展开入口
    closed_count = push_service.closed_pushed_count(
        db,
        q=q,
        supplier=supplier,
        transfer=transfer,
        tracking=tracking,
        date_from=date_from,
        date_to=date_to,
        time_field=date_field,
    )
    rows = push_service.pushed_orders(
        db,
        q=q,
        supplier=supplier,
        transfer=transfer,
        tracking=tracking,
        include_closed=bool(show_closed),
        date_from=date_from,
        date_to=date_to,
        time_field=date_field,
    )
    # 待填回单：数据口径与首页「待供应商回单」、左侧菜单徽标完全一致
    # （以前是在本页把「物流单号为空」的行数一遍，把退单也算进去了，数字和首页对不上）
    filtered_missing = push_service.pending_tracking_count(
        db,
        q=q,
        supplier=supplier,
        transfer=transfer,
        tracking=tracking,
        date_from=date_from,
        date_to=date_to,
        time_field=date_field,
    )
    # 顶部三张卡片走全量口径（不随筛选变）：以前用当前列表行数，点了「待填回单」以后
    # 「已推单」跟着从 93 掉到 21，看着像数据丢了；筛选出多少条改在列表上方说清楚。
    overview = push_service.pushed_overview(db)
    has_filter = any((q, supplier, transfer, tracking, date_from, date_to))
    # 当前筛选下的单量 / 金额与按时间维度的走势：**与列表同一份筛选条件**，
    # 所以「列表 12 条」和「统计 12 单」永远一致（列表有 500 条上限，统计走 SQL 汇总不会被截）
    list_conds = push_service.pushed_filters(
        db,
        q=q,
        supplier=supplier,
        transfer=transfer,
        tracking=tracking,
        date_from=date_from,
        date_to=date_to,
        time_field=date_field,
    )
    if not show_closed:
        list_conds.append(Order.order_state != push_service.ORDER_STATE_CLOSED)
    stats = push_service.pushed_stats(db, list_conds)
    supplier_names = [
        r[0]
        for r in db.execute(
            select(Order.supplier_name)
            .where(Order.supplier_name != "", Order.pushed_at.is_not(None))
            .group_by(Order.supplier_name)
        ).all()
    ]
    return render(
        "batches.html",
        request,
        heading="供应链发货",
        sub=(
            f"已推单记录 {overview['pushed']} 单 · 供应商已发货 {overview['shipped']} 单"
            f" · 待填回单 {overview['pending']} 单"
        ),
        view="pushed",
        orders=rows,
        suppliers=supplier_names,
        filters={
            "q": q,
            "supplier": supplier,
            "transfer": transfer,
            "tracking": tracking,
            "date_from": date_from,
            "date_to": date_to,
        },
        # —— 时间筛选 / 统计用到的菜单与快捷链接（模板只负责渲染，口径都在 push 服务里）——
        date_field=date_field,
        time_fields=push_service.TIME_FIELDS,
        range_key=range_key,
        range_presets=push_service.RANGE_PRESETS,
        range_links={
            key: push_service.range_days(key) for key in push_service.RANGE_PRESETS
        },
        today=beijing_now().strftime("%Y-%m-%d"),
        group=group,
        has_date=has_date,
        stats=stats,
        # 按天 / 按月的走势明细单独成页了（2026-10-11），入口带着当前筛选条件
        stats_url=_stats_url(request),
        pending=supplier_service.count_pending_orders(db),
        pushed_count=overview["pushed"],
        shipped_count=overview["shipped"],
        missing_tracking=overview["pending"],
        filtered_count=len(rows),
        filtered_missing=filtered_missing,
        has_filter=has_filter,
        tracking_max_edit=push_service.TRACKING_MAX_EDIT,
        # 老单号（填的时候还没记公司）在页面上现认一次，不回写库
        guess_company=logistics_service.guess_company,
        # 每行显示「保存记录 / 通知记录」：一次 SQL 把所有订单的发货日志捞出来分组
        logs_by_order=order_log_service.shipping_logs_for_orders(
            db, [o.id for o in rows]
        ),
        notify_enabled=settings.notify_mode != "off",
        show_closed=bool(show_closed),
        closed_count=closed_count,
    )


@router.get("/batches/shipping-stats", response_class=HTMLResponse)
def shipping_stats(
    request: Request,
    q: str = "",
    supplier: str = "",
    transfer: str = "",
    tracking: str = "",
    show_closed: str = "",
    date_from: str = "",
    date_to: str = "",
    date_field: str = push_service.DEFAULT_TIME_FIELD,
    range_key: str = Query("", alias="range"),
    group: str = "day",
    export: str = "",
    db: Session = Depends(get_db),
):
    """发货统计（子页面，2026-10-11）：按时间维度看「这段时间发了多少单、成本多少」。

    与「供应链发货」（/batches）**共用一套筛选条件与统计口径**（同一份 conds），所以
    「列表 N 条」和「统计 N 单」永远对得上；只是把按天 / 按月的走势表单独搬到这里，
    页面能放下更多行、也能把筛选条件排得更开（列表页只留四个汇总数）。

    时间口径三选一（推单 / 发货 / 下单），日期是**北京时间**的自然日，含当天 00:00 到 23:59。
    """
    # —— 与列表页完全一致的筛选翻译（快捷区间 → 日期；口径与分组兜底）——
    if range_key in push_service.RANGE_PRESETS and not (date_from or date_to):
        date_from, date_to = push_service.range_days(range_key)
    if date_field not in push_service.TIME_FIELDS:
        date_field = push_service.DEFAULT_TIME_FIELD
    if group not in ("day", "month"):
        group = "day"
    has_date = bool(date_from or date_to)
    # 「按天 / 按月」切换：保留当前的筛选条件，只换 group（换完还能接着筛）
    trend_urls = {
        "day": _same_query(request, group="day"),
        "month": _same_query(request, group="month"),
    }

    conds = push_service.pushed_filters(
        db,
        q=q,
        supplier=supplier,
        transfer=transfer,
        tracking=tracking,
        date_from=date_from,
        date_to=date_to,
        time_field=date_field,
    )
    if not show_closed:
        conds.append(Order.order_state != push_service.ORDER_STATE_CLOSED)
    stats = push_service.pushed_stats(db, conds)
    trend = push_service.pushed_trend(db, conds, time_field=date_field, group=group)

    if export == "csv":
        head = ["日期" if group == "day" else "月份", "发货单", "已发货", "待回单",
                "采购成本", "单均成本", "销售额"]
        lines = [",".join(head)]
        for t in trend:
            lines.append(
                ",".join(
                    str(x)
                    for x in (t["period"], t["count"], t["shipped"], t["pending"],
                              t["cost"], t["avg_cost"], t["sale"])
                )
            )
        name = f"shipping-stats-{group}-{date_from or 'all'}-{date_to or 'all'}.csv"
        return Response(
            # ﻿（BOM）：Excel 打开中文表头不乱码
            content="\ufeff" + "\n".join(lines) + "\n",
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={name}"},
        )

    supplier_names = [
        r[0]
        for r in db.execute(
            select(Order.supplier_name)
            .where(Order.supplier_name != "", Order.pushed_at.is_not(None))
            .group_by(Order.supplier_name)
        ).all()
    ]
    closed_count = push_service.closed_pushed_count(
        db,
        q=q,
        supplier=supplier,
        transfer=transfer,
        tracking=tracking,
        date_from=date_from,
        date_to=date_to,
        time_field=date_field,
    )
    field_label = push_service.TIME_FIELDS[date_field][0]
    return render(
        "shipping_stats.html",
        request,
        heading="发货统计",
        sub=(
            f"按{field_label} · {date_from or '最早'} ~ {date_to or '今天'}，"
            f"{'按天' if group == 'day' else '按月'}共 {len(trend)} 行"
        ),
        orders_count=stats["count"],
        # —— 筛选区（与供应链发货页同一套参数，改完不用重新筛）——
        filters={
            "q": q,
            "supplier": supplier,
            "transfer": transfer,
            "tracking": tracking,
            "date_from": date_from,
            "date_to": date_to,
        },
        suppliers=supplier_names,
        date_field=date_field,
        time_fields=push_service.TIME_FIELDS,
        range_key=range_key,
        range_presets=push_service.RANGE_PRESETS,
        range_links={
            key: push_service.range_days(key) for key in push_service.RANGE_PRESETS
        },
        today=beijing_now().strftime("%Y-%m-%d"),
        group=group,
        has_date=has_date,
        trend_urls=trend_urls,
        stats=stats,
        trend=trend,
        show_closed=bool(show_closed),
        closed_count=closed_count,
        # 导出 / 返回：都带着当前筛选条件，来回切不丢
        export_url=_same_query(request, export="csv"),
        back_url=_same_query(request, "/batches"),
    )


@router.post("/orders/{order_id}/sync-wms")
async def order_sync_wms(order_id: int, request: Request, db: Session = Depends(get_db)):
    """渔货栈自营商品 → 渔集「发货管理」（2026-10-15 定稿）。

    口径：origin=self 的渔货栈自营单，**或供应链是渔货栈（YS001 / 名称带
    「渔货栈」）的订单**，视为渔货栈自营商品 —— 推单不走外部供应商：
    ① 渔集建一张**发货单**（主表字段按用户定稿：平台 / 平台单号 / 店铺 /
       收货四级地址 / 发货时限 / 金额参考 / 备注 / 版本号）；
    ② 再建一张销售出库单挂到发货单上，仓库按单拣货发货；
    ③ 出库核销填了运单号会自动回填到发货页（已推单记录）。
    幂等：发货单按平台原始单号幂等，重推字段有变 version 递增。
    """
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    if not push_service.is_self_supply_chain(db, order):
        return _redirect(
            f"/orders/{order_id}",
            "只有渔货栈自营商品（自营单 / 供应链是渔货栈YS001）才同步到渔集发货，"
            "其它订单走推单流程",
            "warn",
        )
    if order.is_refund or order.is_recalled:
        return _redirect(f"/orders/{order_id}", "退单 / 撤回的订单不同步发货", "warn")
    try:
        from wms.db import SessionLocal as _WmsSession
        from wms.models import WmsOutboundOrder as _WmsOutboundOrder
        from wms import delivery as wms_delivery_service
        from wms import outbound as wms_outbound_service
    except Exception:
        logger.exception("渔集子系统不可用")
        return _redirect(
            f"/orders/{order_id}", "渔集子系统不可用，暂时无法同步发货", "danger"
        )

    codes: list[str] = []
    if order.product_id:
        _p = db.get(Product, order.product_id)
        if _p and (_p.code or "").strip():
            codes.append(str(_p.code).strip())
    _shop = shop_service.find_by_name(db, order.xianyu_account)

    with _WmsSession() as ws:
        # 发货单：按平台原始单号幂等，重推字段有变 version 递增
        delivery_row, changed = wms_delivery_service.upsert_from_hub(
            ws,
            wms_delivery_service.build_payload(order, shop_code=_shop.code if _shop else ""),
            operator=_username(request) or "渔枢同步",
        )
        if delivery_row.outbound_id and not changed:
            _ob = ws.get(_WmsOutboundOrder, delivery_row.outbound_id)
            return _redirect(
                f"/orders/{order_id}",
                f"这张订单已经同步过：发货单 {delivery_row.delivery_no}"
                + (f" / 出库单 {_ob.order_no}" if _ob else "")
                + "（不会重复建单）",
                "warn",
            )
        outbound_row = None
        if delivery_row.outbound_id:
            # 重推只更新发货单字段（version 递增），出库单照旧不重建
            _ob = ws.get(_WmsOutboundOrder, delivery_row.outbound_id)
            order_log_service.log(
                db,
                order,
                order_log_service.ACTION_STATE,
                detail=(
                    f"渔集发货单已更新：{delivery_row.delivery_no} v{delivery_row.version}"
                    + (f"（出库单 {_ob.order_no} 照旧）" if _ob else "")
                ),
                operator=_username(request),
                commit=True,
            )
            return _redirect(
                f"/orders/{order_id}",
                f"重推生效：发货单 {delivery_row.delivery_no} 已更新到 v{delivery_row.version}"
                "（出库单照旧，不重复建）",
            )
        remark_parts = [
            f"产品：{order.product_name or '-'}",
            f"规格：{order.spec or '-'}",
            f"数量：{order.quantity or 1} 台",
        ]
        if order.buyer_nick:
            remark_parts.append(f"买家：{order.buyer_nick}")
        if codes:
            remark_parts.append("渔货号：" + "、".join(codes))
        remark_parts.append(f"发货单 {delivery_row.delivery_no}")
        outbound_row = wms_outbound_service.create(
            ws,
            {
                "type": "SALE",
                "target_no": order.order_no,
                "warehouse_code": "SALE",
                "receiver_name": order.receiver_name or "",
                "receiver_phone": order.receiver_phone or "",
                "receiver_addr": order.receiver_address or "",
                "remark": "；".join(remark_parts),
            },
            operator=_username(request) or "渔枢同步",
        )
        wms_delivery_service.attach_outbound(ws, delivery_row, outbound_row)
    order_log_service.log(
        db,
        order,
        order_log_service.ACTION_STATE,
        detail=(
            f"已同步渔集发货：发货单 {delivery_row.delivery_no}"
            f" / 出库单 {outbound_row.order_no}"
        ),
        operator=_username(request),
        commit=True,
    )
    return _redirect(
        f"/orders/{order_id}",
        f"已同步到渔集发货管理：发货单 {delivery_row.delivery_no}"
        f" / 出库单 {outbound_row.order_no}，仓库按单拣货发货"
        "（出库后物流单号自动回填到发货页）",
    )


@router.post("/batches/tracking/{order_id}")
async def order_tracking_update(order_id: int, request: Request, db: Session = Depends(get_db)):
    """维护已推单记录的物流单号；填写后自动在群里同步给订单发送人。"""
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    form = await request.form()
    tracking_no = str(form.get("tracking_no", "") or "").strip()
    sync = str(form.get("sync", "") or "") not in ("", "0", "off")
    back = str(form.get("back", "") or "/batches")

    before_tracking = (order.tracking_no or "").strip()
    changed = tracking_no and tracking_no != before_tracking
    # 单号真的变了才写处理日志（原样提交不算一次操作）
    tracking_changed = tracking_no != before_tracking
    # 已填过单号的最多只能再改一次：改单号会在群里 @ 发送人，来回改会刷屏、也给对账留多个版本。
    # 首次填写（空 -> 有值）不计入，所以「填错一次还能救回来」，第二次开始锁死。
    if (
        tracking_changed
        and before_tracking
        and (order.tracking_edit_count or 0) >= push_service.TRACKING_MAX_EDIT
    ):
        return _redirect(
            back,
            f"物流单号已经改过 {push_service.TRACKING_MAX_EDIT} 次，不能再改了（当前：{before_tracking}）；"
            "确实填错请联系管理员处理",
            "warn",
        )
    order.tracking_no = tracking_no
    # 物流公司按单号规则自动认（顺丰速运 / 圆通速递 …），清空单号就一起清掉
    order.tracking_company = logistics_service.guess_company(tracking_no)
    if tracking_changed and before_tracking:
        order.tracking_edit_count = (order.tracking_edit_count or 0) + 1
    order.updated_at = now()
    # 订单状态：上游给了物流单号 -> 已发货；清空单号 -> 按事实回退（回到已推单）
    if tracking_no:
        order_state_service.advance(db, order, order_state_service.ORDER_STATE_SHIPPED)
    else:
        order_state_service.refresh(db, order)
    # 同步更新本订单的物流单号字段：订单详情、批次明细与导出一起跟着变
    for item in db.scalars(
        select(PushBatchItem).where(PushBatchItem.order_id == order.id)
    ).all():
        item.tracking_no = tracking_no
    db.commit()

    # 订单处理日志：手动回填 / 清空物流单号也算一次状态动作
    if tracking_changed:
        order_log_service.log(
            db,
            order,
            order_log_service.ACTION_TRACKING,
            detail=(
                f"物流单号：{before_tracking or '（空）'} → {tracking_no or '（清空）'}"
                + (f"（{order.tracking_company}）" if order.tracking_company else "")
            ),
            operator=_username(request),
            commit=True,
        )

    # 物流单号被维护/更新 -> 语音播报「供应商已发货，请及时完成店铺发货」
    if changed:
        voice_service.tracking_event(db, order)

    operator = _username(request)
    # 「改单号」（不是首次填写）时，群里文案要写明是更新 —— 否则看的人以为又发了一单
    is_update = bool(before_tracking) and tracking_no != before_tracking

    def log_notify(detail: str) -> None:
        """「要不要通知群 / 通知结果」都落一条日志：列表的通知记录、发货详情都读它。"""
        order_log_service.log(
            db,
            order,
            order_log_service.ACTION_TRACKING_NOTIFY,
            detail=detail,
            operator=operator,
            commit=True,
        )

    if not sync:
        log_notify(f"未勾选同步到群，只存档：{tracking_no or '（已清空）'}")
        return _redirect(back, f"物流单号已保存（未同步群）：{tracking_no or '已清空'}", "warn")
    if not tracking_no:
        log_notify("单号被清空，空单号不同步群")
        return _redirect(back, "物流单号已清空（空单号不同步群）", "warn")

    status, error = sync_tracking_to_group(order, session=db, is_update=is_update)
    if status == "sent":
        db.commit()
        log_notify(
            f"已通知发货群{'（单号更新）' if is_update else ''}：{tracking_no}"
            + (f"；{error}" if error else "")
        )
        if error:
            # 群里同步成功，但没 @ 上原发送人（订单来源不是群成员消息）：提示人工 @ 一下
            return _redirect(back, f"物流单号已保存并同步到群：{tracking_no}，{error}", "warn")
        return _redirect(back, f"物流单号已保存并同步到群：{tracking_no}")
    db.commit()
    if status == "skipped":
        log_notify(f"未通知发货群：{error}")
        return _redirect(back, f"物流单号已保存，但未同步到群：{error}", "warn")
    log_notify(f"通知发货群失败：{error}")
    return _redirect(back, f"物流单号已保存，但群同步失败：{error}", "danger")


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(PushBatch, batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    rows = supplier_service.batch_rows(db, batch)
    return render(
        "batch_detail.html",
        request,
        heading=f"批次 {batch.batch_no}",
        sub=f"{batch.supplier_name} · {batch.item_count} 单",
        batch=batch,
        rows=rows,
    )


@router.post("/batches/{batch_id}/status")
async def batch_status(request: Request, batch_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    status = str(form.get("status", ""))
    supplier_service.set_batch_status(db, batch_id, status)
    return _redirect(f"/batches/{batch_id}", f"批次状态已更新为 {status}")


@router.post("/batches/{batch_id}/items/{order_id}")
async def batch_item_update(
    request: Request, batch_id: int, order_id: int, db: Session = Depends(get_db)
):
    form = await request.form()
    supplier_service.update_item(
        db,
        batch_id,
        order_id,
        str(form.get("supplier_order_no", "")).strip(),
        str(form.get("tracking_no", "")).strip(),
    )
    return _redirect(f"/batches/{batch_id}", "已更新")


@router.get("/batches/{batch_id}/export.csv")
def batch_export_csv(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(PushBatch, batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    data = supplier_service.export_csv(db, batch)
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={batch.batch_no}.csv"},
    )


@router.get("/batches/{batch_id}/export.xlsx")
def batch_export_xlsx(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(PushBatch, batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    data = supplier_service.export_xlsx(db, batch)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={batch.batch_no}.xlsx"},
    )


# ------------------------------------------------------------------ 店铺档案
@router.get("/shops", response_class=HTMLResponse)
def shops(
    request: Request,
    q: str = "",
    operator: str = "",
    status: str = "",
    audit: str = "",
    shop_type: str = "",
    nature: str = "",
    db: Session = Depends(get_db),
):
    rows = shop_service.list_shops(
        db, q=q, operator=operator, status=status, audit=audit, shop_type=shop_type,
        nature=nature,
    )
    stats = shop_service.stats_by_account(db)
    pending = shop_service.count_shops(db, audit=SHOP_AUDIT_PENDING)
    sub = f"共 {len(rows)} 个闲鱼自营账号"
    if pending:
        sub += f"：{pending} 个渔货栈提交的店铺待审核"
    return render(
        "shops.html",
        request,
        heading="店铺管理",
        sub=sub,
        shops=rows,
        stats=stats,
        total=len(rows),
        pending_audit=pending,
        operators=shop_service.distinct_values(db, Shop.operator),
        statuses=shop_service.distinct_values(db, Shop.status),
        audits=SHOP_AUDIT_CHOICES,
        audit_text=SHOP_AUDIT_TEXT,
        shop_types=SHOP_TYPE_CHOICES,
        shop_natures=SHOP_NATURE_CHOICES,
        filters={
            "q": q,
            "operator": operator,
            "status": status,
            "audit": audit,
            "shop_type": shop_type,
            "nature": nature,
        },
        shop_label=shop_service.shop_label,
        linked_names=shop_service.linked_user_names(db, rows),
    )


@router.post("/shops")
async def shop_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        row = shop_service.create_shop(db, form)
    except ValueError as exc:
        return _redirect("/shops", str(exc), "warn")
    return _redirect("/shops", f"已添加店铺「{shop_service.shop_label(row)}」")


@router.post("/shops/backfill")
def shop_backfill(db: Session = Depends(get_db)):
    """按历史订单补齐店铺档案：没建档的自动建档，建档了的补历史信息。"""
    out = shop_service.backfill_from_orders(db)
    return _redirect(
        "/shops",
        f"已按历史订单补齐：新建 {out['created']} 个店铺、补充信息 {out['enriched']} 个",
    )


@router.get("/shops/{shop_id}", response_class=HTMLResponse)
def shop_detail(request: Request, shop_id: int, db: Session = Depends(get_db)):
    row = db.get(Shop, shop_id)
    if not row:
        raise HTTPException(404, "店铺不存在")
    return render(
        "shop_detail.html",
        request,
        heading=shop_service.shop_label(row),
        sub=" · ".join(x for x in (row.operator, row.category) if x) or "店铺档案",
        shop=row,
        stat=shop_service.shop_stat(db, row),
        orders=shop_service.shop_orders(db, row, 10),
        audits=SHOP_AUDIT_CHOICES,
        audit_text=SHOP_AUDIT_TEXT,
        shop_types=SHOP_TYPE_CHOICES,
        shop_natures=SHOP_NATURE_CHOICES,
        linked_name=shop_service.linked_user_names(db, [row]).get(
            int(row.self_linked_user_id or 0), ""
        ),
    )


@router.post("/shops/{shop_id}/audit")
async def shop_audit(request: Request, shop_id: int, db: Session = Depends(get_db)):
    """审核渔货栈提交的店铺：只有「已通过」的店铺才在渔货栈生效。"""
    form = await request.form()
    status = str(form.get("audit_status") or "").strip()
    note = str(form.get("audit_note") or "").strip()
    try:
        row = shop_service.set_audit(db, shop_id, status, note)
    except ValueError as exc:
        return _redirect("/shops", str(exc), "warn")
    if row.audit_status == SHOP_AUDIT_PENDING:
        return _redirect(f"/shops/{shop_id}", "已退回待审核")
    return _redirect(f"/shops/{shop_id}", f"审核结果：{row.audit_text}")


@router.post("/shops/{shop_id}/edit")
async def shop_edit(request: Request, shop_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        shop_service.update_shop(db, shop_id, form)
    except ValueError as exc:
        return _redirect(f"/shops/{shop_id}", str(exc), "warn")
    return _redirect(f"/shops/{shop_id}", "已保存")


@router.post("/shops/{shop_id}/delete")
def shop_delete(shop_id: int, db: Session = Depends(get_db)):
    shop_service.delete_shop(db, shop_id)
    return _redirect("/shops", "店铺已删除", "warn")


# ------------------------------------------------------------------ 供应商档案
@router.get("/suppliers", response_class=HTMLResponse)
def suppliers(
    request: Request,
    q: str = "",
    city: str = "",
    category: str = "",
    group: str = "",
    db: Session = Depends(get_db),
):
    rows = supplier_service.list_suppliers(db, q=q, city=city, category=category, group=group)
    stats = supplier_service.order_stats_by_name(db)
    # 统计键是统一展示名（名称（编码））：编码 / 名称写法都能算到同一家
    total_sale = sum(
        stats.get(supplier_service.supplier_label(r), {}).get("sale", 0.0) for r in rows
    )
    return render(
        "suppliers.html",
        request,
        heading="供应商管理",
        sub=f"共 {len(rows)} 家供应商",
        suppliers=rows,
        stats=stats,
        total=len(rows),
        total_sale=total_sale,
        cities=supplier_service.distinct_values(db, Supplier.city),
        categories=supplier_service.distinct_values(db, Supplier.category),
        filters={"q": q, "city": city, "category": category, "group": group},
    )


@router.post("/suppliers")
async def supplier_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        row = supplier_service.create_supplier(db, form)
    except ValueError as exc:
        return _redirect("/suppliers", str(exc), "warn")
    return _redirect("/suppliers", f"已添加供应商「{supplier_service.supplier_label(row)}」")


@router.get("/suppliers/{supplier_id}", response_class=HTMLResponse)
def supplier_detail(request: Request, supplier_id: int, db: Session = Depends(get_db)):
    row = db.get(Supplier, supplier_id)
    if not row:
        raise HTTPException(404, "供应商不存在")
    # 订单里写编码、名称或「名称（编码）」都算这家供应商的
    stat = supplier_service.supplier_stat(db, row)
    return render(
        "supplier_detail.html",
        request,
        heading=supplier_service.supplier_label(row),
        sub=" · ".join(x for x in (row.city, row.category) if x) or "供应商档案",
        supplier=row,
        stat=stat,
        orders=supplier_service.supplier_orders(db, row, 10),
    )


@router.post("/suppliers/{supplier_id}/edit")
async def supplier_edit(request: Request, supplier_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        supplier_service.update_supplier(db, supplier_id, form)
    except ValueError as exc:
        return _redirect(f"/suppliers/{supplier_id}", str(exc), "warn")
    return _redirect(f"/suppliers/{supplier_id}", "已保存")


@router.post("/suppliers/{supplier_id}/delete")
def supplier_delete(supplier_id: int, db: Session = Depends(get_db)):
    supplier_service.delete_supplier(db, supplier_id)
    return _redirect("/suppliers", "供应商已删除", "warn")


# ------------------------------------------------------------------ 商品管理
@router.get("/products", response_class=HTMLResponse)
def products(
    request: Request,
    q: str = "",
    category: str = "",
    status: str = "",
    supplier: str = "",
    source: str = "all",
    audit: str = "all",
    view: str = "list",
    page: int = 1,
    per_page: int = PRODUCT_PAGE_SIZE,
    db: Session = Depends(get_db),
):
    """商品列表：照 supply-chain-xiaoyu 的 goods 页面做（商品属性 / 审核分段器 + 列表·卡片双视图）。

    分页：默认每页 50 条（可选 20 / 50 / 100 / 200），48 款以上不分页会把页面拖垮。
    """
    if view not in ("list", "card"):
        view = "list"
    # 每页条数：只认固定档位，地址栏乱填一律回落默认
    if per_page not in PRODUCT_PAGE_SIZES:
        per_page = PRODUCT_PAGE_SIZE
    total_rows = product_service.count_products(
        db,
        q=q,
        category=category,
        status=status,
        supplier=supplier,
        source="" if source == "all" else source,
        audit="" if audit == "all" else audit,
    )
    pages = max((total_rows + per_page - 1) // per_page, 1)
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    current = min(max(current, 1), pages)
    rows = product_service.list_products(
        db,
        q=q,
        category=category,
        status=status,
        supplier=supplier,
        source="" if source == "all" else source,
        audit="" if audit == "all" else audit,
        limit=per_page,
        offset=(current - 1) * per_page,
    )
    stats = product_service.list_stats(db)
    return render(
        "products.html",
        request,
        heading="商品管理",
        sub="订单里的商品名会自动挂到对应商品上 · 商品属性分「供应链商品 / 自营商品」两种",
        products=rows,
        total=total_rows,
        page=current,
        pages=pages,
        per_page=per_page,
        page_sizes=PRODUCT_PAGE_SIZES,
        stats=stats,
        sku_counts=product_service.sku_counts(db, [p.id for p in rows]),
        categories=product_service.distinct_values(db, Product.category),
        suppliers=product_service.distinct_values(db, Product.supplier_name),
        supplier_options=product_service.supplier_options(db),
        keyword=q,
        category_filter=category,
        supplier_filter=supplier,
        status_filter=status,
        source_filter=source,
        audit_filter=audit,
        view=view,
        status_choices=product_service.PRODUCT_STATUS_CHOICES,
        source_choices=product_service.SOURCE_CHOICES,
        audit_choices=product_service.AUDIT_CHOICES,
        condition_choices=product_service.CONDITION_CHOICES,
        shipping_choices=product_service.SHIPPING_CHOICES,
        status_label=product_service.status_label,
        source_label=product_service.source_label,
        source_short_label=product_service.source_short_label,
        audit_label=product_service.audit_label,
        condition_label=product_service.condition_label,
        shipping_label=product_service.shipping_label,
        product_label=product_service.product_label,
    )


# 图片上传：允许的格式与它们的**文件头**（改后缀蒙混过关不管用，得看内容）
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
IMAGE_MAGIC: tuple[tuple[bytes, int], ...] = (
    (b"\x89PNG\r\n\x1a\n", 0),      # PNG
    (b"\xff\xd8\xff", 0),           # JPEG
    (b"GIF87a", 0),                 # GIF
    (b"GIF89a", 0),                 # GIF
    (b"RIFF", 0),                   # WEBP：RIFF????WEBP，WEBP 字样在第 8 字节
)
CHUNK = 65536


def _human_size(value: float) -> str:
    """给人看的体积：8.3MB / 640KB。提示里写「8388608 字节」没人看得懂。"""
    size = float(value or 0)
    if size < 1024:
        return f"{size:.0f} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _is_real_image(data: bytes) -> bool:
    """按文件头判断是不是真图片（只看扩展名的话，改个后缀什么都能传上来）。"""
    head = bytes(data or b"")[:16]
    for magic, offset in IMAGE_MAGIC:
        if head[offset:offset + len(magic)] == magic:
            if magic == b"RIFF":
                return head[8:12] == b"WEBP"
            return True
    return False


@router.post("/products/upload-image")
async def product_upload_image(request: Request):
    """商品图片上传（发布页的本地图片槽位用）：存到 media/products/，返回可直接填的地址。

    三道闸（2026-10-09 加）：
    · **大小**：单张超过 ``settings.upload_image_max_mb`` 直接拒 —— 走域名时 nginx 有
      ``client_max_body_size`` 兜着，但现场常常直连 8000，那一层没有限制，手机原图
      （5~15MB）传一半断了就是一句说不清的「上传失败」。分块读，**超过上限立刻停**，
      不用把整张图读进内存才知道它超了。
    · **格式**：除扩展名外**按文件头再验一次**（改后缀蒙混不了）。
    · **解析异常**：``request.form()`` / ``read()`` 在上传中断时会抛别的异常，兜住
      并记一条异常日志，回一句人话 —— 以前前端只看到 ``Unexpected token '<'``。
    """
    limit_mb = int(getattr(settings, "upload_image_max_mb", 5) or 0)
    limit = limit_mb * 1024 * 1024
    data = b""
    name = ""
    try:
        form = await request.form()
        upload = form.get("file")
        name = getattr(upload, "filename", "") or ""
        if hasattr(upload, "read"):
            while True:
                chunk = await upload.read(CHUNK)
                if not chunk:
                    break
                data += chunk
                if limit and len(data) > limit:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": f"这张图 {_human_size(len(data))}，超过单张 "
                                     f"{limit_mb}MB 上限 —— 用手机自带的编辑裁一下，"
                                     f"或先发到微信再存（会自动压缩）",
                        },
                        status_code=413,
                    )
    except Exception as exc:  # noqa: BLE001 解析失败要给前端一句人话，不能甩 HTML
        log_id = error_log_service.record_request(request, exc, status_code=400)
        return JSONResponse(
            {
                "ok": False,
                "error": f"图片没传完整（{type(exc).__name__}），重新选一次再传",
                "log_id": log_id,
            },
            status_code=400,
        )
    if not name or not data:
        return JSONResponse({"ok": False, "error": "没有收到图片"}, status_code=400)
    suffix = Path(name).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        return JSONResponse(
            {"ok": False, "error": "只支持 png / jpg / jpeg / webp / gif"},
            status_code=400,
        )
    if not _is_real_image(data):
        return JSONResponse(
            {"ok": False, "error": "这个文件不是真图片（内容对不上），换一张再传"},
            status_code=400,
        )
    folder = Path(settings.media_path) / "products"
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid4().hex[:16]}{suffix}"
    try:
        (folder / filename).write_bytes(data)
    except OSError as exc:  # noqa: BLE001 写盘失败不能让页面 500
        log_id = error_log_service.record_request(request, exc, status_code=500)
        return JSONResponse(
            {"ok": False, "error": f"保存失败：{exc}", "log_id": log_id},
            status_code=500,
        )
    return JSONResponse(
        {"ok": True, "url": f"/media/products/{filename}", "size": len(data)}
    )


@router.post("/products/import-yushangzai")
async def product_import_yushangzai(request: Request, db: Session = Depends(get_db)):
    """渔货栈图包导入（2026-10-15 数据需求）。

    multipart：``fish_code``（渔货号，如 YS202610080003）+ ``g1``~``g4``
    多文件字段 —— 一组 = 商品图片，二三四组 = 图文详情图片。
    资料按渔货号从渔集带，详见 ``product_service.import_from_yushangzai``。
    """
    user = current_user(request)
    try:
        form = await request.form()
    except Exception as exc:  # noqa: BLE001 上传中断给一句人话
        log_id = error_log_service.record_request(request, exc, status_code=400)
        return JSONResponse(
            {"ok": False, "error": f"表单没传完整（{type(exc).__name__}）",
             "log_id": log_id},
            status_code=400,
        )
    fish_code = str(form.get("fish_code") or "").strip()
    if not fish_code:
        return JSONResponse({"ok": False, "error": "缺渔货号（fish_code）"},
                            status_code=400)
    groups: dict[str, list[tuple[str, bytes]]] = {}
    for key, upload in form.multi_items():
        if key not in ("g1", "g2", "g3", "g4") or not hasattr(upload, "read"):
            continue
        data = await upload.read()
        if data:
            if len(data) > product_service.IMPORT_IMAGE_MAX_BYTES:
                return JSONResponse(
                    {"ok": False,
                     "error": f"{getattr(upload, 'filename', '') or key} 超过单张 15MB 上限"},
                    status_code=413,
                )
            groups.setdefault(key[1], []).append(
                (str(getattr(upload, "filename", "") or ""), data)
            )
    if not groups.get("1"):
        return JSONResponse(
            {"ok": False, "error": "没有收到一组图（商品图片至少要一张）"},
            status_code=400,
        )
    try:
        report = product_service.import_from_yushangzai(
            db, fish_code, groups, operator=str(getattr(user, "username", "") or "")
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 落库异常不能甩 HTML
        log_id = error_log_service.record_request(request, exc, status_code=500)
        return JSONResponse(
            {"ok": False, "error": f"导入失败：{exc}", "log_id": log_id},
            status_code=500,
        )
    return JSONResponse(report)


@router.get("/products/new", response_class=HTMLResponse)
def product_new(request: Request, db: Session = Depends(get_db)):
    """发布商品：先选商品属性（供应链商品 / 自营商品），再按 7 步分组填 + 右栏实时预览。"""
    # 类目首次访问时按渔货栈那套三级类目播种（表里已有数据就什么都不做）
    category_service.ensure_seeded(db)
    return render(
        "product_new.html",
        request,
        heading="发布商品",
        sub="先选商品属性：供应链商品要指定是哪家供应链；再按上架闲鱼需要的字段一次填完",
        # 单张图片大小上限：页面用它做「选完就校验」，与后端同一个值
        upload_max_mb=int(getattr(settings, "upload_image_max_mb", 5) or 0),
        # 一级 → 二级 → 三级类目树：发布页的级联直接用它（数据在「货源 · 类目管理」里维护）
        category_tree=category_service.options(db),
        # 运费模板与发货方式的候选项都来自「货源 · 运费模板」那份规则（表为空时用内置的兜底）
        freight_templates=shipping_service.freight_names(db) or product_service.FREIGHT_TEMPLATES,
        shipping_choices=shipping_service.METHOD_CHOICES,
        after_sale_options=product_service.AFTER_SALE_OPTIONS,
        # 规格模板：按类目给一组推荐规格项（型号 / 成色 / 重量这类都是规格维度，不是固定字段）
        spec_templates=product_service.SPEC_TEMPLATES,
        # 商品标签（固定标签）：可选项在「商品 · 商品标签」页维护，勾选后随上架同步到渔货栈
        tag_options=tag_service.list_tags(db, only_enabled=True),
        tag_kinds=tag_service.kind_map(db),
        spec_templates_cat2=product_service.SPEC_TEMPLATES_CAT2,
        default_spec_template=product_service.DEFAULT_SPEC_TEMPLATE,
        categories=product_service.distinct_values(db, Product.category),
        suppliers=supplier_service.distinct_values(db, Supplier.name),
        supplier_options=product_service.supplier_options(db),
        source_choices=product_service.SOURCE_CHOICES,
        shops=shop_service.distinct_values(db, Shop.name),
    )


def _json_body(raw: bytes) -> dict:
    """解析前端 POST 过来的 JSON（解不出或不是对象就按空处理，不抛 500）。"""
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@router.get("/products/next-code", response_class=JSONResponse)
def product_next_code(category: str = "", db: Session = Depends(get_db)):
    """商品流水 ID（商家编码）预览：XYSK + 日期 + 4 位全局序号（真正落库在提交那一下）。"""
    return JSONResponse({"code": product_service.next_code(db, category)})


@router.get("/products/wms-item", response_class=JSONResponse)
def product_wms_item(code: str = ""):
    """扫商品条码 → 带出渔集（检测仓）里那台货（2026-10-10）。

    带过来的：商品名称 / 品牌 / 渔货价 / 建议售价 / 市场价（= 渔集基准价）/ 库存（= 渔集数量），
    **商家编码直接复用渔集的条码**（前端填进 #codeInput，SKU 编码跟着变成「条码-01」）。
    渔集没启用或条码查不到都不报错 —— 给一句人话，让人手工填就是了。
    """
    row = wms_bridge.lookup_item(code)
    if not row:
        return JSONResponse({
            "ok": False, "item": {},
            "error": f"渔集（检测仓）里没有条码 / 货号「{str(code or '（空）').strip()}」这台货"
                     f" —— 先去渔集建档，或直接手工填下面的字段",
        })
    # 2026-10-13 用户拍板：渔枢上架商品的条码必须是「可售档 + 可售状态 + 在可售仓」的货 ——
    # 报修中 / 报废 / 待检 / 还在维修仓的那些不能拿去建档上架。
    if not row.get("sellable", True):
        return JSONResponse({
            "ok": False, "item": {},
            "error": row.get("block_reason") or "这台货在渔集不在可售状态，不能用它建档上架",
        })
    return JSONResponse({"ok": True, "item": row})


@router.get("/products/wms-items", response_class=JSONResponse)
def product_wms_items(q: str = "", limit: int = 20):
    """列渔集（检测仓）在库货品 —— 「商品条码」那格的下拉候选（像供应链商品选供应链一样可选）。

    只读，桥断给空列表（页面照旧能手输 / 扫码枪扫）。
    """
    return JSONResponse({"ok": True, "items": wms_bridge.search_items(q, limit=limit)})


@router.post("/products/suggest-title", response_class=JSONResponse)
async def product_suggest_title(request: Request, db: Session = Depends(get_db)):
    """AI 生成渔货商品标题：按「品牌词 + 产品词 + 修饰词 + 热搜词 + 长尾词」给候选（≤30 字）。"""
    payload = _json_body(await request.body())
    return JSONResponse(
        product_service.suggest_titles(
            db,
            name=str(payload.get("name") or ""),
            brand=str(payload.get("brand") or ""),
            category=str(payload.get("category") or ""),
            selling_points=str(payload.get("selling_points") or ""),
        )
    )


@router.post("/products/ai-rewrite-block", response_class=JSONResponse)
async def product_ai_rewrite_block(request: Request, db: Session = Depends(get_db)):
    """AI 重写一组详情文案：按类目词库把这段文案重写一版，前端拿到后直接进剪贴板。

    渔枢系统查看页与渔货栈商品详情页共用（那边没有分组数据时按段拆开来重写）。
    """
    payload = _json_body(await request.body())
    return JSONResponse(
        product_service.rewrite_detail_text(
            db,
            text=str(payload.get("text") or ""),
            name=str(payload.get("name") or ""),
            brand=str(payload.get("brand") or ""),
            category=str(payload.get("category") or ""),
            selling_points=str(payload.get("selling_points") or ""),
        )
    )


@router.get("/products/category-rules", response_class=JSONResponse)
def product_category_rules(category: str = "", db: Session = Depends(get_db)):
    """类目特性规则（一级类目维护）：发布页用作类目经营提示，也是标题词库。"""
    rule = category_service.rules_for(db, category)
    rules = rule["rules"]
    return JSONResponse(
        {
            "category": rule.get("category", ""),
            "tips": rules["tips"],
            "words": {
                "产品词": rules["product"],
                "修饰词": rules["modifier"],
                "热搜词": rules["hot"],
                "长尾词": rules["longtail"],
            },
        }
    )


@router.get("/products/shipping-default", response_class=JSONResponse)
def product_shipping_default(category: str = "", supplier_id: str = "", db: Session = Depends(get_db)):
    """按「供应商 + 类目」规则给出该用的运费模板与发货方式（发布页预选，留空即自动）。"""
    try:
        sid = int(str(supplier_id).strip()) if str(supplier_id).strip() else None
    except ValueError:
        sid = None
    return JSONResponse(
        shipping_service.resolve_for_product(db, category_text=category, supplier_id=sid)
    )


@router.get("/products/stats", response_class=HTMLResponse)
def product_stats(request: Request, db: Session = Depends(get_db)):
    data = product_service.overview(db)
    return render(
        "products_stats.html",
        request,
        heading="商品统计",
        sub="按商品属性 / 审核 / 上架 / 类目 / 供应链看商品盘子，以及卖得最好的几款（不含演示数据）",
        **data,
        source_label=product_service.source_label,
        source_short_label=product_service.source_short_label,
        status_label=product_service.status_label,
        audit_label=product_service.audit_label,
        product_label=product_service.product_label,
    )


@router.get("/products/categories", response_class=HTMLResponse)
def product_categories(request: Request, db: Session = Depends(get_db)):
    """类目管理：发布商品时选的一级 / 二级 / 三级类目就在这里维护。

    首次打开时按渔货栈那套三级类目自动播种；之后可以增删改、调顺序、停用，
    也可以再点「从渔货栈同步」把渔货栈新增的类目补进来（只补不删）。
    """
    seeded = category_service.ensure_seeded(db)
    if seeded:
        return _redirect(
            "/products/categories",
            f"已按「{category_service.SEED_SOURCE}」导入 {seeded} 个类目",
        )
    return render(
        "product_categories.html",
        request,
        heading="类目管理",
        sub=f"发布商品时选的一级 / 二级 / 三级类目就是这份数据（来源：{category_service.SEED_SOURCE}）",
        tree=category_service.tree(db),
        stat=category_service.stats(db),
        usage=category_service.usage_counts(db),
        seed_source=category_service.SEED_SOURCE,
        max_level=category_service.MAX_LEVEL,
        # 类目特性规则：不影响类目显示，供标题生成 / 别名推荐 / 上架经营提示使用
        rules_template=category_service.RULES_TEMPLATE,
    )


@router.get("/products/categories/leaves", response_class=JSONResponse)
def product_category_leaves(parent_id: int = 0, db: Session = Depends(get_db)):
    """某个二级类目下的三级类目（JSON）。

    类目换成 1688 那套之后，一个二级下面能有上百个三级，管理页一次全渲染会有几 MB，
    所以三级改成点了「展开三级」才拉这一份。
    """
    from ..models import ProductCategory  # noqa: PLC0415

    rows = db.scalars(
        select(ProductCategory)
        .where(ProductCategory.parent_id == parent_id)
        .order_by(ProductCategory.sort_order, ProductCategory.id)
    ).all()
    usage = category_service.usage_counts(db)
    return JSONResponse(
        {
            "leaves": [
                {
                    "id": int(node.id),
                    "name": node.name,
                    "enabled": bool(node.enabled),
                    "used": int(usage.get(node.id, 0) or 0),
                }
                for node in rows
            ]
        }
    )


@router.post("/products/categories/create")
async def product_category_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    raw_parent = str(form.get("parent_id", "") or "").strip()
    parent_id = int(raw_parent) if raw_parent.isdigit() else None
    node = category_service.create_node(db, parent_id, str(form.get("name", "") or ""))
    if node is None:
        return _redirect(
            "/products/categories",
            "没加成功：名称为空、同级重名，或者已经到三级了（类目最多三级）",
            "warn",
        )
    return _redirect("/products/categories", f"已新增类目「{node.name}」")


@router.post("/products/categories/rename")
async def product_category_rename(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    node_id = int(str(form.get("id", "") or 0) or 0)
    if not category_service.rename_node(db, node_id, str(form.get("name", "") or "")):
        return _redirect("/products/categories", "改名没生效：名称为空，或与同级类目重名", "warn")
    return _redirect("/products/categories", "类目名称已更新")


@router.post("/products/categories/toggle")
async def product_category_toggle(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    node_id = int(str(form.get("id", "") or 0) or 0)
    enabled = str(form.get("enabled", "1")) == "1"
    if not category_service.toggle_node(db, node_id, enabled):
        return _redirect("/products/categories", "类目不存在", "warn")
    return _redirect(
        "/products/categories",
        "已启用该类目" if enabled else "已停用该类目（发布页不再出现，历史商品不受影响）",
    )


@router.post("/products/categories/move")
async def product_category_move(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    node_id = int(str(form.get("id", "") or 0) or 0)
    delta = -1 if str(form.get("direction", "up")) == "up" else 1
    category_service.move_node(db, node_id, delta)
    return _redirect("/products/categories", "")


@router.post("/products/categories/reorder", response_class=JSONResponse)
async def product_category_reorder(request: Request, db: Session = Depends(get_db)):
    """拖拽排序：按传上来的 id 顺序重排一级类目（页面拖完直接提交，不整页刷新）。

    只认同一层的类目；前台（发布商品类目级联、渔货栈货源广场）立刻按新顺序展示。
    """
    form = await request.form()
    ids = [
        int(x.strip())
        for x in str(form.get("ids", "") or "").split(",")
        if x.strip().lstrip("-").isdigit()
    ]
    ok = category_service.reorder_nodes(db, ids)
    return JSONResponse({"ok": bool(ok), "count": len(ids)})


@router.post("/products/categories/delete")
async def product_category_delete(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    node_id = int(str(form.get("id", "") or 0) or 0)
    removed = category_service.delete_node(db, node_id)
    if not removed:
        return _redirect("/products/categories", "该类目已经不在了", "warn")
    return _redirect(
        "/products/categories",
        f"已删除 {removed} 个类目（已发布商品上的类目文字不会被改动）",
    )


@router.post("/products/categories/rules")
async def product_category_rules_save(request: Request, db: Session = Depends(get_db)):
    """保存类目特性规则：不影响类目显示，只作为上架商品与后续经营的经验规则。"""
    form = await request.form()
    node_id = int(str(form.get("id", "") or 0) or 0)
    if not category_service.set_rules(db, node_id, str(form.get("rules", "") or "")):
        return _redirect("/products/categories", "类目不存在", "warn")
    return _redirect("/products/categories", "类目特性规则已保存")


@router.post("/products/categories/sync")
def product_category_sync(db: Session = Depends(get_db)):
    """从渔货栈同步：只补渔货栈新增的、并跟进改名，本地自建的与排序 / 停用状态都保留。"""
    result = category_service.sync_from_sourcing(db)
    return _redirect(
        "/products/categories",
        f"已从「{category_service.SEED_SOURCE}」同步：新增 {result['added']} 个、跟进改名 {result['renamed']} 个",
    )


# ---------------------------------------------------------------- 运费模板 / 发货方式
def _form_int(value) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.lstrip("-").isdigit() else None


def _form_money(value) -> float:
    try:
        return round(float(str(value or "").strip() or 0), 2)
    except ValueError:
        return 0.0


def _shipping_kind(value) -> str:
    kind = str(value or "").strip()
    return kind if kind in shipping_service.KINDS else shipping_service.KIND_FREIGHT


@router.get("/shipping-rules", response_class=HTMLResponse)
def shipping_rules_page(request: Request, kind: str = shipping_service.KIND_FREIGHT, db: Session = Depends(get_db)):
    """运费模板 / 闲鱼发货方式管理：一条全局默认 + 按供应商 / 类目细分（越具体越优先）。"""
    kind = _shipping_kind(kind)
    return render(
        "shipping_rules.html",
        request,
        heading="运费模板 / 发货方式",
        sub="上架商品时默认套用的规则：支持一条全局默认，也可以按供应商 / 类目细分"
            "（供应商 + 类目 > 类目 > 供应商 > 全局默认）",
        kind=kind,
        kind_labels=shipping_service.KIND_LABELS,
        kinds=shipping_service.KINDS,
        rows=shipping_service.rule_rows(db, kind),
        method_choices=shipping_service.METHOD_CHOICES,
        category_options=shipping_service.category_options(db),
        suppliers=product_service.supplier_options(db),
        default_rule=shipping_service.default_rule(db, kind),
        kind_label=shipping_service.kind_label(kind),
    )


@router.post("/shipping-rules/create")
async def shipping_rule_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    kind = _shipping_kind(form.get("kind"))
    name = str(form.get("name") or "").strip()
    if not name:
        return _redirect(f"/shipping-rules?kind={kind}", "模板名不能为空", "warn")
    rule = shipping_service.create_rule(
        db,
        kind=kind,
        name=name,
        fee=_form_money(form.get("fee")),
        mode=str(form.get("mode") or ""),
        supplier_id=_form_int(form.get("supplier_id")),
        category_id=_form_int(form.get("category_id")),
        priority=_form_int(form.get("priority")) or 100,
        note=str(form.get("note") or ""),
        is_default=str(form.get("is_default") or "") not in ("", "0"),
    )
    return _redirect(
        f"/shipping-rules?kind={kind}",
        f"已新增「{rule.name}」（{shipping_service.rule_scope(rule)}）",
    )


@router.post("/shipping-rules/edit")
async def shipping_rule_edit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    rule_id = _form_int(form.get("id")) or 0
    rule = shipping_service.get_rule(db, rule_id)
    if rule is None:
        return _redirect("/shipping-rules", "规则不存在", "warn")
    shipping_service.update_rule(db, rule, dict(form))
    return _redirect(f"/shipping-rules?kind={rule.kind}", f"已保存「{rule.name}」")


@router.post("/shipping-rules/delete")
async def shipping_rule_delete(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    rule_id = _form_int(form.get("id")) or 0
    rule = shipping_service.get_rule(db, rule_id)
    if rule is None:
        return _redirect("/shipping-rules", "规则不存在", "warn")
    kind, name = rule.kind, rule.name
    shipping_service.delete_rule(db, rule)
    return _redirect(f"/shipping-rules?kind={kind}", f"已删除「{name}」（商品上已经存下的值不变）")


@router.get("/product-tags", response_class=HTMLResponse)
def product_tags_page(request: Request, db: Session = Depends(get_db)):
    """商品标签字典：发布 / 编辑商品时能勾选的固定标签（源头工厂 / 现货速发 …）。

    只管「人工认定的事实标签」；渔货栈按数据实时算的动态标签（今日上新 / 直降 x 元 /
    高毛利 x%）不在这里配。
    """
    return render(
        "product_tags.html",
        request,
        heading="商品标签",
        sub="发布商品时勾选的固定标签：源头工厂、现货速发这类「事实标签」由人工认；"
            "直降 x 元 / 高毛利 x% 这类动态标签由渔货栈按数据当场算，不用在这里配",
        rows=tag_service.list_tags(db),
        kinds=tag_service.KIND_CHOICES,
        kind_text=tag_service.KIND_TEXT,
        usage=tag_service.usage_counts(db),
    )


@router.post("/product-tags/create")
async def product_tag_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    tag, err = tag_service.create_tag(
        db,
        name=str(form.get("name") or ""),
        kind=str(form.get("kind") or "info"),
        hint=str(form.get("hint") or ""),
        sort_order=_form_int(form.get("sort_order")) or 100,
    )
    if err:
        return _redirect("/product-tags", err, "warn")
    return _redirect("/product-tags", f"已新增标签「{tag.name}」，发布 / 编辑商品时就能勾选了")


@router.post("/product-tags/{tag_id}/edit")
async def product_tag_edit(tag_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    tag = tag_service.get_tag(db, tag_id)
    if tag is None:
        return _redirect("/product-tags", "标签不存在", "warn")
    tag, err = tag_service.update_tag(db, tag, dict(form))
    if err:
        return _redirect("/product-tags", err, "warn")
    return _redirect("/product-tags", f"已保存标签「{tag.name}」（商品上用的旧名一并换掉了）")


@router.post("/product-tags/{tag_id}/toggle")
async def product_tag_toggle(tag_id: int, db: Session = Depends(get_db)):
    tag = tag_service.get_tag(db, tag_id)
    if tag is None:
        return _redirect("/product-tags", "标签不存在", "warn")
    tag = tag_service.toggle_tag(db, tag)
    return _redirect("/product-tags", f"标签「{tag.name}」已{'启用' if tag.enabled else '停用'}")


@router.post("/product-tags/{tag_id}/delete")
async def product_tag_delete(tag_id: int, db: Session = Depends(get_db)):
    tag = tag_service.get_tag(db, tag_id)
    if tag is None:
        return _redirect("/product-tags", "标签不存在", "warn")
    name = tag.name
    err = tag_service.delete_tag(db, tag)
    if err:
        return _redirect("/product-tags", err, "warn")
    return _redirect("/product-tags", f"已删除标签「{name}」")


# ------------------------------------------------------------------ 采购节点（活动日历）
@router.get("/sourcing-display", response_class=HTMLResponse)
def sourcing_display_page(request: Request, db: Session = Depends(get_db)):
    """货源展示基数：渔货栈卡片「X 家上架 / 已售 N」垫的那份基数。

    只影响展示 —— 真实值照旧单独算：排序、热销榜计数、商品详情页「在售闲鱼号」
    用的都是真实值，基数一个都不加（用户原话「实际的上架数量仍然要准确的」）。
    """
    return render(
        "sourcing_display.html",
        request,
        heading="展示基数",
        sub="渔货栈货源广场卡片上的「X 家上架」与「已售 N」："
            "显示的是「基数 + 真实数量」，真实数量一个不动",
        config=display_service.get_config(db),
        base_max=display_service.BASE_MAX,
    )


@router.post("/sourcing-display")
async def sourcing_display_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    user = current_user(request)
    row = display_service.save_config(
        db, dict(form), operator=(user.username if user else "")
    )
    return _redirect(
        "/sourcing-display",
        f"已保存：上架家数基数 {row.listed_shops_base} · 已售基数 {row.sold_base}"
        f"（渔货栈最多 60 秒后生效）",
    )


@router.get("/purchase-nodes", response_class=HTMLResponse)
def purchase_nodes_page(request: Request, db: Session = Depends(get_db)):
    """采购节点：渔货栈货源广场顶部「采购节点」运营位那份活动日历。

    节点是运营日历（日期窗口 + 图标 + 关键词）：开关、日期、关键词、排序都在这里改，
    渔货栈最多 60 秒后生效。节点下的商品按关键词在货源标题里实时匹配，不做写死绑定。
    """
    return render(
        "purchase_nodes.html",
        request,
        heading="采购节点",
        sub="渔货栈「货源广场」顶部的活动运营位：窗口内的节点排前面，"
            "点某个节点就把它当搜索条件过滤货源（关键词命中标题）",
        rows=node_service.list_nodes(db),
        windows=node_service.windows(beijing_now().date()),
        icon_choices=node_service.ICON_CHOICES,
        node_seeds=node_service.NODE_SEEDS,
        # 手动配置的商品（2026-10-11）：按节点分组 + 可配货源下拉（渔货栈在架货源）
        items_by_node=node_service.items_by_node(db),
        item_options=node_service.item_candidates(db),
    )


@router.post("/purchase-nodes/create")
async def purchase_node_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    node, err = node_service.create_node(db, dict(form))
    if err:
        return _redirect("/purchase-nodes", err, "warn")
    return _redirect("/purchase-nodes", f"已新增节点「{node.name}」（{node.start_md} ~ {node.end_md}）")


@router.post("/purchase-nodes/{node_id}/edit")
async def purchase_node_edit(node_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    node = node_service.get_node(db, node_id)
    if node is None:
        return _redirect("/purchase-nodes", "节点不存在", "warn")
    node, err = node_service.update_node(db, node, dict(form))
    if err:
        return _redirect("/purchase-nodes", err, "warn")
    return _redirect("/purchase-nodes", f"已保存节点「{node.name}」")


@router.post("/purchase-nodes/{node_id}/toggle")
async def purchase_node_toggle(node_id: int, db: Session = Depends(get_db)):
    node = node_service.get_node(db, node_id)
    if node is None:
        return _redirect("/purchase-nodes", "节点不存在", "warn")
    node = node_service.toggle_node(db, node)
    return _redirect(
        "/purchase-nodes",
        f"节点「{node.name}」已{'开启' if node.enabled else '关闭'}（关闭后渔货栈不再展示）",
    )


@router.post("/purchase-nodes/{node_id}/delete")
async def purchase_node_delete(node_id: int, db: Session = Depends(get_db)):
    node = node_service.get_node(db, node_id)
    if node is None:
        return _redirect("/purchase-nodes", "节点不存在", "warn")
    name = node.name
    node_service.delete_node(db, node)
    return _redirect("/purchase-nodes", f"已删除节点「{name}」")


# ---------------------------------------------- 采购节点 · 手动配置的商品
@router.post("/purchase-nodes/{node_id}/items/add")
async def purchase_node_item_add(node_id: int, request: Request, db: Session = Depends(get_db)):
    """给节点手动配一款货：手动配置的在渔货栈排最前，关键词规则照旧。"""
    form = await request.form()
    node = node_service.get_node(db, node_id)
    if node is None:
        return _redirect("/purchase-nodes", "节点不存在", "warn")
    row, err = node_service.add_item(
        db,
        node,
        str(form.get("item_id") or ""),
        note=str(form.get("note") or ""),
    )
    if err:
        return _redirect("/purchase-nodes", err, "warn")
    return _redirect(
        "/purchase-nodes",
        f"已把「{row.title}」配到节点「{node.name}」—— 渔货栈里它排在这个活动的最前面",
    )


@router.post("/purchase-nodes/items/{row_id}/move")
async def purchase_node_item_move(row_id: int, request: Request, db: Session = Depends(get_db)):
    """手动配置上下挪位（页面上的 ↑ / ↓）。"""
    form = await request.form()
    row = node_service.get_item(db, row_id)
    if row is None:
        return _redirect("/purchase-nodes", "配置不存在", "warn")
    direction = str(form.get("dir") or "up")
    if not node_service.move_item(db, row, direction):
        return _redirect("/purchase-nodes", "已经是第一个 / 最后一个了", "warn")
    return _redirect("/purchase-nodes", f"已调整「{row.title or row.item_id}」的展示顺序")


@router.post("/purchase-nodes/items/{row_id}/toggle")
async def purchase_node_item_toggle(row_id: int, db: Session = Depends(get_db)):
    """临时不放这款货的置顶位：关掉就退回关键词那套，配置留着不用重配。"""
    row = node_service.get_item(db, row_id)
    if row is None:
        return _redirect("/purchase-nodes", "配置不存在", "warn")
    row = node_service.toggle_item(db, row)
    return _redirect(
        "/purchase-nodes",
        f"「{row.title or row.item_id}」已{'启用置顶' if row.enabled else '停用（退回按关键词匹配）'}",
    )


@router.post("/purchase-nodes/items/{row_id}/remove")
async def purchase_node_item_remove(row_id: int, db: Session = Depends(get_db)):
    """从这个节点撤掉这款货（只撤置顶关系，货源本身不动）。"""
    row = node_service.get_item(db, row_id)
    if row is None:
        return _redirect("/purchase-nodes", "配置不存在", "warn")
    title = row.title or row.item_id
    node_service.remove_item(db, row)
    return _redirect("/purchase-nodes", f"已把「{title}」从这个节点撤下（货源本身不受影响）")


@router.get("/purchase-nodes/item-options", response_class=JSONResponse)
def purchase_node_item_options(keyword: str = "", db: Session = Depends(get_db)):
    """按关键词搜可配的货源（节点页「加商品」那个搜索框用）。"""
    return JSONResponse({"items": node_service.item_candidates(db, keyword, limit=50)})


# ------------------------------------------------------------------ 公司资源 · 手机号
# 一个手机号是一条资源：它开出来的闲鱼 / 支付宝 / 微信 / 小红书账号是能直接干活的资产。
# 台账只记资源本身（谁实名、什么性质、主卡副卡、套餐怎么付、开了哪些平台），
# **不存任何登录凭据**（密码 / Cookie 一律不入库）。
PHONE_FILTER_KEYS = ("q", "nature", "card_type", "plan", "carrier", "status", "platform", "idle")


def _phone_query(filters: dict, **overrides) -> str:
    """筛选条件的查询串：切换筛选时把别的条件带着走，一点「全部」才清空。"""
    merged = dict(filters)
    merged.update(overrides)
    items = [(k, str(v)) for k, v in merged.items() if str(v or "").strip()]
    return ("?" + urlencode(items)) if items else ""


@router.get("/phone-numbers", response_class=HTMLResponse)
def phone_numbers_page(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    nature: str = "",
    card_type: str = "",
    plan: str = "",
    carrier: str = "",
    status: str = "",
    platform: str = "",
    idle: str = "",
):
    """手机号管理：列表页（默认）+ 顶部统计。

    顶部那几个数与下面的列表用的是**同一份筛选条件**，所以「共 N 个号」与列表条数
    必然对得上（筛选了就是筛选后的数，没筛就是全量）。
    """
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    filters = {
        "q": q, "nature": nature, "card_type": card_type, "plan": plan,
        "carrier": carrier, "status": status, "platform": platform, "idle": idle,
    }
    has_filter = any(str(v or "").strip() for v in filters.values())
    return render(
        "phone_numbers.html",
        request,
        heading="手机号管理",
        sub="公司资源台账：一个号是一条资源 —— 谁实名、什么性质、主卡还是副卡、套餐怎么付，"
            "以及它开出了哪些平台账号（闲鱼 / 支付宝 / 微信 / 小红书）",
        rows=phone_service.rows(db, **filters),
        stats=phone_service.stats(db, **filters),
        filters=filters,
        has_filter=has_filter,
        clear_url="/phone-numbers",
        nature_choices=phone_service.NATURE_CHOICES,
        nature_text=phone_service.NATURE_TEXT,
        card_choices=phone_service.CARD_CHOICES,
        card_text=phone_service.CARD_TEXT,
        plan_choices=phone_service.PLAN_CHOICES,
        plan_text=phone_service.PLAN_TEXT,
        carrier_choices=phone_service.CARRIER_CHOICES,
        carrier_text=phone_service.CARRIER_TEXT,
        platforms=phone_service.PLATFORMS,
        platform_text=phone_service.PLATFORM_TEXT,
        status_choices=phone_service.STATUS_CHOICES,
        status_text=phone_service.STATUS_TEXT,
        # 顶部「闲鱼 / 支付宝 / 微信 / 小红书」那四个数点得进去：看已开了这个平台账号的号
        # （再点一次取消，其它筛选条件带着走）
        platform_links={
            key: "/phone-numbers" + _phone_query(filters, platform="" if platform == key else key)
            for key, _label, _icon in phone_service.PLATFORMS
        },
        # 一个平台账号都没开的号：运营最该先处理的一批
        idle_url="/phone-numbers" + _phone_query(filters, platform="", idle="" if idle else "1"),
    )


def _phone_form_ctx(**extra) -> dict:
    """新建 / 编辑页共用的下拉候选。"""
    ctx = {
        "nature_choices": phone_service.NATURE_CHOICES,
        "nature_text": phone_service.NATURE_TEXT,
        "card_choices": phone_service.CARD_CHOICES,
        "card_text": phone_service.CARD_TEXT,
        "plan_choices": phone_service.PLAN_CHOICES,
        "plan_text": phone_service.PLAN_TEXT,
        "carrier_choices": phone_service.CARRIER_CHOICES,
        "carrier_text": phone_service.CARRIER_TEXT,
        "platforms": phone_service.PLATFORMS,
        "platform_text": phone_service.PLATFORM_TEXT,
    }
    ctx.update(extra)
    return ctx


@router.get("/phone-numbers/new", response_class=HTMLResponse)
def phone_new_page(request: Request):
    """新增手机号资源：字段多，单独开一页填（字段按板块分组，见 phone_form.html）。"""
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    return render(
        "phone_form.html",
        request,
        crumb_path="/phone-numbers",
        crumb_leaf="新增手机号",
        heading="新增手机号资源",
        sub="一个号填一条：号码与归属是必填，平台账号开了哪个勾哪个（后面随时能改）",
        is_edit=False,
        **_phone_form_ctx(),
    )


@router.get("/phone-numbers/{phone_id}/edit", response_class=HTMLResponse)
def phone_edit_page(phone_id: int, request: Request, db: Session = Depends(get_db)):
    """编辑手机号资源（与新增同一张表单，按已有记录预填）。"""
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    phone = phone_service.get_phone(db, phone_id)
    if phone is None:
        return _redirect("/phone-numbers", "这个手机号记录不存在（可能已经被删了）", "warn")
    return render(
        "phone_form.html",
        request,
        crumb_path="/phone-numbers",
        crumb_leaf="编辑手机号",
        heading=f"编辑 {phone_service.format_number(phone.number)}",
        sub="改完点保存：实名、套餐、平台账号的改动都留在台账里（停用不删记录）",
        is_edit=True,
        phone=phone,
        **_phone_form_ctx(),
    )


@router.post("/phone-numbers/create")
async def phone_create(request: Request, db: Session = Depends(get_db)):
    admin = _admin_user(request)
    if not admin:
        return _admin_or_redirect(request)
    form = await request.form()
    phone, err = phone_service.create_phone(db, dict(form))
    if err:
        return _redirect("/phone-numbers/new", err, "warn")
    return _redirect(
        "/phone-numbers",
        f"已新增手机号资源 {phone_service.format_number(phone.number)}（{phone.owner_name}）",
    )


@router.post("/phone-numbers/{phone_id}/edit")
async def phone_edit(phone_id: int, request: Request, db: Session = Depends(get_db)):
    admin = _admin_user(request)
    if not admin:
        return _admin_or_redirect(request)
    phone = phone_service.get_phone(db, phone_id)
    if phone is None:
        return _redirect("/phone-numbers", "这个手机号记录不存在（可能已经被删了）", "warn")
    form = await request.form()
    phone, err = phone_service.update_phone(db, phone, dict(form))
    if err:
        return _redirect(f"/phone-numbers/{phone_id}/edit", err, "warn")
    return _redirect(
        "/phone-numbers",
        f"已保存 {phone_service.format_number(phone.number)}（{phone.owner_name}）",
    )


@router.post("/phone-numbers/{phone_id}/toggle")
async def phone_toggle(phone_id: int, request: Request, db: Session = Depends(get_db)):
    admin = _admin_user(request)
    if not admin:
        return _admin_or_redirect(request)
    phone = phone_service.get_phone(db, phone_id)
    if phone is None:
        return _redirect("/phone-numbers", "这个手机号记录不存在（可能已经被删了）", "warn")
    phone = phone_service.toggle_phone(db, phone)
    text = phone_service.format_number(phone.number)
    return _redirect(
        "/phone-numbers",
        f"{text} 已{'启用' if phone.enabled else '停用'}"
        + ("" if phone.enabled else "（记录保留：它可能开过平台账号，删了就查不到是谁开的）"),
    )


@router.post("/phone-numbers/{phone_id}/delete")
async def phone_delete(phone_id: int, request: Request, db: Session = Depends(get_db)):
    admin = _admin_user(request)
    if not admin:
        return _admin_or_redirect(request)
    phone = phone_service.get_phone(db, phone_id)
    if phone is None:
        return _redirect("/phone-numbers", "这个手机号记录不存在（可能已经被删了）", "warn")
    text = phone_service.format_number(phone.number)
    phone_service.delete_phone(db, phone)
    return _redirect("/phone-numbers", f"已删除手机号资源 {text}")


def _address_kind(form) -> str:
    """表单里带回来的用途（隐藏字段 kind），缺省当退货地址。"""
    return return_addr_service.normalize_kind(str(form.get("kind") or ""))


def _addresses_url(kind: str | None) -> str:
    return f"/return-addresses?kind={return_addr_service.normalize_kind(kind)}"


@router.get("/addresses", response_class=HTMLResponse)
@router.get("/return-addresses", response_class=HTMLResponse)
def return_addresses_page(request: Request, db: Session = Depends(get_db), kind: str = "return"):
    """地址管理：退货地址（买家寄回）+ 发货地址（我们寄出），两种用途逻辑完全一致。

    入口保留两个：`/addresses`（语义更准，菜单用这个）与 `/return-addresses`
    （老链接照旧可用，不做跳转，免得运营收藏的书签失效）。
    """
    use = return_addr_service.normalize_kind(kind)
    is_ship = use == return_addr_service.KIND_SHIP
    return render(
        "return_addresses.html",
        request,
        heading="地址管理",
        sub=(
            "我们的货从哪儿发出：一条全局默认，也可以按供应商 / 一级类目细分，或直接指定商品"
            if is_ship
            else "买家退货寄到哪儿：一条全局默认，也可以按供应商 / 一级类目细分，或直接指定商品"
        ),
        kind=use,
        kind_label=return_addr_service.kind_label(use),
        rows=return_addr_service.rows(db, kind=use),
        default_address=return_addr_service.default_address(db, kind=use),
        category_options=return_addr_service.category_options(db),
        suppliers=product_service.supplier_options(db),
        product_options=return_addr_service.product_options(db),
    )


@router.post("/return-addresses/create")
async def return_address_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    use = _address_kind(form)
    name = str(form.get("name") or "").strip()
    if not name:
        return _redirect(_addresses_url(use), "地址名称不能为空", "warn")
    addr = return_addr_service.create_address(
        db,
        kind=use,
        name=name,
        contact=str(form.get("contact") or ""),
        phone=str(form.get("phone") or ""),
        region=str(form.get("region") or ""),
        address=str(form.get("address") or ""),
        supplier_id=_form_int(form.get("supplier_id")),
        category_id=_form_int(form.get("category_id")),
        priority=_form_int(form.get("priority")) or 100,
        note=str(form.get("note") or ""),
        is_default=str(form.get("is_default") or "") not in ("", "0"),
        product_ids=_form_int_list(form, "product_ids"),
    )
    return _redirect(
        _addresses_url(use),
        f"已新增「{addr.name}」（{return_addr_service.scope_text(addr, len(_form_int_list(form, 'product_ids')))}）",
    )


@router.post("/return-addresses/edit")
async def return_address_edit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    addr = return_addr_service.get_address(db, _form_int(form.get("id")) or 0)
    if addr is None:
        return _redirect(_addresses_url(_address_kind(form)), "地址不存在", "warn")
    return_addr_service.update_address(db, addr, dict(form))
    return _redirect(_addresses_url(addr.kind), f"已保存「{addr.name}」")


@router.post("/return-addresses/products")
async def return_address_products(request: Request, db: Session = Depends(get_db)):
    """重设这条地址关联的商品（多选，留空 = 不指定商品）。"""
    form = await request.form()
    addr = return_addr_service.get_address(db, _form_int(form.get("id")) or 0)
    if addr is None:
        return _redirect(_addresses_url(_address_kind(form)), "地址不存在", "warn")
    product_ids = _form_int_list(form, "product_ids")
    return_addr_service.set_products(db, addr, product_ids)
    if product_ids:
        return _redirect(_addresses_url(addr.kind), f"「{addr.name}」已指定 {len(product_ids)} 款商品")
    return _redirect(
        _addresses_url(addr.kind), f"「{addr.name}」已取消指定商品（按供应商 / 类目 / 默认走）"
    )


@router.post("/return-addresses/delete")
async def return_address_delete(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    addr = return_addr_service.get_address(db, _form_int(form.get("id")) or 0)
    if addr is None:
        return _redirect(_addresses_url(_address_kind(form)), "地址不存在", "warn")
    name = addr.name
    kind = addr.kind
    return_addr_service.delete_address(db, addr)
    return _redirect(_addresses_url(kind), f"已删除「{name}」")


def _form_int_list(form, key: str) -> list[int]:
    """多选框（同名多个值）→ int 列表。"""
    values = form.getlist(key) if hasattr(form, "getlist") else []
    out: list[int] = []
    for value in values:
        num = _form_int(value)
        if num:
            out.append(num)
    return out


@router.get("/products/categories/export")
def product_category_export(db: Session = Depends(get_db)):
    """导出类目 JSON：要跟渔货栈侧核对口径时，拿这份文件直接比。"""
    category_service.ensure_seeded(db)
    return JSONResponse(
        {"source": category_service.SEED_SOURCE, "tree": category_service.tree(db)},
        headers={"Content-Disposition": 'attachment; filename="product_categories.json"'},
    )


def _products_back(raw: str) -> str:
    """批量操作完回跳的地址：只认本站 /products 开头的相对地址（带筛选 / 页码），
    其它一律回落 /products —— 页面传来的地址不能直接信，防开放重定向。"""
    raw = (raw or "").strip()
    if raw.startswith("/products") and "\n" not in raw and "\\" not in raw:
        return raw
    return "/products"


@router.post("/products/bulk")
async def product_bulk(request: Request, db: Session = Depends(get_db)):
    """批量操作：上架 / 下架 / 审核通过 / 驳回 / 删除 / 匹配类目（列表页勾选后执行）。"""
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).strip().isdigit()]
    action = str(form.get("action", "") or "").strip()
    # 操作完回到原来的筛选 / 页码（页面把当前地址带过来），不再把人踢回第 1 页
    back = _products_back(str(form.get("return_to", "") or ""))
    labels = {
        "list": "上架", "unlist": "下架", "approve": "审核通过",
        "reject": "驳回", "delete": "删除",
    }
    if not ids:
        return _redirect(back, "请先勾选商品", "warn")
    if action == "category":
        result = product_service.bulk_match_category(db, ids)
        tip = f"类目匹配完成：补上 {result['done']} 款"
        if result["skipped"]:
            tip += f" · 已有类目 {result['skipped']} 款未改动"
        if result["failed"]:
            tip += f" · {result['failed']} 款没认出来"
        if result["samples"]:
            tip += "（" + "；".join(result["samples"][:3]) + "）"
        return _redirect(
            back, tip, "warn" if result["done"] == 0 else "ok"
        )
    done = product_service.bulk_action(db, ids, action, note=str(form.get("note", "") or ""))
    tip = f"已{labels.get(action, '处理')} {done} 款商品"
    if done < len(ids):
        tip += f"（共勾选 {len(ids)} 款，状态已一致的会自动跳过）"
    return _redirect(back, tip)


@router.post("/products")
async def product_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        row = product_service.create_product(db, form, created_by=_username(request))
    except ValueError as exc:
        return _redirect("/products", str(exc), "warn")
    # 跳到商品编码地址（不用中文商品名进 URL）；提示走一次性 cookie
    return _redirect(product_service.product_url(row), "商品已创建：可继续完善资料或上架")


@router.post("/products/backfill")
def product_backfill(db: Session = Depends(get_db)):
    """按历史订单的商品名批量建「草稿商品」并把订单挂上去（不动订单里的商品名与金额）。"""
    out = product_service.backfill_from_orders(db)
    return _redirect(
        "/products",
        f"已按历史订单补齐：新建商品 {out['created']} 款、挂上订单 {out['linked']} 单，"
        f"当前商品共 {out['total']} 款",
    )


@router.get("/products/{key}", response_class=HTMLResponse)
def product_detail(request: Request, key: str, db: Session = Depends(get_db)):
    """商品详情。

    地址里的流水号是**商品编码**（如 /products/SPD0013），不是中文商品名：
    分享 / 复制链接更短、更稳；老链接里的数字 id 继续兼容。
    """
    row = product_service.get_by_key(db, key)
    if not row:
        raise HTTPException(404, "商品不存在")
    skus = product_service.list_skus(db, row.id)
    package = product_service.listing_package(row, skus)
    return render(
        "product_detail.html",
        request,
        heading=product_service.product_label(row),
        # 副标题留空：来源 / 类目 / 审核 / 上架状态都在商品名下面那一行徽章里，不再重复一行文字
        sub="",
        # 查看页只读：字段展示 + 渔货栈上架状态与上架预览；
        # 编辑 / 审核 / 规格维护一律走独立编辑页（/products/{id}/edit）
        product=row,
        stat=product_service.product_stat(db, row),
        # 商品标签的配色（取「商品标签」字典里的 kind；字典里没有的老标签回落 info）
        tag_kinds=tag_service.kind_map(db),
        skus=skus,
        sku_count=len(skus),
        package=package,
        # 商品详情按「组」展示：文案一组一段，详情图每 9 张一组（一组一个左右切换的看图条）
        detail_blocks=product_service.detail_groups_of(row),
        detail_block_count=len(product_service.detail_groups_of(row)),
        detail_image_groups=product_service.detail_image_groups(product_service.detail_images_of(row)),
        detail_image_count=len(product_service.detail_images_of(row)),
        detail_image_group_size=product_service.DETAIL_IMAGE_GROUP_SIZE,
        # 渔货栈上架状态：渔货栈商品池那条记录 + 类目映射结果（「上架到渔货栈」卡片用）
        sourcing_status=sourcing_service.status_of(db, row),
        # 素材图（主图 + 图集，去重）
        gallery=product_service.gallery_images(row),
        status_label=product_service.status_label,
        source_label=product_service.source_label,
        source_short_label=product_service.source_short_label,
        audit_label=product_service.audit_label,
        condition_label=product_service.condition_label,
        shipping_label=product_service.shipping_label,
        # 渔集（检测仓）那台货的商品资料（2026-10-15）：渔枢这边还没填型号 / 规格时，
        # 按「商家编码 == 渔货号」去渔集那边取一份（桥断 / 查不到就是 None，页面显「—」）
        wms_profile=(wms_bridge.lookup_item((row.code or "").strip())
                     if (row.code or "").strip().upper().startswith("YS") else None),
    )


@router.post("/products/{product_id}/handoff")
def product_handoff(product_id: int, db: Session = Depends(get_db)):
    """上架到店铺：返回闲鱼发布页地址 + 复制用的预填文本。

    闲鱼没有开放卖家发布接口，所以这里不「代发」，而是替运营把发布页要填的东西准备好：
    前端拿到结果后打开闲鱼发布页（连带把预填文本放进剪贴板），粘贴即成。
    """
    row = db.get(Product, product_id)
    if not row:
        raise HTTPException(404, "商品不存在")
    package = product_service.listing_package(row, product_service.list_skus(db, row.id))
    missing = []
    if not package["title"]:
        missing.append("标题")
    if not package["price"]:
        missing.append("售价")
    if not package["images"]:
        missing.append("主图")
    return JSONResponse(
        {
            "ok": True,
            "publish_url": settings.xianyu_publish_url,
            "text": product_service.listing_text(package),
            "title_length": package["title_length"],
            "title_limit": package["title_limit"],
            "missing": missing,
        }
    )


@router.post("/products/{product_id}/draft")
def product_make_draft(product_id: int, db: Session = Depends(get_db)):
    """仅生成铺货草稿：把商品退回草稿态（还没在闲鱼发布时用）。"""
    row = db.get(Product, product_id)
    if not row:
        raise HTTPException(404, "商品不存在")
    row.status = product_service.PRODUCT_STATUS_DRAFT
    db.commit()
    return _redirect(_product_back(db, product_id), "已置为草稿（未上架）")


@router.post("/products/{product_id}/sourcing-publish")
def product_sourcing_publish(request: Request, product_id: int, db: Session = Depends(get_db)):
    """上架到渔货栈：直接写进渔货栈商品池，并按类目落到渔货栈对应类目下（幂等）。

    闲鱼发布不在这里做 —— 渔货栈自己到店铺发布。
    """
    row = db.get(Product, product_id)
    if not row:
        raise HTTPException(404, "商品不存在")
    ok, text = sourcing_service.publish(db, row, actor=_username(request))
    db.commit()
    return _redirect(_product_back(db, product_id), text, "" if ok else "warn")


@router.post("/products/{product_id}/sourcing-unpublish")
async def product_sourcing_unpublish(request: Request, product_id: int, db: Session = Depends(get_db)):
    """从渔货栈下架（渔货栈商品池置为下架，资料与历史保留）。

    下架会把「下架前的资料快照」一起写进商品池：渔货栈在货源广场与
    商品详情页看到下架提示 + 上架预览那一份资料，知道自己下掉的是哪款货。
    """
    row = db.get(Product, product_id)
    if not row:
        raise HTTPException(404, "商品不存在")
    form = await request.form()
    reason = str(form.get("reason") or "").strip()
    ok, text = sourcing_service.unpublish(db, row, actor=_username(request), reason=reason)
    db.commit()
    return _redirect(_product_back(db, product_id), text, "" if ok else "warn")


@router.post("/sourcing/publish")
async def sourcing_publish_batch(request: Request, db: Session = Depends(get_db)):
    """渔货栈发布管理页的批量上架。"""
    form = await request.form()
    ids = [int(v) for v in form.getlist("product_id") if str(v).strip().isdigit()]
    if not ids:
        return _redirect("/sourcing", "没勾选商品", "warn")
    rows = db.scalars(select(Product).where(Product.id.in_(ids))).all()
    ok_count, failures = sourcing_service.publish_many(db, list(rows), actor=_username(request))
    db.commit()
    if failures:
        head = "；".join(failures[:3]) + ("…" if len(failures) > 3 else "")
        return _redirect("/sourcing", f"上架 {ok_count} 款，{len(failures)} 款没上成：{head}", "warn")
    return _redirect("/sourcing", f"已上架 {ok_count} 款到渔货栈")


@router.get("/sourcing", response_class=HTMLResponse)
def sourcing_admin(
    request: Request,
    q: str = "",
    category: str = "",
    state: str = "all",
    source: str = "all",
    audit: str = "all",
    page: int = 1,
    per_page: int = PRODUCT_PAGE_SIZE,
    db: Session = Depends(get_db),
):
    """渔货栈发布管理：渔枢系统商品 → 渔货栈商品池（供应商商品）的上架与内容管理。

    页面结构对齐渔货栈（货源广场）：搜索 + 类目筛选 + 卡片/列表，
    只是这里管的是"发布"这件事：内容在渔枢系统端维护，一键上架到对应类目。
    分页与商品管理页同口径（默认每页 50，翻页保留筛选条件）。
    """
    if state not in ("all", "listed", "unlisted", "category_failed"):
        state = "all"
    if per_page not in PRODUCT_PAGE_SIZES:
        per_page = PRODUCT_PAGE_SIZE
    rows_all = product_service.list_products(
        db,
        q=q,
        category=category,
        source="" if source == "all" else source,
        audit="" if audit == "all" else audit,
    )
    overview_all = sourcing_service.overview(db, rows_all)
    # 统计与「渔货栈状态」筛选都基于全量：上架状态与类目能不能对上只有算完才知道，
    # 所以先筛再分页（真正贵的是渲染，只渲染当前页那一屏）
    stats = {
        "total": len(overview_all),
        "listed": sum(1 for r in overview_all if r["listed"]),
        "category_failed": sum(1 for r in overview_all if not r["category"].ok),
    }
    if state == "listed":
        filtered = [r for r in overview_all if r["listed"]]
    elif state == "unlisted":
        filtered = [r for r in overview_all if not r["listed"]]
    elif state == "category_failed":
        filtered = [r for r in overview_all if not r["category"].ok]
    else:
        filtered = overview_all
    total_rows = len(filtered)
    pages = max((total_rows + per_page - 1) // per_page, 1)
    try:
        current = int(page)
    except (TypeError, ValueError):
        current = 1
    current = min(max(current, 1), pages)
    overview = filtered[(current - 1) * per_page : current * per_page]
    return render(
        "sourcing.html",
        request,
        heading="渔货栈发布管理",
        sub="商品内容在渔枢系统维护 · 「上架到渔货栈」= 直接推到渔货栈的对应类目下，渔货栈自己去店铺发布",
        rows=overview,
        total=total_rows,
        page=current,
        pages=pages,
        per_page=per_page,
        page_sizes=PRODUCT_PAGE_SIZES,
        stats=stats,
        keyword=q,
        category_filter=category,
        state_filter=state,
        source_filter=source,
        audit_filter=audit,
        categories=product_service.distinct_values(db, Product.category),
        source_choices=product_service.SOURCE_CHOICES,
        audit_choices=product_service.AUDIT_CHOICES,
        source_label=product_service.source_label,
        audit_label=product_service.audit_label,
        product_label=product_service.product_label,
        product_url=product_service.product_url,
    )


@router.get("/products/{product_id}/edit", response_class=HTMLResponse)
def product_edit_form(product_id: int, request: Request, db: Session = Depends(get_db)):
    """编辑商品资料：与「发布商品」同一套表单（预填现有资料），提交到 POST /products/{id}/edit。

    查看页是只读的，所有能改的东西（图片 / 类目 / 价格 / 规格 / 详情 / 属性）都在这里。
    """
    row = db.get(Product, product_id)
    if not row:
        raise HTTPException(404, "商品不存在")
    skus = product_service.list_skus(db, row.id)
    return render(
        "product_new.html",
        request,
        heading="编辑商品",
        sub="",
        is_edit=True,
        product=row,
        upload_max_mb=int(getattr(settings, "upload_image_max_mb", 5) or 0),
        # 前端状态预填：主图 / 规格维度 / SKU 价目（详情图不预填 —— 它们已经拼在详情描述里了）
        edit_state=product_service.edit_state(row, skus),
        # 各输入框的值：name 与发布页表单里的字段名一一对应
        edit_fields=product_service.edit_fields(row),
        # 已填类目拆成一级 / 二级 / 三级，回填到级联下拉
        edit_category=category_service.split_text(row.category),
        category_tree=category_service.options(db),
        freight_templates=shipping_service.freight_names(db) or product_service.FREIGHT_TEMPLATES,
        shipping_choices=shipping_service.METHOD_CHOICES,
        after_sale_options=product_service.AFTER_SALE_OPTIONS,
        spec_templates=product_service.SPEC_TEMPLATES,
        spec_templates_cat2=product_service.SPEC_TEMPLATES_CAT2,
        default_spec_template=product_service.DEFAULT_SPEC_TEMPLATE,
        categories=product_service.distinct_values(db, Product.category),
        suppliers=supplier_service.distinct_values(db, Supplier.name),
        supplier_options=product_service.supplier_options(db),
        source_choices=product_service.SOURCE_CHOICES,
        condition_choices=product_service.CONDITION_CHOICES,
        # 商品标签：可选项 + 配色（前端按名称回填勾选状态）
        tag_options=tag_service.list_tags(db, only_enabled=True),
        tag_kinds=tag_service.kind_map(db),
        shops=shop_service.distinct_values(db, Shop.name),
        xianyu_publish_url=settings.xianyu_publish_url,
    )


@router.post("/products/{product_id}/edit")
async def product_edit(request: Request, product_id: int, db: Session = Depends(get_db)):
    row = db.get(Product, product_id)
    if not row:
        raise HTTPException(404, "商品不存在")
    form = await request.form()
    try:
        product_service.update_product(db, row, form)
        # 编辑页的 SKU 矩阵是「全量」的：矩阵里删掉的规格，库里也一并删掉
        product_service.sync_skus_json(db, row, str(form.get("skus_json", "") or ""))
    except ValueError as exc:
        return _redirect(_product_back(db, product_id), str(exc), "warn")
    # 已经上架到渔货栈的：改完资料顺手回传，别让渔货栈一直看着旧资料
    # （回传失败也不影响渔枢系统这边保存成功 —— 页面会挂「资料已过期」提示，可手动重传）
    tip, kind = "已保存", ""
    try:
        if sourcing_service.published_item(db, row) is not None:
            ok, text = sourcing_service.publish(db, row, actor=_username(request))
            db.commit()
            if ok:
                tip = f"已保存并回传渔货栈：{text}"
            else:
                tip, kind = f"已保存，但回传渔货栈失败：{text}", "warn"
    except Exception as exc:  # noqa: BLE001  回传失败不能让资料保存失败
        logger.warning("商品 %s 保存后回传渔货栈失败：%s", product_id, exc)
        tip, kind = "已保存，但回传渔货栈失败（可到商品详情页手动重传）", "warn"
    return _redirect(_product_back(db, product_id), tip, kind)


@router.post("/products/{product_id}/delete")
def product_delete(product_id: int, db: Session = Depends(get_db)):
    row = db.get(Product, product_id)
    if not row:
        raise HTTPException(404, "商品不存在")
    product_service.delete_product(db, row)
    return _redirect("/products", "商品已删除（订单本身保留，只是解除关联）", "warn")


@router.post("/products/{product_id}/skus")
async def product_sku_create(request: Request, product_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        product_service.add_sku(db, product_id, form)
    except ValueError as exc:
        return _redirect(_product_back(db, product_id), str(exc), "warn")
    return _redirect(_product_back(db, product_id), "已添加规格")


@router.post("/products/skus/{sku_id}/delete")
def product_sku_delete(sku_id: int, db: Session = Depends(get_db)):
    sku = db.get(ProductSku, sku_id)
    if not sku:
        raise HTTPException(404, "规格不存在")
    product_id = sku.product_id
    product_service.delete_sku(db, sku)
    return _redirect(_product_back(db, product_id), "规格已删除", "warn")


@router.post("/products/{product_id}/listing")
async def product_listing(request: Request, product_id: int, db: Session = Depends(get_db)):
    """上架 / 下架（闲鱼侧没有开放接口，这里记录我们自己这边的上架状态）。"""
    row = db.get(Product, product_id)
    if not row:
        raise HTTPException(404, "商品不存在")
    form = await request.form()
    listed = str(form.get("listed", "1")) == "1"
    product_service.set_listing(db, row, listed)
    return _redirect(_product_back(db, product_id), "已标记为在售" if listed else "已标记为下架")


@router.post("/products/{product_id}/audit")
async def product_audit(request: Request, product_id: int, db: Session = Depends(get_db)):
    """审核：通过 / 驳回（驳回理由记在商品上，列表能看）。"""
    row = db.get(Product, product_id)
    if not row:
        raise HTTPException(404, "商品不存在")
    form = await request.form()
    status = str(form.get("audit_status", "") or "").strip()
    if status not in dict(product_service.AUDIT_CHOICES):
        return _redirect(_product_back(db, product_id), "审核状态不合法", "warn")
    product_service.set_audit(db, row, status, note=str(form.get("audit_note", "") or ""))
    return _redirect(_product_back(db, product_id), f"审核已标记：{dict(product_service.AUDIT_CHOICES)[status]}")


@router.post("/products/{product_id}/gen-skus")
async def product_gen_skus(request: Request, product_id: int, db: Session = Depends(get_db)):
    """按规格定义批量生成 SKU 组合（如 颜色[黑,白] × 容量[128G] → 2 个 SKU）。"""
    row = db.get(Product, product_id)
    if not row:
        raise HTTPException(404, "商品不存在")
    form = await request.form()
    text = str(form.get("spec_input", "") or "")
    defs = product_service.parse_spec_defs(text)
    if not defs:
        return _redirect(
            f"/products/{product_id}",
            "规格没填对：一行一个维度，如「颜色: 黑, 白」",
            "warn",
        )
    created = product_service.generate_skus(db, row, defs)
    db.commit()
    return _redirect(_product_back(db, product_id), f"已按规格生成 {created} 个 SKU")


@router.get("/products/{product_id}/listing.json")
def product_listing_json(product_id: int, db: Session = Depends(get_db)):
    """上架预填包（JSON 下载）：闲鱼发布页要填的字段一次带走。"""
    row = db.get(Product, product_id)
    if not row:
        raise HTTPException(404, "商品不存在")
    payload = product_service.listing_package(row, product_service.list_skus(db, row.id))
    filename = f"{row.code or ('product-' + str(row.id))}.json"
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------ 资金结算
@router.get("/finance", response_class=HTMLResponse)
def finance(
    request: Request,
    kind: str = "",
    status: str = "",
    counterparty: str = "",
    granularity: str = "order",
    day_from: str = "",
    day_to: str = "",
    q: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    """资金板块：公司自营应收 / 应付 / 退款给买家，默认按明细（逐笔）展示，也可按天 · 按月聚合。"""
    if granularity not in SETTLEMENT_GRANULARITIES:
        granularity = "order"
    conditions = finance_service.filters(
        kind=kind,
        status=status,
        counterparty=counterparty,
        day_from=day_from,
        day_to=day_to,
        keyword=q,
    )
    stat = finance_service.summary(db)
    totals = finance_service.totals(db, conditions)

    # 按笔逐单展示要分页；按天 / 按月是聚合结果，直接全量列出
    rows: list[dict] = []
    page_count = 1
    total = 0
    if granularity == "order":
        total = finance_service.count_rows(db, conditions)
        page_count = max(1, -(-total // FINANCE_PAGE_SIZE))
        page = min(max(1, page), page_count)
        rows = finance_service.order_rows(
            db, conditions, offset=(page - 1) * FINANCE_PAGE_SIZE, limit=FINANCE_PAGE_SIZE
        )
    else:
        rows = finance_service.aggregate_rows(db, conditions, granularity)

    return render(
        "finance.html",
        request,
        heading="资金结算",
        sub="公司自营应收 / 应付、上游供应商结算台账",
        stat=stat,
        totals=totals,
        rows=rows,
        page=page,
        per_page=FINANCE_PAGE_SIZE,
        page_count=page_count,
        total_rows=total,
        granularity=granularity,
        granularity_labels=SETTLEMENT_GRANULARITY_LABELS,
        kind_labels=SETTLEMENT_KIND_LABELS,
        status_labels=SETTLEMENT_STATUS_LABELS,
        statuses=SETTLEMENT_STATUSES,
        kinds=SETTLEMENT_KINDS,
        counterparties=finance_service.counterparties(db, kind),
        filters={
            "kind": kind,
            "status": status,
            "counterparty": counterparty,
            "day_from": day_from,
            "day_to": day_to,
            "q": q,
            "granularity": granularity,
        },
        today=beijing_now().strftime("%Y-%m-%d"),
        month=beijing_now().strftime("%Y-%m"),
    )


@router.post("/finance/settle")
async def finance_settle(request: Request, db: Session = Depends(get_db)):
    """标记已结算：支持选中若干笔，或按「类型 + 对象 + 期间」整批核销。"""
    form = await request.form()
    ids = [int(v) for v in form.getlist("ids") if str(v).strip().isdigit()]
    kind = str(form.get("kind", "")).strip()
    counterparty = str(form.get("counterparty", "")).strip()
    day = str(form.get("day", "")).strip()
    month = str(form.get("month", "")).strip()
    note = str(form.get("note", "")).strip()
    if not (ids or day or month):
        return _redirect("/finance", "请先选中要结算的流水，或选择要整期结算的期间", "warn")
    try:
        count = finance_service.settle(
            db,
            ids=ids or None,
            kind=kind,
            counterparty=counterparty,
            day=day,
            month=month,
            actor=_username(request),
            note=note,
        )
    except ValueError as exc:
        return _redirect("/finance", str(exc), "warn")
    scope = day or month or f"选中 {len(ids)} 笔"
    if count == 0:
        return _redirect("/finance", f"{scope} 没有待结算的流水", "warn")
    return _redirect("/finance", f"已结算 {count} 笔（{scope}）")


@router.post("/finance/unsettle")
async def finance_unsettle(request: Request, db: Session = Depends(get_db)):
    """撤销结算（点错了）：退回待结算。"""
    form = await request.form()
    ids = [int(v) for v in form.getlist("ids") if str(v).strip().isdigit()]
    count = finance_service.unsettle(db, ids)
    if not count:
        return _redirect("/finance", "没有可撤销的结算记录", "warn")
    return _redirect("/finance", f"已撤销 {count} 笔结算，退回待结算")


# ------------------------------------------------------------------ 原始消息
@router.get("/messages", response_class=HTMLResponse)
def messages(
    request: Request,
    status: str = "",
    kind: str = "",
    chat_id: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    # 飞书消息页只给管理员开放：同步与重扫会动到群数据，普通账号看不到也不需要
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    stmt = db.query(RawMessage)
    if status:
        stmt = stmt.filter(RawMessage.parse_status == status)
    if kind:
        stmt = stmt.filter(RawMessage.kind == kind)
    if chat_id:
        stmt = stmt.filter(RawMessage.chat_id == chat_id)
    # 按消息真实发送时间倒序（补录的历史消息 id 更大、时间更早，按 id 排会把老消息顶到最前面）；
    # 时间缺失的排最后，同秒消息再用 id 兜底保持稳定顺序
    stmt = stmt.order_by(RawMessage.msg_time.desc().nullslast(), RawMessage.id.desc())
    items, total, pages, page = _paginate(stmt, page, 30)
    chats = db.scalars(select(SyncState)).all()
    counts = {
        s: db.scalar(select(func.count(RawMessage.id)).where(RawMessage.parse_status == s)) or 0
        for s in ("pending", "parsed", "ignored", "filtered", "failed", "duplicate")
    }
    kind_counts = {
        k: db.scalar(select(func.count(RawMessage.id)).where(RawMessage.kind == k)) or 0
        for k in ("template", "screenshot", "intent", "other")
    }
    intent_counts = {
        k: db.scalar(select(func.count(RawMessage.id)).where(RawMessage.intent == k)) or 0
        for k in ("refund", "cancel")
    }
    notify_counts = {
        s: db.scalar(select(func.count(RawMessage.id)).where(RawMessage.notify_status == s)) or 0
        for s in ("sent", "held", "failed")
    }
    # 当前页订单的撤单状态：撤过单的不再提供「不是订单 / 补发回执」，只显示已撤单
    order_ids = [m.order_id for m in items if m.order_id]
    orders_meta = {
        o.id: bool(o.is_recalled)
        for o in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
    } if order_ids else {}
    return render(
        "messages.html",
        request,
        heading="飞书消息",
        sub=f"共 {total} 条消息",
        messages=items,
        total=total,
        page=page,
        per_page=30,
        pages=pages,
        counts=counts,
        kind_counts=kind_counts,
        intent_counts=intent_counts,
        notify_counts=notify_counts,
        chats=chats,
        orders_meta=orders_meta,
        # 人工标记学到的解析策略：排除词 / 订单特征词 + 最近的标记记录
        policy_stats=policy_service.stats(db),
        policy_exclude=policy_service.list_phrases(db, policy_service.KIND_EXCLUDE),
        policy_force=policy_service.list_phrases(db, policy_service.KIND_FORCE),
        policy_refund=policy_service.list_phrases(db, policy_service.KIND_REFUND),
        policy_feedbacks=policy_service.recent_feedbacks(db, 8),
        policy_feedback_phrases=policy_service.feedback_phrases,
        policy_labels=policy_service.LABEL_TEXT,
        filters={"status": status, "kind": kind, "chat_id": chat_id},
        feishu_ready=settings.feishu_enabled,
        notify_mode=settings.notify_mode,
    )


@router.post("/messages/sync")
async def messages_sync(request: Request, db: Session = Depends(get_db)):
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    form = await request.form()
    chat_id = str(form.get("chat_id_manual", "")).strip() or str(form.get("chat_id", "")).strip()
    if not chat_id:
        return _redirect("/messages", "请先选择或填写群 chat_id", "warn")
    try:
        days = int(str(form.get("days", "1")))
    except ValueError:
        days = 1
    # 毫秒时间戳要按真实 UTC 时刻算（naive 的北京时间 .timestamp() 会多算 8 小时）
    start_ms = int((time.time() - days * 86400) * 1000)
    try:
        st = sync_chat(chat_id, start_ms=start_ms)
        return _redirect(
            "/messages",
            f"同步完成：拉取 {st.fetched} / 新增 {st.created} / 解析 {st.parsed} / 忽略 {st.ignored}",
        )
    except Exception as exc:  # noqa: BLE001
        return _redirect("/messages", f"同步失败：{exc}", "danger")


@router.post("/messages/reparse")
def messages_reparse(db: Session = Depends(get_db)):
    db.execute(RawMessage.__table__.update().where(
        RawMessage.parse_status.in_(("ignored", "failed"))
    ).values(parse_status="pending", ignore_reason="", parse_error=""))
    db.commit()
    result = process_pending(db)
    img_result = process_pending_images(db)
    return _redirect(
        "/messages",
        f"重解析完成：{result['parsed'] + img_result['parsed']} 成功 / "
        f"{len(result['errors']) + len(img_result['errors'])} 失败",
    )


@router.post("/messages/{message_id}/notify")
def message_resend_notify(message_id: str, db: Session = Depends(get_db)):
    """补发「已收到并处理」回执（通知失败或未通知时使用）。"""
    msg = db.scalar(select(RawMessage).where(RawMessage.message_id == message_id))
    if not msg:
        raise HTTPException(404, "消息不存在")
    order = db.get(Order, msg.order_id) if msg.order_id else None
    if not order:
        return _redirect("/messages", "该消息没有已归档的订单，无法回执", "warn")
    status, err = notify_processed(msg, order, get_client(), session=db, force=True)
    msg.notify_status = status
    msg.notify_error = err
    msg.notified_at = now() if status == "sent" else None
    db.commit()
    if status == "sent":
        return _redirect("/messages", "回执已发送")
    return _redirect("/messages", f"回执发送失败：{err or '未知原因'}", "danger")


@router.post("/messages/{message_id}/mark-not-order")
def message_mark_not_order(message_id: str, request: Request, db: Session = Depends(get_db)):
    """人工标记：这条消息不是订单（解析误判）。

    一次做完三件事：
      ① 误判归档的订单撤单（只留档、不参与统计与后续流程）并写订单处理日志；
      ② 消息改判为「人工确认不是订单」；
      ③ 记录这次判断并从消息里学话术 —— 下一条同类通告直接过滤，不用再人工标。
    """
    msg = db.scalar(select(RawMessage).where(RawMessage.message_id == message_id))
    if not msg:
        raise HTTPException(404, "消息不存在")
    operator = _username(request)
    order = db.get(Order, msg.order_id) if msg.order_id else None
    recalled = False
    if order is not None and not order.is_recalled:
        # 撤单口径与群里撤回一致（只留档、作废结算流水），撤单来源写清楚是人工标记；
        # 订单处理日志由 mark_recalled 自己写（撤单来源：人工标记：…）
        recall_service.mark_recalled(
            db,
            order,
            msg,
            recaller=operator,
            reason="人工标记：这条消息不是订单（解析误判归档）",
            commit=False,
        )
        recalled = True
    _row, phrases = policy_service.record(
        db,
        msg,
        policy_service.LABEL_NOT_ORDER,
        operator=operator,
        order_id=order.id if order else None,
        note="飞书消息页人工标记：不是订单",
        commit=False,
    )
    msg.parse_status = "filtered"
    msg.kind = "other"
    msg.ignore_reason = "人工标记：不是订单消息" + (
        f"（误判归档的订单 #{order.id} 已撤单留档）" if recalled else ""
    )
    db.commit()
    parts = ["已标记为「不是订单」"]
    if recalled:
        parts.append(f"误判订单 #{order.id} 已撤单留档")
    if phrases:
        parts.append("学到话术：" + "、".join(phrases[:3]))
    return _redirect("/messages", "；".join(parts))


@router.post("/messages/{message_id}/mark-order")
def message_mark_order(message_id: str, request: Request, db: Session = Depends(get_db)):
    """人工标记：这条消息其实是订单（规则漏判）。记录后立刻重新解析归档。"""
    msg = db.scalar(select(RawMessage).where(RawMessage.message_id == message_id))
    if not msg:
        raise HTTPException(404, "消息不存在")
    operator = _username(request)
    _row, phrases = policy_service.record(
        db,
        msg,
        policy_service.LABEL_ORDER,
        operator=operator,
        note="飞书消息页人工标记：其实是订单",
        commit=False,
    )
    msg.parse_status = "pending"
    msg.kind = ""
    msg.ignore_reason = ""
    msg.parse_error = ""
    msg.intent = ""
    msg.intent_note = ""
    db.commit()
    # 学到「订单特征词」后同一条消息就已经能判成订单：直接重新解析归档
    process_pending(db)
    if msg.msg_type == "image":
        process_pending_images(db)
    db.refresh(msg)
    learned = f"（学到特征：{'、'.join(phrases[:3])}）" if phrases else ""
    if msg.order_id:
        return _redirect("/messages", f"已按订单归档：订单 #{msg.order_id}{learned}")
    return _redirect(
        "/messages",
        f"已记为「其实是订单」，但这次没能归档（{msg.ignore_reason or msg.parse_error or '请检查消息内容'}）{learned}",
        "warn",
    )


@router.post("/messages/{message_id}/mark-refund")
def message_mark_refund(message_id: str, request: Request, db: Session = Depends(get_db)):
    """人工标记：这条消息其实是退单（模板消息首行写「此单退」之类，被当成新订单归档了）。

    一次做完三件事：
      ① 误建的那笔订单撤单留档（退单不该凭空建单）；
      ② 按消息里的手机号 / 收货人 / 地址去存量订单里找原订单，找到就标记退单
         （已发货的按口径不受理：让用户收到货后再退）；
      ③ 记住这句话的说法，下次带同样说法的模板消息直接按退单处理。
    """
    msg = db.scalar(select(RawMessage).where(RawMessage.message_id == message_id))
    if not msg:
        raise HTTPException(404, "消息不存在")
    operator = _username(request)
    parsed = parse_text(msg.raw_text)
    fields = parsed.fields if parsed.matched else {}
    keyword = intent_service.classify(msg.raw_text, template=True).keyword or "退单"

    # ① 误建的新订单撤单留档
    wrong = db.get(Order, msg.order_id) if msg.order_id else None
    recalled = False
    if wrong is not None and not wrong.is_recalled:
        recall_service.mark_recalled(
            db,
            wrong,
            msg,
            recaller=operator,
            reason="人工标记：这条消息其实是退单（误建成新订单）",
            commit=False,
        )
        recalled = True
        db.flush()  # 立刻生效：下面找原订单时要排除掉这笔刚撤单的

    # ② 找原订单 -> 标记退单（排除误建的那笔，否则会匹配到它自己）
    original = refund_service.match_original(
        db, msg, fields, exclude_id=wrong.id if wrong else None
    )
    accepted = False
    if original is not None:
        _order, accepted = refund_service.mark_refund(
            db, original, msg, keyword, commit=False
        )

    # ③ 记住这句话的说法
    _row, phrases = policy_service.record(
        db,
        msg,
        policy_service.LABEL_REFUND,
        operator=operator,
        order_id=original.id if original else None,
        note="飞书消息页人工标记：其实是退单",
        commit=False,
    )

    msg.intent = intent_service.INTENT_REFUND
    msg.kind = "intent"
    msg.parse_status = "parsed"
    if original is not None:
        msg.order_id = original.id
        head_note = (
            f"原订单 #{original.id} 已标记退单"
            if accepted
            else f"原订单 #{original.id} 已发货：退单未受理，请让用户收到货后再退"
        )
    else:
        head_note = "没匹配到原订单：请到订单页手工处理（手机号 / 收货人 / 地址都没对上）"
    if recalled:
        head_note += f"；误建的订单 #{wrong.id} 已撤单留档"
    msg.intent_note = f"人工标记「其实是退单」（{keyword}）：{head_note}"
    msg.ignore_reason = ""
    db.commit()

    parts = [f"已标记为「其实是退单」（{keyword}）", head_note]
    if phrases:
        parts.append("学到话术：" + "、".join(phrases[:3]))
    return _redirect("/messages", "；".join(parts))


@router.post("/messages/policy/phrase")
async def message_policy_add_phrase(request: Request, db: Session = Depends(get_db)):
    """手工加一条策略话术：排除词（命中判「不是订单」）/ 订单特征词（命中判「是订单」）。"""
    form = await request.form()
    phrase = str(form.get("phrase", "")).strip()
    kind = str(form.get("kind", policy_service.KIND_EXCLUDE)).strip()
    ok, message = policy_service.add_phrase(db, phrase, kind, operator=_username(request))
    return _redirect("/messages", message, "" if ok else "warn")


@router.post("/messages/policy/phrase/{phrase_id}/delete")
def message_policy_delete_phrase(phrase_id: int, db: Session = Depends(get_db)):
    if not policy_service.delete_phrase(db, phrase_id):
        return _redirect("/messages", "这条策略已不存在", "warn")
    return _redirect("/messages", "策略已删除（已过滤 / 已归档的消息不受影响）")


@router.post("/messages/policy/feedback/{feedback_id}/delete")
def message_policy_delete_feedback(feedback_id: int, db: Session = Depends(get_db)):
    """撤销一次人工标记：这条样本的指纹不再生效（学到的话术仍在，可单独删）。"""
    if not policy_service.delete_feedback(db, feedback_id):
        return _redirect("/messages", "这条标记记录已不存在", "warn")
    return _redirect("/messages", "已撤销这次人工标记（订单状态如需恢复请到订单页处理）")


# ------------------------------------------------------------------ 规则（管理员）
@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    rules = get_rules()
    text = settings.rules_path.read_text(encoding="utf-8") if settings.rules_path.exists() else ""
    return render(
        "rules.html",
        request,
        heading="解析规则",
        sub="按飞书群里的真实模板调整字段标签与正则",
        rules=rules,
        yaml_text=text,
        rules_path=str(settings.rules_path),
    )


@router.post("/rules")
async def rules_save(request: Request):
    if not _admin_user(request):
        return _admin_or_redirect(request)
    form = await request.form()
    text = str(form.get("yaml_text", ""))
    try:
        import yaml

        data = yaml.safe_load(text) or {}
        parse_rule_dict(data)
    except Exception as exc:  # noqa: BLE001
        return _redirect("/rules", f"规则校验失败：{exc}", "danger")
    settings.rules_path.parent.mkdir(parents=True, exist_ok=True)
    settings.rules_path.write_text(text, encoding="utf-8")
    get_rules(force=True)
    return _redirect("/rules", "规则已保存并生效")


@router.post("/rules/reset")
def rules_reset(request: Request):
    if not _admin_user(request):
        return _admin_or_redirect(request)
    backup = settings.rules_path
    default = BASE_DIR / "config" / "parse_rules.yaml"
    if default.exists() and backup != default:
        backup.write_text(default.read_text(encoding="utf-8"), encoding="utf-8")
    get_rules(force=True)
    return _redirect("/rules", "已恢复默认规则", "warn")


# ------------------------------------------------------------------ 设置（管理员）
# 设置页「菜鸟测试查询」的结果：POST 查完先放这里，跳回设置页读一次就删 ——
# 结果里有报文与菜鸟原文，塞不进 cookie，也不该留在地址栏里。
_CAINIAO_TESTS: dict[str, dict] = {}
_CAINIAO_TEST_KEEP = 12


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    test_key = request.query_params.get("cainiao_test", "")
    cainiao_test_result = _CAINIAO_TESTS.pop(test_key, None) if test_key else None
    chats = db.scalars(select(SyncState).order_by(SyncState.id.desc())).all()
    bot_chats: list[dict] = []
    error = ""
    if settings.feishu_enabled:
        try:
            bot_chats = get_client().list_bot_chats()
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
    return render(
        "settings.html",
        request,
        heading="系统设置",
        sub="飞书应用与同步配置",
        template_sample=STANDARD_TEMPLATE,
        chats=chats,
        bot_chats=bot_chats,
        error=error,
        settings=settings,
        env_chats=settings.feishu_chat_ids,
        masked_secret=mask_secret(settings.feishu_app_secret),
        cainiao_masked_secret=mask_secret(settings.cainiao_app_secret),
        cainiao_ready=logistics_service.is_enabled(),
        cainiao_test_result=cainiao_test_result,
        env_writable=is_writable(),
        env_path=str(ENV_PATH),
        notify_active_now=is_active_time(),
        notify_min_interval_minutes=max(settings.notify_min_interval_seconds, 0) // 60,
        notify_held_count=db.scalar(
            select(func.count(RawMessage.id)).where(RawMessage.notify_status == "held")
        )
        or 0,
        voice=voice_service.payload(),
        # 通知模板：给设置页「填入当前默认模板」按钮用的原文
        notify_defaults={
            "receipt": DEFAULT_RECEIPT_TEMPLATE,
            "refund_detail": DEFAULT_REFUND_DETAIL_TEMPLATE,
        },
        daily={
            "enabled": settings.daily_summary_enabled,
            "hour": settings.daily_summary_hour,
            "chat_ids": settings.daily_summary_chat_ids,
            "at_users": settings.daily_summary_at_users,
            "skip_empty": settings.daily_summary_skip_empty,
            "last": daily_summary_service.load_state(),
            "next": daily_summary_service.next_run_time().strftime("%Y-%m-%d %H:%M"),
            "target_date": daily_summary_service.default_target_date().isoformat(),
            "today": beijing_now().date().isoformat(),
        },
        backup_latest=backup_service.latest_backup(),
        backup_count=len(backup_service.list_backups()),
        backup_next=backup_service.next_backup_time().strftime("%Y-%m-%d %H:%M"),
        backup_enabled=settings.backup_enabled,
        backup_hour=settings.backup_hour,
        keep_days=settings.backup_keep_days,
        # 机器人 / 应用自己发的消息（cli_xxx / app_xxx）没有真实姓名，不算「未识别」
        sender_missing=db.scalar(
            select(func.count(RawMessage.id)).where(
                RawMessage.sender_id != "",
                RawMessage.sender_name == "",
                ~RawMessage.sender_id.like("cli\\%", escape="\\"),
                ~RawMessage.sender_id.like("app\\%", escape="\\"),
            )
        )
        or 0,
        sender_total=db.scalar(
            select(func.count(RawMessage.id)).where(RawMessage.sender_id != "")
        )
        or 0,
    )


@router.get("/message-center", response_class=HTMLResponse)
def message_center_page(request: Request):
    """消息中心：系统对外发的每一条消息，逐个标明「发群里还是发私信」。"""
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    rows = [
        {"kind": m, "channels": message_rules.channels_of(m.key)} for m in KINDS
    ]
    return render(
        "message_center.html",
        request,
        heading="消息中心",
        sub="系统对外下发的每一条消息，逐个定发送方式",
        rows=rows,
        channels=CHANNELS,
        notify_mode=settings.notify_mode,
        tpl_card="/settings#templates",
    )


@router.post("/message-center")
async def message_center_save(request: Request):
    """保存每条消息的发送方式到 .env 并立即生效。"""
    if not _admin_user(request):
        return _admin_or_redirect(request)
    form = await request.form()
    updates: dict[str, str] = {}
    for kind in KINDS:
        picked = [
            ch
            for ch, _ in CHANNELS
            if str(form.get(f"{kind.key}__{ch}", "")).strip() in ("1", "on", "true", "yes")
        ]
        updates[kind.env_key] = message_rules.set_route(kind.key, picked)
    if not is_writable():
        return _redirect("/message-center", "已在运行中配置生效（.env 不可写，重启后会丢失）", "warn")
    try:
        write_updates(updates)
    except Exception as exc:  # noqa: BLE001
        return _redirect("/message-center", f"写入 .env 失败（本次已生效）：{exc}", "danger")
    return _redirect("/message-center", "各消息的发送方式已保存并立即生效")


# 老路径：v1.60 之前消息中心挂在「系统设置」下面（/settings/messages）。
# 页面已经独立成一级菜单，路径也跟着独立；这里保留跳转，老书签、缓存里的老页面都不会 404。
@router.get("/settings/messages")
def legacy_settings_messages():
    return RedirectResponse("/message-center", status_code=301)


@router.post("/settings/messages")
async def legacy_settings_messages_save():
    """老表单（浏览器缓存里的旧页面）提交时按原样转到新地址，勾选不会白改。"""
    return RedirectResponse("/message-center", status_code=307)


@router.get("/settings/senders", response_class=HTMLResponse)
def settings_senders_page(request: Request, db: Session = Depends(get_db)):
    """发送人对照表：open_id 与真实姓名的对应关系。

    谁还没认出来、谁已经不在群里一眼看清，配合「一键补全」把缺的姓名补上。
    """
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked

    people_map: dict[str, dict] = {}
    for sid, name, cnt, last, chat in db.execute(
        select(
            RawMessage.sender_id,
            func.max(RawMessage.sender_name),
            func.count(RawMessage.id),
            func.max(RawMessage.msg_time),
            func.max(RawMessage.chat_id),
        )
        .where(RawMessage.sender_id != "")
        .group_by(RawMessage.sender_id)
    ).all():
        people_map[sid] = {
            "id": sid,
            "name": str(name or "").strip(),
            "msgs": int(cnt or 0),
            "orders": 0,
            "last": last,
            "chat_id": str(chat or ""),
        }
    for sid, name, cnt in db.execute(
        select(Order.sender_id, func.max(Order.sender_name), func.count(Order.id))
        .where(Order.sender_id != "")
        .group_by(Order.sender_id)
    ).all():
        row = people_map.setdefault(
            sid,
            {"id": sid, "name": "", "msgs": 0, "orders": 0, "last": None, "chat_id": ""},
        )
        row["orders"] = int(cnt or 0)
        if not row["name"]:
            row["name"] = str(name or "").strip()

    # 群成员名单：既能补出姓名，也能看出「这人已经不在群里了」
    members: dict[str, str] = {}
    error = ""
    if settings.feishu_enabled:
        try:
            client = get_client()
            for chat_id in sorted(
                {r["chat_id"] for r in people_map.values() if r["chat_id"]}
            ):
                members.update(client.get_chat_members(chat_id) or {})
        except Exception as exc:  # noqa: BLE001
            error = str(exc)

    people: list[dict] = []
    for sid, row in people_map.items():
        name = row["name"] or members.get(sid, "")
        if ingest_service.is_app_sender(sid):
            name = ingest_service.APP_SENDER_NAME
        people.append(
            {
                "id": sid,
                "name": name,
                "missing": not name and sid.startswith("ou_"),
                "in_group": sid in members,
                "msgs": row["msgs"],
                "orders": row["orders"],
                "last": row["last"],
            }
        )
    # 没认出来的排最前面，其余按最近发言时间倒序
    people.sort(key=lambda p: (not p["missing"], str(p["last"] or "")), reverse=True)
    missing_count = sum(1 for p in people if p["missing"])
    return render(
        "senders.html",
        request,
        heading="发送人对照表",
        sub=f"共 {len(people)} 个发送人 · {missing_count} 个还没拿到真实姓名",
        people=people,
        missing_count=missing_count,
        members_count=len(members),
        feishu_ready=settings.feishu_enabled,
        error=error,
    )


@router.post("/settings/save")
async def settings_save(request: Request):
    """保存飞书凭证到 .env 并立即生效（无需重启）。"""
    if not _admin_user(request):
        return _admin_or_redirect(request)
    form = await request.form()
    app_id = str(form.get("feishu_app_id", "")).strip()
    chat_ids = str(form.get("feishu_chat_ids", "")).strip()
    secret = str(form.get("feishu_app_secret", "")).strip()

    # 同一张表单也可能只改页面选项（自动刷新频率 / 截图重识别间隔）：
    # 这时没有飞书字段，绝不能把凭证清空
    page_opts = "feishu_app_id" not in form and any(
        key in form for key in ("auto_refresh_sec", "ocr_reocr_minutes")
    )
    updates: dict[str, str] = {}
    if "auto_refresh_sec" in form:
        try:
            settings.auto_refresh_sec = max(0, int(str(form.get("auto_refresh_sec", "60"))))
        except ValueError:
            pass
        updates["AUTO_REFRESH_SEC"] = str(settings.auto_refresh_sec)
    if "ocr_reocr_minutes" in form:
        try:
            settings.ocr_reocr_minutes = max(0, int(str(form.get("ocr_reocr_minutes", "15"))))
        except ValueError:
            pass
        updates["OCR_REOCR_MINUTES"] = str(settings.ocr_reocr_minutes)
    if not page_opts:
        updates["FEISHU_APP_ID"] = app_id
        updates["FEISHU_CHAT_IDS"] = chat_ids
        # 掩码原样提交表示不修改
        if secret and "****" not in secret:
            updates["FEISHU_APP_SECRET"] = secret
            settings.feishu_app_secret = secret
        settings.feishu_app_id = app_id
        settings.feishu_chat_ids = [x.strip() for x in chat_ids.split(",") if x.strip()]

    writable = is_writable()
    if writable:
        try:
            write_updates(updates)
        except Exception as exc:  # noqa: BLE001
            reset_client()
            return _redirect("/settings", f"写入 .env 失败（本次已生效）：{exc}", "danger")
    reset_client()
    if page_opts:
        tips: list[str] = []
        if "auto_refresh_sec" in form:
            tips.append(
                f"首页自动刷新「每 {settings.auto_refresh_sec} 秒」"
                if settings.auto_refresh_sec
                else "已关闭首页自动刷新"
            )
        if "ocr_reocr_minutes" in form:
            tips.append(
                f"截图重识别「每 {settings.ocr_reocr_minutes} 分钟一轮」"
                if settings.ocr_reocr_minutes
                else "已关闭截图自动重识别"
            )
        tip = "，".join(tips)
        if not writable:
            return _redirect("/settings", tip + "（.env 不可写，重启后会丢失）", "warn")
        return _redirect("/settings", tip + "，保存后立即生效")
    if not writable:
        return _redirect("/settings", "已在运行中配置生效（.env 不可写，重启后会丢失）", "warn")
    return _redirect("/settings", "飞书配置已保存到 .env 并立即生效")


@router.post("/settings/logistics")
async def settings_logistics(request: Request):
    """保存菜鸟物流查询配置到 .env 并立即生效（网关地址 / 接口名 / 秘钥 / CPCODE）。"""
    if not _admin_user(request):
        return _admin_or_redirect(request)
    form = await request.form()

    def _int(key: str, default: int) -> int:
        try:
            return max(3, int(str(form.get(key, default) or default)))
        except ValueError:
            return default

    enabled = str(form.get("cainiao_enabled", "")).strip() in ("1", "on", "true", "yes")
    updates = {
        "CAINIAO_ENABLED": "1" if enabled else "0",
        "CAINIAO_GATEWAY_URL": str(form.get("cainiao_gateway_url", "")).strip(),
        "CAINIAO_MSG_TYPE": str(form.get("cainiao_msg_type", "")).strip(),
        "CAINIAO_CP_CODE": str(form.get("cainiao_cp_code", "")).strip(),
        "CAINIAO_APP_CODE": str(form.get("cainiao_app_code", "")).strip(),
        "CAINIAO_TIMEOUT_SEC": str(_int("cainiao_timeout_sec", 15)),
        # 报文结构：arg0 = {"arg0":{...}} / flat = 平铺；认不出按 arg0
        "CAINIAO_INTERFACE_STYLE": (
            "flat"
            if str(form.get("cainiao_interface_style", "")).strip().lower() == "flat"
            else "arg0"
        ),
    }
    # 秘钥是掩码回显的：原样提交表示不修改
    secret = str(form.get("cainiao_app_secret", "")).strip()
    if secret and "****" not in secret:
        updates["CAINIAO_APP_SECRET"] = secret
        settings.cainiao_app_secret = secret
    settings.cainiao_enabled = enabled
    settings.cainiao_gateway_url = updates["CAINIAO_GATEWAY_URL"]
    settings.cainiao_msg_type = updates["CAINIAO_MSG_TYPE"]
    settings.cainiao_cp_code = updates["CAINIAO_CP_CODE"]
    settings.cainiao_app_code = updates["CAINIAO_APP_CODE"]
    settings.cainiao_timeout_sec = int(updates["CAINIAO_TIMEOUT_SEC"])
    settings.cainiao_interface_style = updates["CAINIAO_INTERFACE_STYLE"]

    writable = is_writable()
    if writable:
        try:
            write_updates(updates)
        except Exception as exc:  # noqa: BLE001
            return _redirect("/settings", f"写入 .env 失败（本次已生效）：{exc}", "danger")
    if not writable:
        return _redirect("/settings", "菜鸟物流查询已配置（.env 不可写，重启后会丢失）", "warn")
    tip = "菜鸟物流查询已开启" if enabled else "菜鸟物流查询已关闭"
    return _redirect("/settings", f"{tip}，保存后立即生效")


@router.post("/settings/logistics/test")
async def settings_logistics_test(request: Request):
    """菜鸟物流查询的「测试查询」：填一个真实运单号，当场把这条链路走一遍。

    为什么要有这个按钮：菜鸟的报文/签名/CPCODE 配错只回一句 S02/S18，光看文档猜不出来；
    这里直接把**发出去的报文**和**菜鸟回的原文**摆出来，配没配对一眼就知道。
    不写订单、不改状态，纯粹试。
    """
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    form = await request.form()
    mail_no = str(form.get("test_mail_no", "") or "").strip()
    if not mail_no:
        return _redirect("/settings", "先在「测试查询」里填一个运单号", "warn")
    # cpCode 留空就按单号自动识别（顺丰 SF / 圆通 YTO …），填了以填的为准
    cp_code = str(form.get("test_cp_code", "") or "").strip() or logistics_service.guess_cp_code(mail_no)
    phone = logistics_service.phone_tail(str(form.get("test_phone", "") or ""))
    result = logistics_service.query_debug(mail_no, cp_code=cp_code, phone=phone)

    if len(_CAINIAO_TESTS) >= _CAINIAO_TEST_KEEP:
        _CAINIAO_TESTS.pop(next(iter(_CAINIAO_TESTS)), None)
    key = uuid4().hex[:10]
    _CAINIAO_TESTS[key] = result
    if result.get("ok"):
        detail = f"轨迹 {len(result.get('nodes') or [])} 条"
        if not result.get("nodes"):
            detail = result.get("msg") or "菜鸟没返回轨迹"
        return _redirect(f"/settings?cainiao_test={key}", f"测试查询成功：{detail}")
    return _redirect(
        f"/settings?cainiao_test={key}", f"测试查询失败：{result.get('msg') or '原因见下方原文'}", "warn"
    )


@router.post("/settings/notify")
async def settings_notify(request: Request):
    """保存回执推送策略到 .env 并立即生效。"""
    if not _admin_user(request):
        return _admin_or_redirect(request)
    form = await request.form()

    def _int(key: str, default: int) -> int:
        try:
            return int(str(form.get(key, default)).strip())
        except (TypeError, ValueError):
            return default

    mode = str(form.get("notify_mode", "reply")).strip().lower()
    if mode not in ("reply", "direct", "off"):
        mode = "reply"
    action = str(form.get("notify_quiet_action", "hold")).strip().lower()
    if action not in ("hold", "drop"):
        action = "hold"
    active_hours = str(form.get("notify_active_hours", "")).strip()
    interval_min = max(_int("notify_min_interval_minutes", 10), 0)
    max_per_hour = max(_int("notify_max_per_hour", 0), 0)
    offset = min(max(_int("notify_utc_offset", 8), -12), 14)
    digest = str(form.get("notify_digest", "1")).strip() in ("1", "on", "true", "yes")
    wait_shot = min(max(_int("notify_wait_screenshot_seconds", 60), 0), 3600)
    # 群里照旧通知之外，再私聊同步一条给发送人本人
    pm_sync = str(form.get("notify_pm_sync", "0")).strip() in ("1", "on", "true", "yes")

    updates = {
        "NOTIFY_MODE": mode,
        "NOTIFY_ACTIVE_HOURS": active_hours,
        "NOTIFY_QUIET_ACTION": action,
        "NOTIFY_MIN_INTERVAL_SECONDS": str(interval_min * 60),
        "NOTIFY_MAX_PER_HOUR": str(max_per_hour),
        "NOTIFY_DIGEST": "1" if digest else "0",
        "NOTIFY_UTC_OFFSET": str(offset),
        "NOTIFY_WAIT_SCREENSHOT_SECONDS": str(wait_shot),
        "NOTIFY_PM_SYNC": "1" if pm_sync else "0",
    }
    # 立即生效（无需重启）
    settings.notify_mode = mode
    settings.notify_active_hours = active_hours
    settings.notify_quiet_action = action
    settings.notify_min_interval_seconds = interval_min * 60
    settings.notify_max_per_hour = max_per_hour
    settings.notify_digest = digest
    settings.notify_utc_offset = offset
    settings.notify_wait_screenshot_seconds = wait_shot
    settings.notify_pm_sync = pm_sync

    if not is_writable():
        return _redirect("/settings", "已在运行中配置生效（.env 不可写，重启后会丢失）", "warn")
    try:
        write_updates(updates)
    except Exception as exc:  # noqa: BLE001
        return _redirect("/settings", f"写入 .env 失败（本次已生效）：{exc}", "danger")
    return _redirect("/settings", "回执推送策略已保存并立即生效")


@router.post("/settings/daily-summary")
async def settings_daily_summary(request: Request):
    """保存每日出单汇总通知配置到 .env 并立即生效。"""
    if not _admin_user(request):
        return _admin_or_redirect(request)
    form = await request.form()

    def _int(key: str, default: int) -> int:
        try:
            return int(str(form.get(key, default)).strip())
        except (TypeError, ValueError):
            return default

    enabled = str(form.get("daily_summary_enabled", "0")).strip() in ("1", "on", "true", "yes")
    hour = min(max(_int("daily_summary_hour", 24), 0), 24)
    chat_ids = [x.strip() for x in str(form.get("daily_summary_chat_ids", "")).split(",") if x.strip()]
    at_users = str(form.get("daily_summary_at_users", "0")).strip() in ("1", "on", "true", "yes")
    skip_empty = str(form.get("daily_summary_skip_empty", "0")).strip() in ("1", "on", "true", "yes")

    updates = {
        "DAILY_SUMMARY_ENABLED": "1" if enabled else "0",
        "DAILY_SUMMARY_HOUR": str(hour),
        "DAILY_SUMMARY_CHAT_IDS": ",".join(chat_ids),
        "DAILY_SUMMARY_AT_USERS": "1" if at_users else "0",
        "DAILY_SUMMARY_SKIP_EMPTY": "1" if skip_empty else "0",
    }
    # 立即生效（无需重启）
    settings.daily_summary_enabled = enabled
    settings.daily_summary_hour = hour
    settings.daily_summary_chat_ids = chat_ids
    settings.daily_summary_at_users = at_users
    settings.daily_summary_skip_empty = skip_empty

    if not is_writable():
        return _redirect("/settings", "已在运行中配置生效（.env 不可写，重启后会丢失）", "warn")
    try:
        write_updates(updates)
    except Exception as exc:  # noqa: BLE001
        return _redirect("/settings", f"写入 .env 失败（本次已生效）：{exc}", "danger")
    return _redirect("/settings", "每日出单汇总配置已保存并立即生效")


@router.post("/settings/daily-summary/send")
async def settings_daily_summary_send(request: Request, db: Session = Depends(get_db)):
    """手动推送一次出单汇总（默认推今天，也可指定日期补推）。"""
    if not _admin_user(request):
        return _admin_or_redirect(request)
    form = await request.form()
    raw = str(form.get("day", "")).strip()
    day = None
    if raw:
        try:
            day = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return _redirect("/settings", "日期格式不正确，应为 YYYY-MM-DD", "warn")
    else:
        day = beijing_now().date()
    out = daily_summary_service.send_summary(db, day=day, skip_empty=False)
    if out.get("sent"):
        return _redirect(
            "/settings",
            f"已推送 {out['date']} 出单汇总到 {out['sent']} 个群（{out['people']} 人）",
        )
    if out.get("errors"):
        return _redirect("/settings", f"推送失败：{out['errors'][0]}", "danger")
    return _redirect("/settings", f"{out['date']} 没有需要推送的汇总数据", "warn")


@router.post("/settings/notify/flush")
def settings_notify_flush(request: Request, db: Session = Depends(get_db)):
    """手动把挂起的回执合并补发出去。"""
    if not _admin_user(request):
        return _admin_or_redirect(request)
    out = flush_held_notifications(db)
    if out.get("sent"):
        return _redirect("/settings", f"已补发 {out['sent']} 条挂起回执")
    if out.get("errors"):
        return _redirect("/settings", f"补发失败：{out['errors'][0]}", "danger")
    return _redirect("/settings", "当前没有待补发的回执", "warn")


@router.post("/settings/chats")
async def settings_add_chat(request: Request, db: Session = Depends(get_db)):
    if not _admin_user(request):
        return _admin_or_redirect(request)
    form = await request.form()
    chat_id = str(form.get("chat_id", "")).strip()
    if not chat_id:
        return _redirect("/settings", "请填写群 ID", "warn")
    exists = db.scalar(select(SyncState).where(SyncState.chat_id == chat_id))
    if exists is None:
        db.add(SyncState(chat_id=chat_id, chat_name=str(form.get("chat_name", ""))))
        db.commit()
    return _redirect("/settings", f"已添加群 {chat_id}")


@router.post("/settings/chats/{chat_id}/delete")
def settings_delete_chat(request: Request, chat_id: str, db: Session = Depends(get_db)):
    if not _admin_user(request):
        return _admin_or_redirect(request)
    row = db.scalar(select(SyncState).where(SyncState.chat_id == chat_id))
    if row:
        db.delete(row)
        db.commit()
    return _redirect("/settings", "已移除群", "warn")


@router.post("/settings/fill-sender-names")
def settings_fill_sender_names(request: Request, db: Session = Depends(get_db)):
    """一键补全历史记录里缺失的飞书发送人真实姓名。"""
    if not _admin_user(request):
        return _admin_or_redirect(request)
    if not settings.feishu_enabled:
        return _redirect("/settings", "未配置飞书凭证，无法读取员工姓名", "warn")
    try:
        filled = ingest_service.backfill_sender_names(db, limit=500)
    except Exception as exc:  # noqa: BLE001
        return _redirect("/settings", f"补全发送人姓名失败：{exc}", "danger")
    if not filled:
        return _redirect(
            "/settings",
            "没有补到姓名：请确认应用已开通通讯录权限（contact:user.base:readonly）或 im:chat 权限",
            "warn",
        )
    return _redirect("/settings", f"已补全 {filled} 条发送人姓名")


@router.post("/settings/voice")
async def settings_voice(request: Request):
    """保存语音播报配置到 .env 并立即生效。"""
    if not _admin_user(request):
        return _admin_or_redirect(request)
    form = await request.form()

    def _num(key: str, default: float, low: float, high: float) -> float:
        try:
            value = float(str(form.get(key, default)).strip())
        except (TypeError, ValueError):
            return default
        return min(max(value, low), high)

    enabled = str(form.get("voice_enabled", "0")).strip().lower() in ("1", "on", "true", "yes")
    volume = _num("voice_volume", 1.0, 0.0, 1.0)
    rate = _num("voice_rate", 1.0, 0.1, 10.0)
    pitch = _num("voice_pitch", 1.0, 0.0, 2.0)
    lang = str(form.get("voice_lang", "")).strip() or "zh-CN"
    name = str(form.get("voice_name", "")).strip()
    repeat = int(_num("voice_repeat", 1, 1, 5))
    default_clip = str(form.get("voice_default_clip", "1")).strip().lower() in ("1", "on", "true", "yes")
    order_text = str(form.get("voice_order_text", "")).strip() or voice_service.DEFAULT_TEXTS[
        voice_service.KIND_ORDER
    ]
    tracking_text = str(form.get("voice_tracking_text", "")).strip() or voice_service.DEFAULT_TEXTS[
        voice_service.KIND_TRACKING
    ]
    refund_text = str(form.get("voice_refund_text", "")).strip() or voice_service.DEFAULT_TEXTS[
        voice_service.KIND_REFUND
    ]

    settings.voice_enabled = enabled
    settings.voice_volume = volume
    settings.voice_rate = rate
    settings.voice_pitch = pitch
    settings.voice_lang = lang
    settings.voice_name = name
    settings.voice_repeat = repeat
    settings.voice_default_clip = default_clip
    settings.voice_order_text = order_text
    settings.voice_tracking_text = tracking_text
    settings.voice_refund_text = refund_text

    updates = {
        "VOICE_ENABLED": "1" if enabled else "0",
        "VOICE_VOLUME": f"{volume:g}",
        "VOICE_RATE": f"{rate:g}",
        "VOICE_PITCH": f"{pitch:g}",
        "VOICE_LANG": lang,
        "VOICE_NAME": name,
        "VOICE_REPEAT": str(repeat),
        "VOICE_DEFAULT_CLIP": "1" if default_clip else "0",
        "VOICE_ORDER_TEXT": order_text,
        "VOICE_TRACKING_TEXT": tracking_text,
        "VOICE_REFUND_TEXT": refund_text,
    }
    if not is_writable():
        return _redirect("/settings", "语音播报已在运行中配置生效（.env 不可写，重启后会丢失）", "warn")
    try:
        write_updates(updates)
    except Exception as exc:  # noqa: BLE001
        return _redirect("/settings", f"写入 .env 失败（本次已生效）：{exc}", "danger")
    return _redirect("/settings", "语音播报配置已保存并立即生效")


@router.post("/settings/templates")
async def settings_templates(request: Request):
    """保存通知消息模板（收到订单的回执 / 退单通知）与 @ 规则，保存后立即生效。"""
    if not _admin_user(request):
        return _admin_or_redirect(request)
    form = await request.form()

    def _text(key: str) -> str:
        # 多行文案：换行统一成 \n
        return str(form.get(key, "")).replace("\r\n", "\n").strip()

    def _people(key: str) -> list[str]:
        # @ 名单：每行一个 / 逗号分隔，每项形如「牛磊」或「牛磊|要给他说的一句话」
        out: list[str] = []
        for line in _text(key).split("\n"):
            for part in line.split(","):
                part = part.strip()
                if part:
                    out.append(part)
        return out

    receipt_tpl = _text("notify_receipt_template")
    refund_detail_tpl = _text("notify_refund_detail_template")
    ack_text = _text("refund_ack_text") or settings.refund_ack_text
    cc_text = _text("refund_cc_text") or settings.refund_cc_text
    # @ 谁：勾选了才 @（都不勾 = none，文案照发，只是不提醒本人）
    receipt_at = (
        ",".join(
            key
            for key, field in (
                ("sender", "receipt_at_sender"),
                ("names", "receipt_at_names_on"),
            )
            if str(form.get(field, "")).strip()
        )
        or "none"
    )
    # 回执额外要 @ 的人：每行一个（或逗号分隔），可写「姓名|要给他说的一句话」
    at_names = _people("notify_receipt_at_names")
    refund_at = (
        ",".join(
            key
            for key, field in (("sender", "refund_at_sender"), ("cc", "refund_at_cc"))
            if str(form.get(field, "")).strip()
        )
        or "none"
    )
    names = _people("refund_cc_names")  # 「牛磊」或「牛磊|要给他说的一句话」

    # 其余通知模板（设置页按分类编辑，留空都用系统内置格式）
    others = {
        "NOTIFY_DUPLICATE_TEMPLATE": ("notify_duplicate_template", _text("notify_duplicate_template")),
        "NOTIFY_CANCEL_TEMPLATE": ("notify_cancel_template", _text("notify_cancel_template")),
        "NOTIFY_INTENT_TEMPLATE": ("notify_intent_template", _text("notify_intent_template")),
        "NOTIFY_MISSING_IMAGE_TEMPLATE": (
            "notify_missing_image_template",
            _text("notify_missing_image_template"),
        ),
        "NOTIFY_DIGEST_TEMPLATE": ("notify_digest_template", _text("notify_digest_template")),
        "PUSH_TEMPLATE": ("push_template", _text("push_template")),
        "TRACKING_TEMPLATE": ("tracking_template", _text("tracking_template")),
        "DAILY_SUMMARY_TEMPLATE": ("daily_summary_template", _text("daily_summary_template")),
    }

    settings.notify_receipt_template = receipt_tpl
    settings.notify_refund_detail_template = refund_detail_tpl
    settings.refund_ack_text = ack_text
    settings.refund_cc_text = cc_text
    settings.notify_receipt_at = receipt_at
    settings.notify_refund_at = refund_at
    settings.notify_receipt_at_names = at_names
    if names:
        settings.refund_cc_names = names
    for _key, (_field, _value) in others.items():
        setattr(settings, _field, _value)

    updates = {
        # .env 一行一个配置，换行写成 \n 存（读的时候再还原）
        "NOTIFY_RECEIPT_TEMPLATE": receipt_tpl.replace("\n", "\\n"),
        "NOTIFY_REFUND_DETAIL_TEMPLATE": refund_detail_tpl.replace("\n", "\\n"),
        "REFUND_ACK_TEXT": ack_text,
        "REFUND_CC_TEXT": cc_text,
        "NOTIFY_RECEIPT_AT": receipt_at,
        "NOTIFY_RECEIPT_AT_NAMES": ",".join(at_names),
        "NOTIFY_REFUND_AT": refund_at,
    }
    updates.update(
        {key: value.replace("\n", "\\n") for key, (_, value) in others.items()}
    )
    if names:
        updates["REFUND_CC_NAMES"] = ",".join(names)
    if not is_writable():
        return _redirect(
            "/settings", "通知模板已在运行中配置生效（.env 不可写，重启后会丢失）", "warn"
        )
    try:
        write_updates(updates)
    except Exception as exc:  # noqa: BLE001
        return _redirect("/settings", f"写入 .env 失败（本次已生效）：{exc}", "danger")
    return _redirect("/settings", "通知消息模板已保存并立即生效")


@router.post("/settings/voice/clip")
async def upload_voice_clip(request: Request):
    """上传自定义录音（自己录的提示音）：配了就播录音，不再用浏览器合成音。

    音频存进 media/voice（docker 部署时挂在数据卷里，重建容器不丢），文件名写进 .env。
    """
    if not _admin_user(request):
        return _admin_or_redirect(request)
    form = await request.form()
    kind = str(form.get("kind", "")).strip()
    label = voice_service.CLIP_LABELS.get(kind)
    if not label:
        return _redirect("/settings", "未知的播报场景", "danger")

    upload = form.get("clip")
    name = getattr(upload, "filename", "") or ""
    if not name:
        return _redirect("/settings", f"请先选择{label}的录音文件", "danger")
    data = await upload.read() if hasattr(upload, "read") else b""

    filename, err = voice_service.save_clip(kind, name, data)
    if err:
        return _redirect("/settings", f"{label}录音：{err}", "danger")

    env_key = voice_service.env_key(kind)
    if is_writable():
        try:
            write_updates({env_key: filename})
        except Exception as exc:  # noqa: BLE001
            return _redirect(
                "/settings", f"{label}录音已生效，但写入 .env 失败（重启后会丢）：{exc}", "warn"
            )
    else:
        return _redirect("/settings", f"{label}录音已生效（.env 不可写，重启后会丢失）", "warn")
    return _redirect("/settings", f"{label}录音已上传，播报时会播放你录的声音")


@router.post("/settings/voice/clip/delete")
async def delete_voice_clip(request: Request):
    """删除自定义录音：播报回落到浏览器合成音。"""
    if not _admin_user(request):
        return _admin_or_redirect(request)
    form = await request.form()
    kind = str(form.get("kind", "")).strip()
    label = voice_service.CLIP_LABELS.get(kind)
    if not label:
        return _redirect("/settings", "未知的播报场景", "danger")

    msg = voice_service.delete_clip(kind)
    if is_writable():
        try:
            write_updates({voice_service.env_key(kind): ""})
        except Exception as exc:  # noqa: BLE001
            return _redirect("/settings", f"{msg}，但写入 .env 失败：{exc}", "warn")
    return _redirect("/settings", f"{label}：{msg}")


# ------------------------------------------------------------------ 数据备份（管理员）
# ------------------------------------------------------------------ 更新日志
@router.get("/changelog", response_class=HTMLResponse)
def changelog_page(request: Request, portal: str = ""):
    """更新日志：三个子系统各记各的（渔枢系统 / 渔货栈 / 渔集），按子系统看（?portal=…）。

    渔集的记录也写在 ``config/changelog.json`` 里（条目标了 ``system: "wms"``），
    ``load_all()`` 按这个字段把它分到「渔集」那一端 —— 以前会被标成渔枢系统。
    """
    entries = changelog_service.load_all(portal)
    counts: dict[str, int] = {}
    for row in changelog_service.load_all(""):
        counts[row["portal"]] = counts.get(row["portal"], 0) + 1
    return render(
        "changelog.html",
        request,
        heading="更新日志",
        sub=f"共 {len(entries)} 次更新 · 记录每次上线的核心能力",
        entries=entries,
        portal_filter=portal,
        counts=counts,
        total_count=sum(counts.values()),
        # 端名只在 app/portals.py 维护一处，筛选项动态取，改名不用动模板
        portal_tabs=PORTALS,
        current_version=changelog_service.current_version(),
        changelog_path=str(changelog_service.path()),
    )


@router.get("/backups", response_class=HTMLResponse)
def backups_page(request: Request):
    blocked = _admin_or_redirect(request)
    if blocked:
        return blocked
    rows = backup_service.list_backups()
    total = sum(r["size"] for r in rows)
    latest = rows[0] if rows else None
    return render(
        "backups.html",
        request,
        heading="数据备份",
        sub=f"共 {len(rows)} 个备份文件 · 保留最近 {settings.backup_keep_days} 天",
        files=rows,
        total_size=backup_service._human_size(total),
        total_count=len(rows),
        latest=latest,
        next_run=backup_service.next_backup_time().strftime("%Y-%m-%d %H:%M"),
        keep_days=settings.backup_keep_days,
        backup_hour=settings.backup_hour,
        backup_enabled=settings.backup_enabled,
        backup_dir=str(settings.backup_path),
        # 更新日志放在数据备份页（首页不再展示）
        updates=changelog_service.latest(3),
        current_version=changelog_service.current_version(),
    )


@router.post("/backups/create")
def backup_create(request: Request, db: Session = Depends(get_db)):
    if not _admin_user(request):
        return _admin_or_redirect(request)
    try:
        meta = backup_service.create_backup(session=db, label="manual")
    except Exception as exc:  # noqa: BLE001
        return _redirect("/backups", f"备份失败：{exc}", "danger")
    return _redirect("/backups", f"已备份 {meta['order_count']} 单：{meta['file']}")


@router.post("/backups/prune")
def backup_prune(request: Request):
    if not _admin_user(request):
        return _admin_or_redirect(request)
    out = backup_service.prune_backups()
    if not out["count"]:
        return _redirect("/backups", f"没有超过 {out['keep_days']} 天的备份需要清理", "warn")
    return _redirect("/backups", f"已清理 {out['count']} 个过期备份")


@router.get("/backups/download/{name}")
def backup_download(request: Request, name: str):
    if not _admin_user(request):
        return _admin_or_redirect(request)
    target = backup_service.backup_file(name)
    if target is None:
        raise HTTPException(404, "备份文件不存在")
    from fastapi.responses import FileResponse

    return FileResponse(str(target), filename=target.name, media_type="application/json")


@router.post("/backups/delete/{name}")
def backup_delete(request: Request, name: str):
    if not _admin_user(request):
        return _admin_or_redirect(request)
    if not backup_service.delete_backup(name):
        return _redirect("/backups", "备份文件不存在或文件名非法", "warn")
    return _redirect("/backups", f"已删除备份 {name}", "warn")


# ================================================================ 引流名片
# 一份带 OG 标签的 H5 长链接：贴到抖音群里，抖音爬虫抓 <head> 里的 OG 标签
# 自动渲染成卡片（封面图 + 标题 + 简介）。**爬虫不执行 JS**，所以 OG 全部
# 由服务端写进 HTML 源码（见 services/share_card.render_html）。
# 对外长链接 /c/<slug> 与封面图目录 /media/cards/ 都在 auth.py 里免登录放行 ——
# 爬虫没有 Cookie，卡在登录页就是一张空白卡片。
@router.get("/share-cards", response_class=HTMLResponse)
def share_cards_page(request: Request, q: str = "", db: Session = Depends(get_db)):
    """名片列表：每张一行，长链接一键复制。"""
    rows = share_card_service.list_cards(db, q=q)
    return render(
        "share_cards.html",
        request,
        heading="引流名片",
        sub="填一次生成一张带封面图的 H5 卡片，复制长链接贴到抖音群，对方看到的就是卡片而不是一串网址",
        rows=rows,
        keyword=q,
        # 对外域名：OG 里的地址都用域名拼绝对地址，是 http 就红字提醒（抖音基本不抓 http）
        site_base=share_card_service.site_base(),
        site_https=share_card_service.is_https(),
        card_url=share_card_service.card_url,
        fresh_url=share_card_service.fresh_url,
        hits_of=share_card_service.risk_hits,
        # 封面图体检（竖图 / 小图抖音常常不渲染卡片）—— 列表页直接打标
        cover_info=share_card_service.cover_info,
    )


@router.get("/share-cards/new", response_class=HTMLResponse)
def share_card_new(request: Request):
    """新建名片表单。"""
    return render(
        "share_card_form.html",
        request,
        heading="新建引流名片",
        sub="OG 三件套（标题 / 简介 / 封面图）决定卡片长什么样，下面几项是点开之后看到的",
        card=None,
        site_base=share_card_service.site_base(),
        site_https=share_card_service.is_https(),
        max_mb=int(getattr(settings, "upload_image_max_mb", 5) or 0),
        cover_advice="",
    )


@router.get("/share-cards/{card_id}/edit", response_class=HTMLResponse)
def share_card_edit(card_id: int, request: Request, db: Session = Depends(get_db)):
    card = share_card_service.get_card(db, card_id)
    if card is None:
        return _redirect("/share-cards", "这张名片不存在", "warn")
    return render(
        "share_card_form.html",
        request,
        heading="编辑引流名片",
        sub="改完保存即可；抖音那边有缓存，卡片不会立刻变，用「重新抓取」链接换一个地址",
        card=card,
        site_base=share_card_service.site_base(),
        site_https=share_card_service.is_https(),
        max_mb=int(getattr(settings, "upload_image_max_mb", 5) or 0),
        # 已存的封面图当场体检：竖图 / 小图直接提示（抖音卡片要横图 ≥1200×630）
        cover_advice=share_card_service.cover_advice(card.og_image),
    )


@router.post("/share-cards/create")
async def share_card_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    card, err = share_card_service.create_card(db, dict(form))
    if err:
        return _redirect("/share-cards/new", err, "warn")
    url = share_card_service.card_url(card)
    msg = f"已生成名片「{card.name}」：{url}"
    hits = share_card_service.risk_hits(card.og_title, card.og_desc, card.body_text)
    if hits:
        msg += f"（提醒：文案里有「{'、'.join(hits)}」这类引流话术，抖音可能吞卡片或限流，慎用）"
    return _redirect("/share-cards", msg)


@router.post("/share-cards/{card_id}/edit")
async def share_card_save(card_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    card = share_card_service.get_card(db, card_id)
    if card is None:
        return _redirect("/share-cards", "这张名片不存在", "warn")
    card, err = share_card_service.update_card(db, card, dict(form))
    if err:
        return _redirect(f"/share-cards/{card_id}/edit", err, "warn")
    return _redirect("/share-cards", f"已保存名片「{card.name}」（改了内容记得用「重新抓取」链接）")


@router.post("/share-cards/{card_id}/toggle")
async def share_card_toggle(card_id: int, db: Session = Depends(get_db)):
    card = share_card_service.get_card(db, card_id)
    if card is None:
        return _redirect("/share-cards", "这张名片不存在", "warn")
    on = share_card_service.toggle_card(db, card)
    return _redirect(
        "/share-cards",
        f"名片「{card.name}」已{'启用，链接恢复访问' if on else '停用，旧链接暂时打不开（404）'}",
    )


@router.post("/share-cards/{card_id}/delete")
async def share_card_delete(card_id: int, db: Session = Depends(get_db)):
    card = share_card_service.get_card(db, card_id)
    if card is None:
        return _redirect("/share-cards", "这张名片不存在", "warn")
    name = card.name
    share_card_service.delete_card(db, card)
    return _redirect("/share-cards", f"已删除名片「{name}」", "warn")


@router.post("/share-cards/upload-image")
async def share_card_upload(request: Request):
    """封面图上传：存到 media/cards/（该目录免登录，爬虫抓得到）。"""
    try:
        form = await request.form()
        upload = form.get("file")
        name = getattr(upload, "filename", "") or ""
        data = await upload.read() if hasattr(upload, "read") else b""
    except Exception as exc:  # noqa: BLE001 解析失败要给前端一句人话
        log_id = error_log_service.record_request(request, exc, status_code=400)
        return JSONResponse(
            {"ok": False, "error": f"图片没传完整（{type(exc).__name__}），重新选一次再传",
             "log_id": log_id},
            status_code=400,
        )
    url, err = share_card_service.save_cover(data, name)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    size = share_card_service.cover_size(url) or (0, 0)
    return JSONResponse({
        "ok": True,
        "url": url,
        "abs": share_card_service.abs_url(url),
        "width": size[0],
        "height": size[1],
        # 尺寸不合规时给一句人话（抖音要横图 ≥1200×630），前端黄字提示
        "advice": share_card_service.cover_advice(url),
    })


@router.post("/share-cards/preview-html")
async def share_card_preview_html(request: Request):
    """实时预览：按表单当前内容渲染一份 HTML（**不入库**），塞进编辑页右侧的预览框。

    走的是与公开长链接**同一个** render_html，所以预览看到的就是真发出去的那份；
    预览态不带跳转脚本（iframe 里真跳走就没法盯着看了）。
    """
    form = await request.form()
    return JSONResponse({"ok": True, "html": share_card_service.preview_from_form(dict(form))})


@router.get("/share-cards/{card_id}/preview", response_class=HTMLResponse)
def share_card_preview(card_id: int, request: Request, db: Session = Depends(get_db)):
    """预览：抖音爬虫抓到的就是这一份（OG 在源码里，右键查看源代码能亲眼确认）。"""
    card = share_card_service.get_card(db, card_id)
    if card is None:
        return _redirect("/share-cards", "这张名片不存在", "warn")
    return HTMLResponse(share_card_service.render_html(card))


@router.api_route("/c/{slug}", methods=["GET", "HEAD"], response_class=HTMLResponse)
def share_card_public(slug: str, request: Request, db: Session = Depends(get_db)):
    """对外长链接：https://域名/c/<slug> —— 免登录（auth.py 放行 /c/）。

    返回的是**服务端拼好的静态 HTML**：OG 标签写在源码里，爬虫不执行 JS 也抓得到。

    ``methods`` 里显式带 HEAD：平台爬虫常先用 HEAD 探一下 Content-Type / 状态码，
    只声明 GET 时这里会回 405（线上实测过），有的爬虫就此放弃、不再抓正文。
    """
    card = share_card_service.by_slug(db, slug)
    if card is None or not card.enabled:
        return HTMLResponse(
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>链接已失效</title></head><body style=\"font-family:-apple-system,"
            "'PingFang SC',sans-serif;padding:16vh 24px;text-align:center;color:#52606d\">"
            "<div style=\"font-size:40px\">🔗</div>"
            "<h1 style=\"font-size:18px;color:#1f2933\">这个链接已经失效</h1>"
            "<p style=\"font-size:14px\">名片被停用或删除了，找分享的人要一条新的。</p>"
            "</body></html>",
            status_code=404,
        )
    share_card_service.count_hit(db, card)
    return HTMLResponse(share_card_service.render_html(card))
