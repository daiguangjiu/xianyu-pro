"""FastAPI 入口。

启动方式：
    uvicorn src.main:app --reload

包含：
- /docs         Swagger UI（FastAPI 自动生成）
- /api/v1/...   业务路由（由 src.api.v1.api_router 统一挂载）
"""
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.config import get_settings
from src import __version__
from src.api.v1 import api_router
from src.deps import session_scope
from src.merchant.service import ensure_platform_admin as svc_ensure_platform_admin
from src.web.router import router as web_router
from src.merchant.router import router as merchant_web_router
from src.external_identity.router import router as external_identity_router
from src.external_identity.products import router as external_products_router
from src.external_identity.categories import router as external_categories_router


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动保障：确保存在平台管理员账号。

    审核台所有审核动作（入驻 / 商品 / 退款 / 结算 / 提现）都要求 platform_admin，
    而该角色不开放注册。若新环境没有管理员，审核台将无人可操作 —— 这里兜底创建。
    失败只告警不阻断启动（避免数据库不可用时整个应用起不来）。
    """
    try:
        with session_scope() as db:
            svc_ensure_platform_admin(db)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("ensure_platform_admin failed: %s", exc)
    yield


def create_app() -> FastAPI:
    """应用工厂，便于后续接入 pytest / 多 worker 配置。"""
    settings = get_settings()

    app = FastAPI(
        lifespan=_lifespan,
        title="渔货栈 Supply Chain Xiaoyu",
        description=(
            "渔货栈 - 学员和自营团队的选货平台（货源 / 铺货 / 采购）。<br>"
            "子系统：渔货栈（选货 / 铺货 / 采购，SSO 接入闲参谋）+ 渔枢系统（商品运营与订单中台）。"
        ),
        version=__version__,
        debug=settings.app_debug,
    )

    # CORS（开发期放开；生产应按白名单收敛）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.app_debug else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ====== 统一挂载 v1 业务路由 ======
    # 详细子模块在 src/api/v1/__init__.py 中组装
    app.include_router(api_router, prefix="/api/v1")
    app.include_router(external_identity_router)
    app.include_router(external_products_router)
    app.include_router(external_categories_router)

    # ====== Web UI（HTML 模板，只读） ======
    # 详见 src/web/router.py；include_in_schema=True 已在路由内关闭，避免污染 Swagger
    app.include_router(web_router)
    # 页面里的站内链接统一走 self_url（带 config.self_prefix，默认 /self）：
    # 合并部署时渔枢系统也是这个前缀挂的，渔货栈单独跑时这里补一份，链接才不会 404。
    if settings.self_prefix:
        app.include_router(web_router, prefix=settings.self_prefix)
    # 商家入驻 + 商家端后台（/merchant/... · /platform/applies · /platform/items）
    app.include_router(merchant_web_router)

    # ====== 本地上传图片静态服务（uploads/） ======
    import pathlib
    uploads_dir = pathlib.Path("uploads")
    uploads_dir.mkdir(exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

    # ====== 品牌静态资源（static/：渔货栈 logo / favicon） ======
    static_dir = pathlib.Path("static")
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


app = create_app()