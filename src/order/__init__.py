"""下游订单接收与处理。

- 接收来自闲鱼回调 / 手动录入
- 校验 SKU 映射
- 库存复核 + 预占
- 触发推单
"""

from src.order.service import (
    AcceptedOrderResult,
    InsufficientStock,
    ItemOversold,
    OrderAcceptError,
    SkuMappingMissing,
    accept_order,
    get_order_with_pos,
)

__all__ = [
    "accept_order",
    "AcceptedOrderResult",
    "get_order_with_pos",
    "OrderAcceptError",
    "SkuMappingMissing",
    "InsufficientStock",
    "ItemOversold",
]