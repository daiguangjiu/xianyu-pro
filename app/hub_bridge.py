"""把渔货栈（自营选品平台，代码在仓库根 `src/`）挂到渔枢系统应用上。

两端跑在同一个进程、同一个端口（8000）：

- 渔货栈的页面统一挂在 `/self` 前缀下 —— 渔枢系统自己有 `/orders`、`/changelog`，
  不加前缀会撞车；老地址 `/hub/*` 会 308 跳到 `/self/*`；
- 渔货栈的对外接口保持 `/api/v1` 原路径 —— 闲参谋的 SSO 跳转、发布包票据都按这个地址
  对接，改了外部就断；
- 登录走渔枢系统这套（见 `app/hub_auth.py`），能不能进看账号的子系统权限。
"""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .config import BASE_DIR
from .hub_auth import unified_user
from .portals import PORTAL_SELF, portal, portal_of_path, switch_options

logger = logging.getLogger("order-hub.hub")

# 渔货栈页面的统一前缀：模板里的链接、静态资源都按它走
# （不用登录也能进的渔货栈页面见 web/auth.py 的 SELF_PUBLIC_PATHS）
SELF_PREFIX = "/self"
# 旧前缀（v1.20 之前渔货栈挂在 /hub 下）：老书签 / 外部链接 308 跳到新前缀
LEGACY_PREFIX = "/hub"
# 渔货栈的选货页：货源广场（含热销榜单 / 采购节点）。
# 「我的上架」页已下线（2026-10-11），它以前也在这里。
SELF_SHARED_PATHS = (f"{SELF_PREFIX}/harbor",)
# 渔货栈首页（2026-10-03 起 = 货源广场，/self/ 只做一次跳转）：品牌 logo、端切换都打在 /self/ 上
SELF_HOME_PATHS = (SELF_PREFIX, f"{SELF_PREFIX}/")

# 这些页面**不按端拦**（两端都能进）：首页跳转 + 选货页。
# 2026-10-11 修：以前把它们划给「渔枢系统端」，而账号的端是管理员在「用户管理」里勾的 ——
# 学员账号只勾了「渔货栈」时（portals=self），一登录就被 403 拦在自己的首页上
# （现场：liuyuhui 进 /self/harbor 报「没有该页面的访问权限」）。
# 选货页本来就是渔货栈两端共用的功能：渔货栈账号要铺货、渔枢（学员）账号要选货。
SELF_OPEN_PATHS = SELF_HOME_PATHS + SELF_SHARED_PATHS


def needed_portal(path: str) -> str:
    """这个渔货栈页面属于哪个端。

    返回空串 = **不限端**：渔货栈首页与选货页（货源广场 / 我的上架）——
    两端账号都得进（见 SELF_OPEN_PATHS 上面的说明）。
    其余页面归渔货栈（/self 下的订单、采购、店铺、地址等）。
    """
    text = str(path or "")
    if text in SELF_OPEN_PATHS or text.startswith(SELF_SHARED_PATHS):
        return ""
    return PORTAL_SELF


def require_hub(request: Request) -> None:
    """进渔货栈页面的门槛：已登录 + （这个页面要求特定端时）那个端有权限。"""
    user = unified_user(request)
    if user is None:
        # 正常流程下渔枢系统的登录中间件会先拦一道，这里是兜底
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    need = needed_portal(request.url.path)
    if need and not user.has_portal(need):
        raise HTTPException(
            status_code=403,
            detail="当前账号没有该页面的访问权限，请联系管理员在「用户管理」里开通「渔货栈」端",
        )


def portal_switch_ctx(request: Request) -> dict[str, object]:
    """渔枢系统模板里的端切换器数据：只显示有权限的端，并标出当前所在的端。"""
    user = unified_user(request)
    return {
        "portals": switch_options(user, request.url.path),
        "current": portal(portal_of_path(request.url.path)),
    }


class _LegacyPrefixRedirectMiddleware(BaseHTTPMiddleware):
    """老地址 `/hub/*` 永久跳到新地址 `/self/*`。

    渔货栈以前挂在 /hub 下，书签、外部链接、文档里都是旧地址；308 保留请求方法，
    老表单 POST 过来也不会丢。
    """

    async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if path == LEGACY_PREFIX or path.startswith(LEGACY_PREFIX + "/"):
            tail = path[len(LEGACY_PREFIX):]
            target = f"{SELF_PREFIX}{tail}" if tail else f"{SELF_PREFIX}/"
            if request.url.query:
                target = f"{target}?{request.url.query}"
            return RedirectResponse(target, status_code=308)
        return await call_next(request)


