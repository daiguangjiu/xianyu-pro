"""后台登录与权限：账号存库、会话 Cookie、操作日志埋点。

- 账号密码哈希用标准库 pbkdf2_hmac 实现，不额外引入依赖；
- 会话用「用户 id + 过期时间 + HMAC 签名」的无状态 Cookie，登出即失效；
- 脚本 / curl 仍可用 HTTP Basic 带上账号密码调用接口（同样走库内账号校验）。
飞书回调、健康检查与静态资源必须放行，否则飞书推送会被登录跳转挡掉。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from typing import Awaitable, Callable
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp

from ..config import BASE_DIR, settings
from ..db import SessionLocal
from ..models import User
from ..timeutil import now
from . import permissions

# 不需要登录的路径：飞书回调、健康检查、登录页、静态资源
# 渔货栈（挂在 /self 下）共用这套登录，商家的注册 / 登录页与 SSO 跳转入口要放行
SELF_PUBLIC_PATHS = {"/self/merchant/login", "/self/merchant/register", "/self/sso/login"}
PUBLIC_PATHS = {
    "/feishu/event",
    "/api/feishu/event",
    "/api/health",
    "/health",
    "/login",
    # 芯烨云打印回调（2026-10-09）：平台服务器不带登录 Cookie，必须放行
    # （WMS 那边另有 public_router 绕开子系统守卫，两道门缺一不可）
    "/wms/api/cloudprint/callback",
} | SELF_PUBLIC_PATHS

# 前缀放行（PUBLIC_PATHS 是精确匹配，多段路径必须走前缀）：
# · /media/cards/ = 引流名片的封面图 —— 抖音爬虫抓图**不带 Cookie**，
#   卡在登录页就是一张空白卡片，所以这个目录必须裸奔（订单截图所在的其它
#   media 子目录仍然要登录，不跟着一起放）；
# · /c/ = 名片长链接本身（https://域名/c/<slug>），同理。
PUBLIC_PREFIXES = ("/static", "/media/cards/", "/c/")
COOKIE_NAME = "oh_session"
ROLE_ADMIN = "admin"
ROLE_USER = "user"

logger = logging.getLogger("order-hub.auth")

_ITERATIONS = 200_000
_ALGO = "pbkdf2_sha256"
_SECRET_FILE = BASE_DIR / "data" / ".secret_key"


# ------------------------------------------------------------------ 密码
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _ITERATIONS
    )
    return f"{_ALGO}${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, hexdigest = str(stored or "").split("$")
    except ValueError:
        return False
    if algo != _ALGO:
        return False
    try:
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(digest.hex(), hexdigest)


# ------------------------------------------------------------------ 会话
def _secret_key() -> str:
    """会话签名密钥：优先 .env，否则生成一份存到 data/.secret_key。"""
    if settings.secret_key:
        return settings.secret_key
    try:
        if _SECRET_FILE.exists():
            key = _SECRET_FILE.read_text(encoding="utf-8").strip()
            if key:
                settings.secret_key = key
                return key
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(32)
        _SECRET_FILE.write_text(key, encoding="utf-8")
        settings.secret_key = key
        return key
    except OSError:
        # 只读环境兜底：进程内随机密钥（重启后需要重新登录）
        if not settings.secret_key:
            settings.secret_key = secrets.token_urlsafe(32)
        return settings.secret_key


def _signature(user_id: int, expires_at: int) -> str:
    raw = f"{user_id}.{expires_at}".encode("utf-8")
    return hmac.new(_secret_key().encode("utf-8"), raw, hashlib.sha256).hexdigest()


def make_session(user_id: int) -> tuple[str, int]:
    """返回 (Cookie 值, 有效秒数)。"""
    ttl = max(settings.session_ttl_hours, 1) * 3600
    expires_at = int(time.time()) + ttl
    return f"{user_id}.{expires_at}.{_signature(user_id, expires_at)}", ttl


def parse_session(value: str) -> int | None:
    """校验签名与有效期，返回用户 id。"""
    parts = str(value or "").split(".")
    if len(parts) != 3:
        return None
    try:
        user_id, expires_at = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if expires_at < time.time():
        return None
    if not hmac.compare_digest(parts[2], _signature(user_id, expires_at)):
        return None
    return user_id


# ------------------------------------------------------------------ 账号
def _verify_legacy(session, user: User, password: str) -> bool:
    """迁过来的渔枢系统账号：老密码（bcrypt）第一次登录时校验，并升级成这套系统的哈希。

    老用户不用先重置密码 —— 登录一次之后就按新格式存，旧哈希随即清掉。
    """
    legacy = str(getattr(user, "legacy_password_hash", "") or "")
    if not legacy.startswith("$2"):
        return False
    try:
        import bcrypt
    except Exception:  # noqa: BLE001  没装 bcrypt（独立部署）时不支持老密码
        return False
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), legacy.encode("utf-8"))
    except Exception:  # noqa: BLE001  哈希损坏 / 密码超长
        return False
    if not ok:
        return False
    user.password_hash = hash_password(password)
    user.legacy_password_hash = None
    session.commit()
    return True


def verify_user_password(session, user, password: str) -> bool:
    """校验一个账号的密码：先按本系统的哈希，再按迁过来的老哈希（bcrypt）。

    渔货栈（/self）与渔枢系统共用这一个入口 —— 两端一套账号、一套密码，
    在哪一端改的密码都一样生效（渔货栈的登录也走它，见 src/merchant/service.py）。
    """
    if verify_password(password, user.password_hash):
        return True
    # 迁过来的老账号：老密码（bcrypt）首次登录时自动换成这套系统的哈希
    return _verify_legacy(session, user, password)


def authenticate(username: str, password: str) -> User | None:
    """校验账号密码；账号不存在、停用或密码错误都返回 None。

    账号既可以是用户名，也可以是手机号 —— 渔货栈的老账号是按手机号
    注册的，两端合并成一套账号之后，拿手机号在这里也能直接登（见 User.phone）。
    """
    username = str(username or "").strip()
    if not username or not password:
        return None
    with SessionLocal() as session:
        user = session.scalar(
            select(User).where(or_(User.username == username, User.phone == username))
        )
        if not user or user.status != "active":
            return None
        if not verify_user_password(session, user, password):
            return None
        user.last_login_at = now()
        session.commit()
        # 会话里只带 id，这里返回一份脱敏快照（绑定不到别的会话）
        return User(
            id=user.id,
            username=user.username,
            phone=user.phone,
            display_name=user.display_name,
            role=user.role,
            status=user.status,
        )


def ensure_default_admin() -> User:
    """保证至少有一个管理员账号：首次启动自动创建。

    账号取 ADMIN_USERNAME（留空 admin），密码取 ADMIN_PASSWORD（留空 xianyu2026）。
    """
    username = (settings.admin_username or "admin").strip() or "admin"
    password = settings.admin_password or "xianyu2026"
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.role == ROLE_ADMIN))
        if user is None:
            user = session.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(
                username=username,
                display_name="管理员",
                password_hash=hash_password(password),
                role=ROLE_ADMIN,
                status="active",
            )
            session.add(user)
            session.commit()
        elif user.role != ROLE_ADMIN:
            user.role = ROLE_ADMIN
            session.commit()
        return User(id=user.id, username=user.username, role=user.role, status=user.status)


def load_user(user_id: int | None) -> User | None:
    if not user_id:
        return None
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if not user or user.status != "active":
            return None
        # 只带页面上用得到的字段；role_id 要带上 —— 角色权限（菜单与页面内区块）
        # 都靠它判断（见 web/permissions.py），漏了就退回「默认菜单」，角色配了也不生效
        return User(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            role_id=user.role_id,
            status=user.status,
        )


def _basic_credentials(request: Request) -> tuple[str, str]:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return "", ""
    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except Exception:  # noqa: BLE001
        return "", ""
    username, _, password = raw.partition(":")
    return username, password


def current_user(request: Request) -> User | None:
    """当前登录用户：Cookie 优先，其次 HTTP Basic（脚本/curl 用）。"""
    user = load_user(parse_session(request.cookies.get(COOKIE_NAME, "")))
    if user:
        return user
    username, password = _basic_credentials(request)
    if username:
        return authenticate(username, password)
    return None


def set_session_cookie(response: Response, user_id: int) -> None:
    value, ttl = make_session(user_id)
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


# ------------------------------------------------------------------ 中间件
class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(  # type: ignore[override]
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        if settings.auth_public_media and path.startswith("/media"):
            return await call_next(request)

        user = current_user(request)
        if user is None:
            if path.startswith("/api"):
                return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
            target = path
            if request.url.query:
                target = f"{path}?{request.url.query}"
            return RedirectResponse(f"/login?next={quote(target, safe='/?=&')}", status_code=303)

        # 菜单级权限（见 web/permissions.py）：路径能反查到菜单项、而账号没有这个菜单，
        # 就退回首页 —— 菜单不显示只是「看不见」，这里才是真正的那道门。
        # 详情页与接口反查不到菜单项，一律放行（跟着列表页的权限走）。
        if not permissions.can_access_path(user, path):
            logger.info("账号 %s 无权访问 %s，退回它能进的第一个页面", user.username, path)
            if path.startswith("/api"):
                return JSONResponse({"ok": False, "error": "没有权限访问该功能"}, status_code=403)
            # 退回「这个账号能进的第一个页面」：一律退回首页会死循环（首页本身可能也没权限）
            fallback = permissions.first_allowed_path(user)
            if not fallback:
                return HTMLResponse(
                    "<h1>没有可访问的页面</h1>"
                    "<p>这个账号还没有分配任何菜单权限，请联系管理员在「角色权限」里配置。</p>",
                    status_code=403,
                )
            return RedirectResponse(fallback, status_code=303)

        # 写操作留痕（登录/登出由登录路由自己记）
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            await self._log_action(request, user)
        return await call_next(request)

    @staticmethod
    async def _log_action(request: Request, user: User) -> None:
        from ..services.audit import describe, detail_from_body, log_action

        action, target = describe(request.method, request.url.path)
        if not action:
            return
        try:
            # _CachedRequest 会把 body 缓存后回放给下游，不影响表单解析
            body = await request.body()
            detail = detail_from_body(request.headers.get("content-type", ""), body)
            with SessionLocal() as session:
                log_action(session, user, action, target, detail, request)
        except Exception as exc:  # noqa: BLE001  日志失败不能影响业务
            logger.warning("写操作日志失败: %s", exc)


class HtmlNoCacheMiddleware(BaseHTTPMiddleware):
    """缓存策略：页面 HTML 不缓存，静态资源按情况缓存。

    HTML 一律不缓存：升级后刷新就能看到新菜单 / 新样式，不会一直显示旧页面。
    静态资源相反 —— URL 带 ?v=<版本号> 的（骨架引用的样式 / 脚本）升级后 URL
    变化自然失效，放长缓存最省带宽；没带版本号的（图片、语音 mp3、订单截图）
    给 1 小时，避免换了内容还显示旧的。跨公网访问时这一项最影响首屏体感。
    """

    async def dispatch(  # type: ignore[override]
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/html"):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        elif request.url.path.startswith(("/static/", "/media/")):
            if request.url.query.strip():
                # 带 ?v= 版本号：版本号一变 URL 就变，放长缓存不会失效不了
                response.headers["Cache-Control"] = "public, max-age=2592000, immutable"
            else:
                response.headers["Cache-Control"] = "public, max-age=3600"
        return response
