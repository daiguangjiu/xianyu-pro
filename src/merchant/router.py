"""商家入驻 + 商家端后台 + 审核台商家管理（Web 页面路由）。

页面地图：
    商家侧
        /merchant/register              注册账号
        /merchant/login                 登录
        /merchant/logout                登出
        /merchant/apply                 提交入驻申请（查看审核进度 / 驳回后重提）
        /merchant                       商家后台概览
        /merchant/items                 本店商品列表
        /merchant/items/new             发布商品
        /merchant/items/{item_id}/edit  编辑商品

    平台侧
        /platform/applies               入驻申请审核列表
        /platform/applies/{id}          申请详情
        /platform/applies/{id}/review   审核（通过 / 驳回）
        /platform/merchants             商家列表
        /platform/items/{item_id}/audit 商品审核（通过并上架 / 驳回）

与现有 web/router.py 的关系：
    本模块是独立的第三类角色（商家），不复用 portal cookie 的"端"概念，
    而是通过 `nav_role` 让 base.html 渲染商家侧边栏，避免改动现有双端逻辑。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func as sql_func, select
from sqlalchemy.orm import Session

from src.config import get_settings
from src.deps import get_db
from src.models import (
    APPLY_STATUS,
    MERCHANT_STATUS,
    SUBJECT_TYPES,
    DownstreamOrder,
    Merchant,
    MerchantApply,
    MerchantWallet,
    PurchaseOrder,
    SettlementBill,
    SupplierItem,
    User,
    WithdrawApply,
)
from src.merchant import service as svc
from src.merchant.security import (
    AUTH_COOKIE,
    TOKEN_TTL_SECONDS,
    create_token,
    decode_token,
)
from src.version import APP_NAME, APP_TAGLINE
from src.web import hub_shops
from app.timeutil import to_iso, utc_now
from app.utils import media_url, thumb_url

try:  # 与渔枢系统同一个进程时才打通登录；独立部署（没有 app 包）时保持原样
    from app.hub_auth import hub_login_name as _hub_login_name
except Exception:  # noqa: BLE001
    _hub_login_name = None

router = APIRouter(tags=["merchant-web"], include_in_schema=False)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
templates = Jinja2Templates(directory=str(_REPO_ROOT / "templates"))
# 2026-10-15：渔货栈的列表 / 购物车里商品图只占几十像素，原来直接拉 2MB 原图 ——
# 注册 thumb_url 让这些位置改用按需生成的缩略图（裁一次落盘缓存，之后几十 KB）
templates.env.globals.update({"thumb_url": thumb_url, "media_url": media_url})


# =====================================================================
# 登录态
# =====================================================================
def _current_user(request: Request, db: Session) -> User | None:
    """从 Cookie 解出当前登录账号；未登录 / 凭证失效返回 None。

    与渔枢系统打通后优先认渔枢系统的会话（oh_session）：统一账号的登录名就是商家手机号，
    按手机号在渔枢系统库里找到自己的账号，角色与商家归属沿用渔枢系统库里的记录；
    没有对应账号时再回落到老的 scx_auth JWT（商家自己注册登录 / 独立部署的场景）。
    """
    if _hub_login_name is not None:
        phone = _hub_login_name(request)
        if phone:
            user = db.query(User).filter(User.phone == phone).first()
            if user is not None and user.status == "active":
                return user
    payload = decode_token(request.cookies.get(AUTH_COOKIE) or "")
    if not payload:
        return None
    try:
        uid = int(payload.get("sub") or 0)
    except (TypeError, ValueError):
        return None
    user = db.get(User, uid)
    if user is None or user.status != "active":
        return None
    return user


def _login_user(resp: RedirectResponse, user: User) -> RedirectResponse:
    """登录成功：下发 JWT Cookie。"""
    resp.set_cookie(
        AUTH_COOKIE,
        create_token(user.id, user.phone, user.role),
        max_age=TOKEN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


def _ctx(
    request: Request,
    active_nav: str,
    *,
    user: User | None = None,
    nav_role: str = "merchant",
    **extra: Any,
) -> dict[str, Any]:
    """模板上下文：补齐 base.html 依赖的公共变量。"""
    settings = get_settings()
    base: dict[str, Any] = {
        "request": request,
        "active_nav": active_nav,
        "active_sub": None,
        "nav_crumb": None,
        "nav_counts": None,
        "now": utc_now(),
        "env": settings.app_env,
        "db_url_masked": "",
        "sso_user": (user.display_name or user.phone) if user else "",
        # 商家端专属：控制 base.html 侧边栏分支
        "nav_role": nav_role,
        "current_user": user,
        # 顶栏右上角：用户名 / 身份 / 关联店铺数（店铺档案在渔枢系统，见 web/hub_shops.py）
        "viewer": hub_shops.viewer_summary(request, user),
    }
    base.update(extra)
    return base


def _redirect(url: str) -> RedirectResponse:
    """站内跳转：统一带上渔货栈前缀（页面挂在 /self 下，见 config.self_prefix）。"""
    prefix = get_settings().self_prefix
    if prefix and url.startswith("/") and not url.startswith(prefix):
        url = f"{prefix}{url}"
    return RedirectResponse(url, status_code=303)


# =====================================================================
# 注册 / 登录 / 登出
# =====================================================================
@router.get("/merchant/register", response_class=HTMLResponse)
def register_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "merchant/register.html", _ctx(request, "merchant", nav_role="guest")
    )


@router.post("/merchant/register")
async def register_submit(
    request: Request,
    db: Session = Depends(get_db),
    phone: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
    nickname: str = Form(""),
) -> Any:
    phone = phone.strip()
    if not phone or not password:
        return templates.TemplateResponse(
            "merchant/register.html",
            _ctx(request, "merchant", nav_role="guest", error="手机号和密码不能为空"),
            status_code=400,
        )
    if password != password2:
        return templates.TemplateResponse(
            "merchant/register.html",
            _ctx(request, "merchant", nav_role="guest", error="两次输入的密码不一致"),
            status_code=400,
        )
    if len(password) < 6:
        return templates.TemplateResponse(
            "merchant/register.html",
            _ctx(request, "merchant", nav_role="guest", error="密码至少 6 位"),
            status_code=400,
        )
    if svc.get_user_by_phone(db, phone):
        return templates.TemplateResponse(
            "merchant/register.html",
            _ctx(request, "merchant", nav_role="guest", error="该手机号已注册，请直接登录"),
            status_code=400,
        )

    user = svc.register_user(db, phone, password, nickname or f"商家{phone[-4:]}")
    db.commit()
    resp = _redirect("/merchant/apply")
    return _login_user(resp, user)


@router.get("/merchant/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = Query("/merchant")) -> HTMLResponse:
    return templates.TemplateResponse(
        "merchant/login.html",
        _ctx(request, "merchant", nav_role="guest", next_url=next),
    )


@router.post("/merchant/login")
async def login_submit(
    request: Request,
    db: Session = Depends(get_db),
    phone: str = Form(""),
    password: str = Form(""),
    next: str = Form("/merchant"),
) -> Any:
    user = svc.authenticate(db, phone.strip(), password)
    if user is None:
        return templates.TemplateResponse(
            "merchant/login.html",
            _ctx(
                request,
                "merchant",
                nav_role="guest",
                error="手机号或密码不正确",
                next_url=next,
            ),
            status_code=401,
        )
    db.commit()
    # 带 next 时优先回原页面（只读浏览时点"登录"会带上当前页）；
    # 否则平台管理员去审核台，商家去商家后台。
    safe_next = next if (next or "").startswith("/") else ""
    if safe_next:
        target = safe_next
    else:
        target = "/platform/applies" if user.role == "platform_admin" else "/merchant"
    resp = _redirect(target)
    return _login_user(resp, user)


@router.get("/merchant/logout")
def logout() -> RedirectResponse:
    resp = _redirect("/merchant/login")
    resp.delete_cookie(AUTH_COOKIE, path="/")
    return resp


# =====================================================================
# 入驻申请
# =====================================================================
@router.get("/merchant/apply", response_class=HTMLResponse)
def apply_page(request: Request, db: Session = Depends(get_db)) -> Any:
    user = _current_user(request, db)
    if user is None:
        return _redirect("/merchant/login?next=/merchant/apply")

    merchant = svc.get_merchant_of_user(db, user)
    apply = svc.latest_apply_of_user(db, user)
    return templates.TemplateResponse(
        "merchant/apply.html",
        _ctx(
            request,
            "merchant_apply",
            user=user,
            merchant=merchant,
            apply=apply,
            categories=svc.list_category_options(),
            subject_types=SUBJECT_TYPES,
        ),
    )


@router.post("/merchant/apply")
async def apply_submit(request: Request, db: Session = Depends(get_db)) -> Any:
    user = _current_user(request, db)
    if user is None:
        return _redirect("/merchant/login?next=/merchant/apply")

    form = dict(await request.form())
    # 多选类目：request.form() 只取最后一个，这里单独取全部
    form["categories"] = [
        c for c in (await request.form()).getlist("categories") if c
    ]

    apply = svc.build_apply_from_form(db, user, form)
    db.commit()
    return _redirect(f"/merchant/apply?submitted={apply.id}")


# =====================================================================
# 商家后台：概览
# =====================================================================
@router.get("/merchant", response_class=HTMLResponse)
def merchant_home(request: Request, db: Session = Depends(get_db)) -> Any:
    user = _current_user(request, db)
    if user is None:
        return _redirect("/merchant/login?next=/merchant")

    merchant = svc.get_merchant_of_user(db, user)
    if merchant is None:
        return _redirect("/merchant/apply")

    wallet = svc.ensure_wallet(db, merchant)
    db.commit()

    stats = svc.merchant_item_stats(db, merchant)
    pending_bills = db.scalar(
        select(sql_func.count(SettlementBill.id)).where(
            SettlementBill.merchant_id == merchant.id,
            SettlementBill.status == "pending",
        )
    ) or 0

    return templates.TemplateResponse(
        "merchant/dashboard.html",
        _ctx(
            request,
            "merchant_home",
            user=user,
            merchant=merchant,
            wallet=wallet,
            stats=stats,
            pending_bills=pending_bills,
        ),
    )


# =====================================================================
# 商家后台：商品管理
# =====================================================================
@router.get("/merchant/items", response_class=HTMLResponse)
def merchant_items(
    request: Request,
    db: Session = Depends(get_db),
    keyword: str = Query(""),
    audit_status: str = Query(""),
    listed: str = Query(""),
) -> Any:
    user = _current_user(request, db)
    if user is None:
        return _redirect("/merchant/login?next=/merchant/items")

    merchant = svc.get_merchant_of_user(db, user)
    if merchant is None:
        return _redirect("/merchant/apply")

    items = svc.list_merchant_items(
        db, merchant, keyword=keyword, audit_status=audit_status, listed=listed
    )
    for it in items:
        it.category_name = svc.category_label(it.category_id)

    return templates.TemplateResponse(
        "merchant/items.html",
        _ctx(
            request,
            "merchant_items",
            user=user,
            merchant=merchant,
            items=items,
            stats=svc.merchant_item_stats(db, merchant),
            keyword=keyword,
            audit_status=audit_status,
            listed=listed,
        ),
    )


@router.get("/merchant/items/new", response_class=HTMLResponse)
def merchant_item_new(request: Request, db: Session = Depends(get_db)) -> Any:
    user = _current_user(request, db)
    if user is None:
        return _redirect("/merchant/login?next=/merchant/items/new")

    merchant = svc.get_merchant_of_user(db, user)
    if merchant is None:
        return _redirect("/merchant/apply")

    return templates.TemplateResponse(
        "merchant/item_form.html",
        _ctx(
            request,
            "merchant_items",
            user=user,
            merchant=merchant,
            item=None,
            categories=svc.list_category_options(),
        ),
    )


@router.post("/merchant/items/new")
async def merchant_item_create(request: Request, db: Session = Depends(get_db)) -> Any:
    user = _current_user(request, db)
    if user is None:
        return _redirect("/merchant/login?next=/merchant/items/new")

    merchant = svc.get_merchant_of_user(db, user)
    if merchant is None:
        return _redirect("/merchant/apply")

    form = dict(await request.form())
    svc.create_merchant_item(db, merchant, form)
    db.commit()
    return _redirect("/merchant/items?created=1")


@router.get("/merchant/items/{item_id}/edit", response_class=HTMLResponse)
def merchant_item_edit(
    item_id: str, request: Request, db: Session = Depends(get_db)
) -> Any:
    user = _current_user(request, db)
    if user is None:
        return _redirect(f"/merchant/login?next=/merchant/items/{item_id}/edit")

    merchant = svc.get_merchant_of_user(db, user)
    if merchant is None:
        return _redirect("/merchant/apply")

    item = svc.get_merchant_item(db, merchant, item_id)
    if item is None:
        return _redirect("/merchant/items")

    return templates.TemplateResponse(
        "merchant/item_form.html",
        _ctx(
            request,
            "merchant_items",
            user=user,
            merchant=merchant,
            item=item,
            categories=svc.list_category_options(),
        ),
    )


@router.post("/merchant/items/{item_id}/edit")
async def merchant_item_update(
    item_id: str, request: Request, db: Session = Depends(get_db)
) -> Any:
    user = _current_user(request, db)
    if user is None:
        return _redirect("/merchant/login")

    merchant = svc.get_merchant_of_user(db, user)
    if merchant is None:
        return _redirect("/merchant/apply")

    form = dict(await request.form())
    svc.update_merchant_item(db, merchant, item_id, form)
    db.commit()
    return _redirect("/merchant/items?updated=1")


@router.post("/merchant/items/{item_id}/listed")
async def merchant_item_listed(
    item_id: str, request: Request, db: Session = Depends(get_db)
) -> Any:
    user = _current_user(request, db)
    if user is None:
        return _redirect("/merchant/login")

    merchant = svc.get_merchant_of_user(db, user)
    if merchant is None:
        return _redirect("/merchant/apply")

    form = dict(await request.form())
    want = str(form.get("listed", "0")) in ("1", "true", "on")
    svc.set_item_listed(db, merchant, item_id, want)
    db.commit()
    return _redirect("/merchant/items?listed_changed=1")


# =====================================================================
# 审核台：入驻申请审核
# =====================================================================
def _platform_access(request: Request, db: Session) -> tuple[User | None, bool]:
    """审核台访问权限：返回 (当前账号, 是否可写)。

    读：demo 环境下允许未登录浏览（方便演示），页面会提示"只读"。
    写：**一律要求 platform_admin**。此前所有审核台接口都没有守卫，
    未登录也能点"通过提现"直接改资金，属于越权；这里堵住。
    """
    user = _current_user(request, db)
    # 两端合并后角色按渔枢系统口径：管理员 = admin（原渔货栈的 platform_admin）
    can_write = user is not None and user.role == "admin"
    return user, can_write


def _require_admin(request: Request, db: Session) -> User | None:
    """写操作守卫：非管理员（role=admin）返回 None，由调用方转 403/重定向。"""
    user = _current_user(request, db)
    if user is not None and user.role == "admin":
        return user
    return None


def _platform_ctx(
    request: Request,
    db: Session,
    active_nav: str,
    **extra: Any,
) -> dict[str, Any]:
    """审核台页面上下文：额外注入 can_write，供模板禁用写操作按钮。"""
    user, can_write = _platform_access(request, db)
    return _ctx(
        request,
        active_nav,
        user=user,
        nav_role="platform",
        can_write=can_write,
        **extra,
    )


def _denied(msg: str = "需要平台管理员登录后才能操作") -> RedirectResponse:
    return _redirect(f"/platform/applies?msg={_qs(msg)}&ok=0")


@router.get("/platform/applies", response_class=HTMLResponse)
def platform_applies(
    request: Request,
    db: Session = Depends(get_db),
    status: str = Query(""),
) -> Any:
    stmt = select(MerchantApply).order_by(MerchantApply.id.desc())
    if status:
        stmt = stmt.where(MerchantApply.status == status)
    applies = list(db.scalars(stmt).all())

    # 带上申请人账号，便于审核时联系
    phones = {
        u.id: u.phone for u in db.scalars(select(User)).all()
    }

    return templates.TemplateResponse(
        "merchant/platform_applies.html",
        _platform_ctx(
            request,
            db,
            "platform_applies",
            applies=applies,
            phones=phones,
            status=status,
            apply_status=APPLY_STATUS,
            counts={
                "pending": db.scalar(
                    select(sql_func.count(MerchantApply.id)).where(
                        MerchantApply.status == "pending"
                    )
                ) or 0,
                "approved": db.scalar(
                    select(sql_func.count(MerchantApply.id)).where(
                        MerchantApply.status == "approved"
                    )
                ) or 0,
                "rejected": db.scalar(
                    select(sql_func.count(MerchantApply.id)).where(
                        MerchantApply.status == "rejected"
                    )
                ) or 0,
            },
        ),
    )


@router.get("/platform/applies/{apply_id}", response_class=HTMLResponse)
def platform_apply_detail(
    apply_id: int, request: Request, db: Session = Depends(get_db)
) -> Any:
    apply = svc.get_apply(db, apply_id)
    if apply is None:
        return _redirect("/platform/applies")

    applicant = db.get(User, apply.user_id)
    return templates.TemplateResponse(
        "merchant/platform_apply_detail.html",
        _platform_ctx(
            request,
            db,
            "platform_applies",
            apply=apply,
            applicant=applicant,
            subject_types=SUBJECT_TYPES,
        ),
    )


@router.post("/platform/applies/{apply_id}/review")
async def platform_apply_review(
    apply_id: int, request: Request, db: Session = Depends(get_db)
) -> Any:
    if _require_admin(request, db) is None:
        return _denied()

    apply = svc.get_apply(db, apply_id)
    if apply is None:
        return _redirect("/platform/applies")

    form = dict(await request.form())
    approve = str(form.get("action", "")) == "approve"
    note = (form.get("review_note") or "").strip()
    reviewer = (form.get("reviewed_by") or "平台运营").strip()

    if not approve and not note:
        return templates.TemplateResponse(
            "merchant/platform_apply_detail.html",
            _platform_ctx(
            request,
            db,
            "platform_applies",
                apply=apply,
                applicant=db.get(User, apply.user_id),
                subject_types=SUBJECT_TYPES,
                error="驳回时必须填写驳回原因",
            ),
            status_code=400,
        )

    svc.review_apply(db, apply, approve=approve, note=note, reviewer=reviewer)
    db.commit()
    return _redirect(f"/platform/applies?reviewed={apply.id}")


# =====================================================================
# 审核台：商家列表 / 商品审核
# =====================================================================
@router.get("/platform/merchants", response_class=HTMLResponse)
def platform_merchants(request: Request, db: Session = Depends(get_db)) -> Any:
    merchants = list(
        db.scalars(select(Merchant).order_by(Merchant.id.desc())).all()
    )
    return templates.TemplateResponse(
        "merchant/platform_merchants.html",
        _platform_ctx(
            request,
            db,
            "platform_merchants",
            merchants=merchants,
            merchant_status=MERCHANT_STATUS,
            item_counts={
                m.id: db.scalar(
                    select(sql_func.count(SupplierItem.id)).where(
                        SupplierItem.merchant_id == m.id
                    )
                ) or 0
                for m in merchants
            },
        ),
    )


@router.get("/platform/items", response_class=HTMLResponse)
def platform_items(
    request: Request,
    db: Session = Depends(get_db),
    audit_status: str = Query("pending"),
) -> Any:
    """商家发布的商品审核队列（只看 merchant_id 非空的商品）。"""
    stmt = (
        select(SupplierItem)
        .where(SupplierItem.merchant_id.is_not(None))
        .order_by(SupplierItem.id.desc())
    )
    if audit_status:
        stmt = stmt.where(SupplierItem.audit_status == audit_status)
    items = list(db.scalars(stmt).all())

    merchants = {m.id: m for m in db.scalars(select(Merchant)).all()}
    for it in items:
        it.category_name = svc.category_label(it.category_id)
        it.merchant_name = (
            merchants[it.merchant_id].name if it.merchant_id in merchants else "—"
        )

    return templates.TemplateResponse(
        "merchant/platform_items.html",
        _platform_ctx(
            request,
            db,
            "platform_items",
            items=items,
            audit_status=audit_status,
            counts={
                "pending": db.scalar(
                    select(sql_func.count(SupplierItem.id)).where(
                        SupplierItem.merchant_id.is_not(None),
                        SupplierItem.audit_status == "pending",
                    )
                ) or 0,
                "approved": db.scalar(
                    select(sql_func.count(SupplierItem.id)).where(
                        SupplierItem.merchant_id.is_not(None),
                        SupplierItem.audit_status == "approved",
                    )
                ) or 0,
                "rejected": db.scalar(
                    select(sql_func.count(SupplierItem.id)).where(
                        SupplierItem.merchant_id.is_not(None),
                        SupplierItem.audit_status == "rejected",
                    )
                ) or 0,
            },
        ),
    )


@router.post("/platform/items/{item_id}/audit")
async def platform_item_audit(
    item_id: str, request: Request, db: Session = Depends(get_db)
) -> Any:
    """商品审核：通过后自动上架，驳回则下架并记录原因。"""
    if _require_admin(request, db) is None:
        return _denied()

    item = db.scalar(select(SupplierItem).where(SupplierItem.item_id == item_id))
    if item is None:
        return _redirect("/platform/items")

    form = dict(await request.form())
    approve = str(form.get("action", "")) == "approve"
    note = (form.get("audit_note") or "").strip()

    item.audit_status = "approved" if approve else "rejected"
    item.audit_note = note or None
    item.audited_at = utc_now()
    # 审核通过与上架绑定：通过后立即可售，驳回强制下架
    item.is_listed = approve
    if approve:
        # 通过 = 刚上架：货源广场按 last_synced_at 排「最新上架」/ 扫货源，
        # 不刷这个时间商家商品会排在最后（NULL 更是直接垫底），审核过了也找不着
        item.last_synced_at = utc_now()
    db.commit()
    return _redirect("/platform/items?audited=1")


# =====================================================================
# 阶段 3：商家端订单管理 + 发货
# =====================================================================
def _guard_merchant(request: Request, db: Session, next_url: str):
    """商家端守卫：返回 (user, merchant)；未登录 / 未入驻时返回 (None, None)。"""
    user = _current_user(request, db)
    if user is None:
        return None, None
    merchant = svc.get_merchant_of_user(db, user)
    if merchant is None:
        return user, None
    return user, merchant


@router.get("/merchant/orders", response_class=HTMLResponse)
def merchant_orders(
    request: Request,
    db: Session = Depends(get_db),
    status: str = Query(""),
    keyword: str = Query(""),
    page: int = Query(1, ge=1),
) -> Any:
    """本店订单列表：支持状态筛选 + 订单号/买家搜索。"""
    user, merchant = _guard_merchant(request, db, "/merchant/orders")
    if user is None:
        return _redirect("/merchant/login?next=/merchant/orders")
    if merchant is None:
        return _redirect("/merchant/apply")

    orders, total = svc.list_merchant_orders(
        db, merchant, status=status or None, keyword=keyword or None, page=page
    )
    for o in orders:
        o.status_label = svc.order_status_text(o.status)
        o.amount = svc.order_goods_amount(o)
        o.logistics = svc.order_logistics(o)

    return templates.TemplateResponse(
        "merchant/orders.html",
        _ctx(
            request,
            "merchant_orders",
            user=user,
            merchant=merchant,
            orders=orders,
            total=total,
            stats=svc.merchant_order_stats(db, merchant),
            status=status,
            keyword=keyword,
            page=page,
            page_size=20,
            status_options=svc.ORDER_STATUS_TEXT,
        ),
    )


@router.get("/merchant/orders/{order_id}", response_class=HTMLResponse)
def merchant_order_detail(
    order_id: int, request: Request, db: Session = Depends(get_db)
) -> Any:
    """本店订单详情（含物流信息 + 操作区）。跨商家访问 → 回列表。"""
    user, merchant = _guard_merchant(request, db, f"/merchant/orders/{order_id}")
    if user is None:
        return _redirect(f"/merchant/login?next=/merchant/orders/{order_id}")
    if merchant is None:
        return _redirect("/merchant/apply")

    order = svc.get_merchant_order(db, merchant, order_id)
    if order is None:
        return _redirect("/merchant/orders")

    order.status_label = svc.order_status_text(order.status)
    order.amount = svc.order_goods_amount(order)
    order.logistics = svc.order_logistics(order)

    # 商品名：从 raw_json.ali_item_id 反查 SupplierItem
    item = None
    ali_item_id = (order.raw_json or {}).get("ali_item_id")
    if ali_item_id:
        item = db.scalar(
            select(SupplierItem).where(SupplierItem.item_id == ali_item_id)
        )

    pos = db.scalars(
        select(PurchaseOrder).where(
            PurchaseOrder.downstream_order_id == order.downstream_order_id
        )
    ).all()

    return templates.TemplateResponse(
        "merchant/order_detail.html",
        _ctx(
            request,
            "merchant_orders",
            user=user,
            merchant=merchant,
            order=order,
            item=item,
            pos=list(pos),
        ),
    )


@router.post("/merchant/orders/{order_id}/confirm")
def merchant_order_confirm(
    order_id: int, request: Request, db: Session = Depends(get_db)
) -> Any:
    """商家接单：pending → confirmed。"""
    user, merchant = _guard_merchant(request, db, f"/merchant/orders/{order_id}")
    if user is None or merchant is None:
        return _redirect("/merchant/orders")

    order = svc.get_merchant_order(db, merchant, order_id)
    if order is None:
        return _redirect("/merchant/orders")

    ok, msg = svc.confirm_order(db, order)
    db.commit()
    return _redirect(f"/merchant/orders/{order_id}?msg={_qs(msg)}&ok={1 if ok else 0}")


@router.post("/merchant/orders/{order_id}/ship")
async def merchant_order_ship(
    order_id: int, request: Request, db: Session = Depends(get_db)
) -> Any:
    """商家发货：填物流公司 + 运单号。"""
    user, merchant = _guard_merchant(request, db, f"/merchant/orders/{order_id}")
    if user is None or merchant is None:
        return _redirect("/merchant/orders")

    order = svc.get_merchant_order(db, merchant, order_id)
    if order is None:
        return _redirect("/merchant/orders")

    form = dict(await request.form())
    ok, msg = svc.ship_order(
        db,
        order,
        carrier=(form.get("carrier") or "").strip(),
        shipping_no=(form.get("shipping_no") or "").strip(),
    )
    return _redirect(f"/merchant/orders/{order_id}?msg={_qs(msg)}&ok={1 if ok else 0}")


# =====================================================================
# 阶段 4：商家端资金结算
# =====================================================================
@router.get("/merchant/finance", response_class=HTMLResponse)
def merchant_finance(request: Request, db: Session = Depends(get_db)) -> Any:
    """资金页：钱包余额 + 结算单 + 提现记录。"""
    user, merchant = _guard_merchant(request, db, "/merchant/finance")
    if user is None:
        return _redirect("/merchant/login?next=/merchant/finance")
    if merchant is None:
        return _redirect("/merchant/apply")

    summary = svc.merchant_finance_summary(db, merchant)
    return templates.TemplateResponse(
        "merchant/finance.html",
        _ctx(
            request,
            "merchant_finance",
            user=user,
            merchant=merchant,
            bills=svc.list_bills(db, merchant),
            withdraws=svc.list_withdraws(db, merchant),
            **summary,
        ),
    )


@router.post("/merchant/withdraw")
async def merchant_withdraw(request: Request, db: Session = Depends(get_db)) -> Any:
    """商家提交提现申请。"""
    user, merchant = _guard_merchant(request, db, "/merchant/finance")
    if user is None or merchant is None:
        return _redirect("/merchant/finance")

    form = dict(await request.form())
    try:
        amount = Decimal(str(form.get("amount") or "0"))
    except InvalidOperation:
        amount = Decimal("0")

    _, msg = svc.apply_withdraw(
        db,
        merchant,
        amount=amount,
        bank_name=(form.get("bank_name") or "").strip(),
        account_name=(form.get("account_name") or "").strip(),
        bank_account=(form.get("bank_account") or "").strip(),
    )
    return _redirect(f"/merchant/finance?msg={_qs(msg)}&ok=1")


# =====================================================================
# 阶段 4：审核台结算管理
# =====================================================================
@router.get("/platform/settlements", response_class=HTMLResponse)
def platform_settlements(request: Request, db: Session = Depends(get_db)) -> Any:
    """审核台：商家列表 + 钱包 + 结算单，支持按商家生成结算单。"""
    merchants = db.scalars(
        select(Merchant).where(Merchant.status == "approved")
        .order_by(Merchant.id.desc())
    ).all()
    bills = db.scalars(
        select(SettlementBill).order_by(SettlementBill.id.desc()).limit(100)
    ).all()
    wallets = {
        w.merchant_id: w
        for w in db.scalars(select(MerchantWallet)).all()
    }
    return templates.TemplateResponse(
        "merchant/platform_settlements.html",
        _platform_ctx(
            request,
            db,
            "platform_settlements",
            merchants=list(merchants),
            bills=list(bills),
            wallets=wallets,
        ),
    )


@router.post("/platform/settlements/generate")
async def platform_settlement_generate(
    request: Request, db: Session = Depends(get_db)
) -> Any:
    """平台为某商家生成结算单（把已发货/已完成订单汇总成账）。"""
    if _require_admin(request, db) is None:
        return _denied()

    form = dict(await request.form())
    merchant_id = _to_int(form.get("merchant_id"))
    merchant = db.get(Merchant, merchant_id) if merchant_id else None
    if merchant is None:
        return _redirect("/platform/settlements")

    _, msg = svc.generate_settlement(db, merchant)
    return _redirect(f"/platform/settlements?msg={_qs(msg)}&ok=1")


@router.post("/platform/settlements/{bill_id}/settle")
def platform_bill_settle(
    bill_id: int, request: Request, db: Session = Depends(get_db)
) -> Any:
    """平台标记结算单已结算：待结算 → 可提现。"""
    if _require_admin(request, db) is None:
        return _denied()

    bill = db.get(SettlementBill, bill_id)
    if bill is None:
        return _redirect("/platform/settlements")
    _, msg = svc.mark_bill_settled(db, bill)
    return _redirect(f"/platform/settlements?msg={_qs(msg)}&ok=1")


@router.get("/platform/withdraws", response_class=HTMLResponse)
def platform_withdraws(request: Request, db: Session = Depends(get_db)) -> Any:
    """审核台：提现申请审核列表。"""
    rows = db.scalars(
        select(WithdrawApply).order_by(WithdrawApply.id.desc()).limit(100)
    ).all()
    merchants = {m.id: m for m in db.scalars(select(Merchant)).all()}
    return templates.TemplateResponse(
        "merchant/platform_withdraws.html",
        _platform_ctx(
            request,
            db,
            "platform_withdraws",
            rows=list(rows),
            merchants=merchants,
        ),
    )


@router.post("/platform/withdraws/{apply_id}/review")
async def platform_withdraw_review(
    apply_id: int, request: Request, db: Session = Depends(get_db)
) -> Any:
    """平台审核提现：通过（打款）/ 驳回（退回余额）。"""
    if _require_admin(request, db) is None:
        return _denied()

    apply_row = db.get(WithdrawApply, apply_id)
    if apply_row is None:
        return _redirect("/platform/withdraws")

    form = dict(await request.form())
    approve = str(form.get("action", "")) == "approve"
    _, msg = svc.review_withdraw(
        db,
        apply_row,
        approve=approve,
        reviewer="platform",
        note=(form.get("review_note") or "").strip(),
    )
    return _redirect(f"/platform/withdraws?msg={_qs(msg)}&ok=1")


# =====================================================================
# 审核台：商家状态管理（冻结 / 解冻）+ 订单退款审核
# =====================================================================
@router.post("/platform/merchants/{merchant_id}/status")
async def platform_merchant_status(
    merchant_id: int, request: Request, db: Session = Depends(get_db)
) -> Any:
    """冻结 / 解冻商家。冻结后本店商品强制下架。"""
    if _require_admin(request, db) is None:
        return _denied()

    merchant = db.get(Merchant, merchant_id)
    if merchant is None:
        return _redirect("/platform/merchants")

    form = dict(await request.form())
    action = (form.get("action") or "").strip()
    target = {"freeze": "frozen", "unfreeze": "approved"}.get(action)
    if target is None:
        return _redirect("/platform/merchants")

    _, msg = svc.set_merchant_status(
        db, merchant, status=target, note=(form.get("note") or "").strip()
    )
    return _redirect(f"/platform/merchants?msg={_qs(msg)}&ok=1")


@router.get("/platform/orders", response_class=HTMLResponse)
def platform_orders(
    request: Request,
    db: Session = Depends(get_db),
    keyword: str = Query(""),
) -> Any:
    """审核台订单管理：退款审核（标记退款后从下期结算扣回）。"""
    orders = svc.list_refund_candidates(db, keyword=keyword or None)
    merchants = {m.id: m for m in db.scalars(select(Merchant)).all()}
    for o in orders:
        o.status_label = svc.order_status_text(o.status)
        o.amount = svc.order_goods_amount(o)

    return templates.TemplateResponse(
        "merchant/platform_orders.html",
        _platform_ctx(
            request,
            db,
            "platform_orders",
            orders=orders,
            merchants=merchants,
            keyword=keyword,
        ),
    )


@router.post("/platform/orders/{order_id}/refund")
async def platform_order_refund(
    order_id: int, request: Request, db: Session = Depends(get_db)
) -> Any:
    """平台标记订单退款：释放预占库存，已结算的从下期扣回。"""
    if _require_admin(request, db) is None:
        return _denied("需要平台管理员登录后才能审核退款")

    order = db.get(DownstreamOrder, order_id)
    if order is None:
        return _redirect("/platform/orders")

    form = dict(await request.form())
    _, msg = svc.refund_order(db, order, reason=(form.get("reason") or "").strip())
    return _redirect(f"/platform/orders?msg={_qs(msg)}&ok=1")


def _qs(msg: str) -> str:
    """把提示语塞进 query string（简单转义，避免破坏 URL）。"""
    from urllib.parse import quote

    return quote(msg, safe="")


def _to_int(value: Any) -> int:
    try:
        return int(str(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = ["router"]
