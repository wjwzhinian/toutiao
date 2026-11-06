"""Celery Worker/Beat 入口，任务内部继续调用已有异步流水线服务。"""

from __future__ import annotations

import asyncio

from celery import Celery

from config.settings import get_settings

settings = get_settings()
celery_app = Celery(
    "news_headline",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["celery_app"],
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=settings.app_timezone,
    enable_utc=True,
    worker_concurrency=1,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    beat_schedule={
        "news-pipeline-periodic": {
            "task": "news.run_pipeline",
            "schedule": settings.auto_pipeline_interval_seconds,
        },
        # 向量索引采用周期对账，修复 DB 已提交但任务消息未成功投递的情况。
        "news-vector-index-reconcile": {
            "task": "news.sync_vector_index",
            "schedule": 60,
        },
    },
)

# Celery sync worker 進程內保留同一個事件循環，避免 AsyncEngine/Redis 客戶端
# 在每個 asyncio.run() 之間跨循環重用連線池。
_worker_loop: asyncio.AbstractEventLoop | None = None


@celery_app.task(
    name="news.run_pipeline",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_kwargs={"max_retries": 3},
)
def run_pipeline() -> dict[str, int]:
    """執行一輪採集、審核與受規則控制的自動發布。"""
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)

    from services.auto_pipeline import run_auto_pipeline_tick

    return _worker_loop.run_until_complete(run_auto_pipeline_tick())


@celery_app.task(
    name="news.sync_vector_index",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_kwargs={"max_retries": 3},
)
def sync_vector_index() -> dict[str, int | str]:
    """异步补建、更新或清理新闻向量，不占用 API 请求线程。"""
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)

    from services.vector_index import reconcile_vector_index_batch

    return _worker_loop.run_until_complete(reconcile_vector_index_batch())


@celery_app.task(
    name="news.rebuild_vector_index",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_kwargs={"max_retries": 3},
)
def rebuild_vector_index() -> dict[str, int | str]:
    """后台提交一次全量重建请求，实际 Embedding 由后续批次逐篇完成。"""
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_worker_loop)

    from services.vector_index import mark_vector_index_for_rebuild

    count = _worker_loop.run_until_complete(mark_vector_index_for_rebuild())
    return {"status": "queued", "existing_items": count}
