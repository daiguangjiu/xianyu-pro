"""检测仓的 Web 层：自己的 Jinja 环境、自己的布局、自己的路由。

宿主（渔枢系统的 app 或独立部署的 ``wms.main``）只需要做两件事：

    from wms.web.router import router, install
    app.include_router(router)
    install(app)          # 挂上「没登录 / 没权限」两个跳转处理器
"""
from __future__ import annotations

from .router import AccessDenied, NeedLogin, install, public_router, router

__all__ = ["router", "public_router", "install", "NeedLogin", "AccessDenied"]
