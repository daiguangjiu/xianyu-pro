"""API 路由聚合器。

把所有模块的 router 集中起来，按版本挂载到 FastAPI app。

使用：
    from src.api.v1 import api_router
    app.include_router(api_router, prefix="/api")
"""

from src.api.v1 import api_router

__all__ = ["api_router"]