"""Celery 异步任务。

启动 worker：
    celery -A src.worker.celery_app worker -l info

任务清单：
- run_selection_task     跑选品规则链

（1688 相关任务 —— 同步商品 / 推单 / 物流 —— 已于 2026-10-13 整体下线。）
"""
from src.worker.celery_app import celery_app

__all__ = ["celery_app"]
