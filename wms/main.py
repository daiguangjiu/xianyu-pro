"""检测仓独立部署入口（不由渔枢系统挂载时用它）。

    WMS_DATABASE_URL=postgresql://…  python -m wms.main   # 默认 0.0.0.0:8100

合并部署不需要它 —— 渔枢系统的 ``app/main.py`` 直接 ``include_router`` 同一份路由；
两条路共用同一份业务逻辑、同一份模板，行为一致。

**登录**：独立部署时由网关 / SSO 负责，透传 ``X-Forwarded-User``（账号名）与
``X-Forwarded-User-Id`` 两个请求头；``WMS_STANDALONE_ADMIN`` 里写的管理员名单
等价于平台管理员（检测仓全部权限）。
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import bridge
from .web import install, public_router, router

logger = logging.getLogger("wms")


def create_app() -> FastAPI:
    bridge  # 触发模块加载（保留引用，便于测试里 patch bridge.current_identity）
    app = FastAPI(
        title="货品检测入库出库管理系统",
        version=os.getenv("WMS_VERSION", "1.0.0"),
        docs_url="/api/docs",
    )
    # 自己的静态资源：不与平台共用目录，避免改动互相踩
    static_dir = os.getenv("WMS_STATIC_DIR", "")
    if static_dir and os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/api/health", include_in_schema=False)
    def health():
        return {"ok": True, "service": "wms", "mode": "standalone"}

    app.include_router(router)
    # 云打印回调：不带登录态的公网入站（独立部署时没有 AuthMiddleware，只需绕开子系统守卫）
    app.include_router(public_router)
    install(app)
    return app


app = create_app()


def _run() -> None:  # pragma: no cover
    import uvicorn

    from .startup import startup

    startup()
    uvicorn.run(
        app,
        host=os.getenv("WMS_HOST", "0.0.0.0"),
        port=int(os.getenv("WMS_PORT", "8100") or 8100),
    )


if __name__ == "__main__":
    _run()
