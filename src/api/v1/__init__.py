"""API v1 总线：聚合所有业务模块的 router。

新增业务模块时：
1. 在 `src/<module>/router.py` 定义 `router = APIRouter()`
2. 在本文件 `import` 并 `_build_module(...)` 一行
3. 完成！无需改 `main.py`
"""
from fastapi import APIRouter

from src.goods.router import router as goods_router
from src.selection.router import router as selection_router
from src.order.router import router as order_router
from src.purchase.router import router as purchase_router
from src.channel_xianyu.router import router as xianyu_router
from src.logistics.router import router as logistics_router
from src.upload.router import router as upload_router

api_router = APIRouter()


def _mount(module_router, prefix: str, tag: str) -> None:
    """统一挂载：prefix 必须以 / 开头；tag 用于 Swagger 分组。"""
    api_router.include_router(module_router, prefix=prefix, tags=[tag])


# ====== 业务模块挂载 ======
_mount(goods_router, prefix="/goods", tag="goods")
_mount(selection_router, prefix="/selection", tag="selection")
_mount(order_router, prefix="/orders", tag="orders")
_mount(purchase_router, prefix="/purchase", tag="purchase")
_mount(xianyu_router, prefix="/xianyu", tag="xianyu")
_mount(logistics_router, prefix="/logistics", tag="logistics")
_mount(upload_router, prefix="/upload", tag="upload")


# ====== 后续扩展位 ======
# _mount(taobao_router, prefix="/taobao", tag="taobao")
# _mount(douyin_router, prefix="/douyin", tag="douyin")

__all__ = ["api_router"]