"""Celery 应用实例。"""
from celery import Celery

from src.config import get_settings

settings = get_settings()

celery_app = Celery(
    "supply_chain_xiaoyu",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["src.worker.tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    timezone="Asia/Shanghai",
    enable_utc=False,
)

__all__ = ["celery_app"]