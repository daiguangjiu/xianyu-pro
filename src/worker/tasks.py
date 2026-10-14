"""Celery 任务定义。

每个任务都是入口很薄，内部委托给 services。

（1688 相关任务 —— 商品同步 sync_goods_task、推单 push_purchase_task、
物流同步 sync_logistics_task —— 已于 2026-10-13 整体下线：
- 1688 假数据 2026-10-04 已清，无定时同步；
- 推单改为订单页「推给渔枢系统采购」（src/order/flow.py），不再走 1688 代发；
- Celery worker 本就不在部署编排里，这些任务从未在线上执行过。）
"""
from __future__ import annotations

import logging

from src.deps import session_scope  # 提供上下文管理器式 DB session
from src.selection import run_selection
from src.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="selection.run", bind=True, max_retries=2)
def run_selection_task(
    self,
    item_ids: list[str] | None = None,
    shadow_mode: bool = False,
    channel: str = "xianyu",
):
    """选品规则流水线（接 selection.engine.run_selection）。"""
    logger.info("run_selection_task item_ids=%s shadow=%s channel=%s", item_ids, shadow_mode, channel)
    with session_scope() as db:
        result = run_selection(
            db,
            item_ids=item_ids,
            channel=channel,
            shadow_mode=shadow_mode,
        )
    return result.to_dict()


__all__ = [
    "run_selection_task",
]
