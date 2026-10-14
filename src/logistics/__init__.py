"""物流回写。

- 人工推送 tracking 号（receive_callback）→ 回写 DownstreamOrder
- （1688 物流主动拉取已于 2026-10-13 下线）
"""
from src.logistics.service import (
    LogisticsError,
    LogisticsSnapshot,
    OrderNotFound,
    get_logistics,
    receive_callback,
)

__all__ = [
    "receive_callback",
    "get_logistics",
    "LogisticsSnapshot",
    "LogisticsError",
    "OrderNotFound",
]