def _patch_templates(templates: Jinja2Templates) -> None:
    """给渔枢系统自己的 Jinja2Templates 打补丁，兼容旧写法 `TemplateResponse(name, ctx)`。

    渔枢系统的代码（仓库根 `src/`）按旧版 FastAPI 写；现在的 FastAPI 要求
    `TemplateResponse(request, name, ctx)` —— 直接跑会把模板名当成 request、把 ctx 当成
    模板名（页面全 500，报 unhashable type: 'dict'）。这里在第一参数是模板名时自动从
    ctx 里取 request 补上，渔枢系统源码一行不用改。

    注意是**原地**打补丁而不是换一个新的 templates：渔枢系统在模块加载时往
    `templates.env.globals` 里注册过变量（如状态文案表），换实例会把它们弄丢。
    """
    original = templates.TemplateResponse

    def template_response(*args, **kwargs):
        if args and isinstance(args[0], str):
            name = args[0]
            context = args[1] if len(args) > 1 else (kwargs.pop("context", None) or {})
            request = context.get("request") if isinstance(context, dict) else None
            return original(request, name, context, **kwargs)
        return original(*args, **kwargs)

    templates.TemplateResponse = template_response  # type: ignore[method-assign]


def mount_hub(app: FastAPI) -> None:
    """把渔货栈的页面 / 接口 / 静态资源接到渔枢系统应用上。"""
    from src.api.v1 import api_router as hub_api_router
    from src.external_identity.categories import router as external_categories_router
    from src.external_identity.products import router as external_products_router
    from src.external_identity.router import router as external_identity_router
    from src.merchant import router as hub_merchant_module
    from src.merchant.router import router as hub_merchant_router
    from src.web import router as hub_web_module
    from src.web.router import router as hub_web_router

    # 渔货栈的模板调用是旧版写法，原地打补丁（见 _patch_templates）
    _patch_templates(hub_web_module.templates)
    _patch_templates(hub_merchant_module.templates)
    # 渔货栈页面顶部也要有统一的端切换器（渔枢系统 / 渔货栈）
    hub_web_module.templates.env.globals["portal_switch"] = portal_switch_ctx
    hub_merchant_module.templates.env.globals["portal_switch"] = portal_switch_ctx
    # 老地址 /hub/* 跳到 /self/*（见 _LegacyPrefixRedirectMiddleware）
    app.add_middleware(_LegacyPrefixRedirectMiddleware)

    # 页面：统一加 /self 前缀，并统一做登录 + 端权限校验
    app.include_router(hub_web_router, prefix=SELF_PREFIX, dependencies=[Depends(require_hub)])
    app.include_router(hub_merchant_router, prefix=SELF_PREFIX, dependencies=[Depends(require_hub)])
    # 对外接口：保持 /api/v1 原路径（外部按这个地址对接，不能改）
    app.include_router(hub_api_router, prefix="/api/v1")
    # xh-fish 服务间接口：依靠接口内的 HMAC 签名鉴权，由 AuthMiddleware 放行此公开前缀。
    app.include_router(external_identity_router)
    app.include_router(external_products_router)
    app.include_router(external_categories_router)
    # 静态资源：渔枢系统已经占了 /static 与 /media，渔货栈用自己的前缀
    app.mount(
        f"{SELF_PREFIX}/static",
        StaticFiles(directory=str(BASE_DIR / "static")),
        name="self_static",
    )
    uploads = BASE_DIR / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    app.mount(f"{SELF_PREFIX}/uploads", StaticFiles(directory=str(uploads)), name="self_uploads")
    logger.info("渔货栈已挂载在 %s（页面）与 /api/v1（对外接口）", SELF_PREFIX)


def ensure_hub_admin() -> None:
    """启动保障：渔枢系统侧的平台管理员账号（审核台没人能操作就废了）。

    失败只告警不阻断 —— 渔枢系统的库连不上时，渔枢系统该用还得能用。
    """
    try:
        from src.deps import session_scope
        from src.merchant.service import ensure_platform_admin as svc_ensure_platform_admin

        with session_scope() as db:
            svc_ensure_platform_admin(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("渔枢系统平台管理员初始化失败（不影响渔枢系统）：%s", exc)
