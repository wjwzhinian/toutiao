"""新闻向量索引的异步对账、重试和重建流程。"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config.db_confing import AsyncSessionLocal
from config.settings import get_settings
from models.news import News, NewsVectorIndexState
from services.vector_store import QdrantVectorStore, VectorStoreError

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """项目数据库保存无时区 UTC 时间，统一从此函数生成时间戳。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fingerprint(news: News) -> str:
    """用新闻可检索字段生成摘要，检测更新时间精度不足时的内容变化。"""
    content = "\x1f".join(
        str(value or "")
        for value in (
            news.title,
            news.description,
            news.content,
            news.author,
            news.source_url,
            news.category_id,
            news.publish_time.isoformat() if news.publish_time else "",
        )
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def reconcile_vector_index_batch(
    *,
    db: AsyncSession | None = None,
    batch_size: int | None = None,
) -> dict[str, int | str]:
    """异步补齐缺失向量、更新过期向量，并清理已撤回或删除的新闻。"""
    settings = get_settings()
    if not settings.qdrant_enabled:
        return {"status": "disabled", "indexed": 0, "deleted": 0, "failed": 0}

    owns_session = db is None
    session = db or AsyncSessionLocal()
    store = QdrantVectorStore(settings)
    maximum = batch_size or settings.vector_index_batch_size
    now = _utcnow()
    work_ids: list[int] = []
    stats: dict[str, int | str] = {"status": "ok", "indexed": 0, "deleted": 0, "failed": 0}

    try:
        # 先取新任务和到达退避时间的失败项，已提交的待办可在 Worker 重启后继续执行。
        retry_result = await session.execute(
            select(NewsVectorIndexState)
            .where(
                or_(
                    NewsVectorIndexState.status == "pending",
                    and_(
                        NewsVectorIndexState.status == "failed",
                        or_(NewsVectorIndexState.next_retry_at.is_(None), NewsVectorIndexState.next_retry_at <= now),
                    ),
                )
            )
            .order_by(NewsVectorIndexState.updated_at.asc(), NewsVectorIndexState.news_id.asc())
            .limit(maximum)
        )
        for state in retry_result.scalars().all():
            if state.news_id not in work_ids:
                work_ids.append(state.news_id)

        remaining = max(0, maximum - len(work_ids))
        if remaining:
            # 对账会发现首次启用后的历史稿、Embedding 版本变化和应用外的数据库修改。
            news_result = await session.execute(
                select(News)
                .outerjoin(NewsVectorIndexState, NewsVectorIndexState.news_id == News.id)
                .where(
                    News.status == "published",
                    or_(
                        NewsVectorIndexState.news_id.is_(None),
                        NewsVectorIndexState.status == "deleted",
                        and_(
                            NewsVectorIndexState.status == "indexed",
                            or_(
                                NewsVectorIndexState.embedding_version != settings.embedding_version,
                                NewsVectorIndexState.source_updated_at.is_(None),
                                NewsVectorIndexState.source_updated_at < News.updated_at,
                            ),
                        ),
                    ),
                )
                .order_by(News.updated_at.asc(), News.id.asc())
                .limit(remaining)
            )
            for news in news_result.scalars().all():
                state = await session.get(NewsVectorIndexState, news.id)
                if state is None:
                    state = NewsVectorIndexState(news_id=news.id, status="pending")
                    session.add(state)
                else:
                    state.status = "pending"
                    state.next_retry_at = None
                if news.id not in work_ids:
                    work_ids.append(news.id)

        # 先把索引中已撤回或已删除的新闻转成待办，再通过同一幂等删除接口清理。
        stale_result = await session.execute(
            select(NewsVectorIndexState)
            .outerjoin(News, News.id == NewsVectorIndexState.news_id)
            .where(
                NewsVectorIndexState.status == "indexed",
                or_(News.id.is_(None), News.status != "published"),
            )
            .order_by(NewsVectorIndexState.updated_at.asc())
            .limit(maximum)
        )
        for state in stale_result.scalars().all():
            state.status = "pending"
            state.next_retry_at = None
            if state.news_id not in work_ids and len(work_ids) < maximum:
                work_ids.append(state.news_id)
        await session.commit()

        for news_id in work_ids:
            state = await session.get(NewsVectorIndexState, news_id)
            news = await session.get(News, news_id)
            try:
                if news is not None and news.status == "published":
                    fingerprint = _fingerprint(news)
                    await store.upsert_news(news, fingerprint)
                    state.status = "indexed"
                    state.content_fingerprint = fingerprint
                    state.embedding_version = settings.embedding_version
                    state.source_updated_at = news.updated_at
                    state.indexed_at = _utcnow()
                    state.attempts = 0
                    state.next_retry_at = None
                    state.last_error = None
                    stats["indexed"] = int(stats["indexed"]) + 1
                else:
                    await store.delete_news(news_id)
                    state.status = "deleted"
                    state.content_fingerprint = None
                    state.embedding_version = settings.embedding_version
                    state.source_updated_at = None
                    state.indexed_at = _utcnow()
                    state.attempts = 0
                    state.next_retry_at = None
                    state.last_error = None
                    stats["deleted"] = int(stats["deleted"]) + 1
                await session.commit()
            except Exception as exc:
                # 错误信息只记录内部错误码或异常类型，避免日志泄露服务地址和密钥。
                state.status = "failed"
                state.attempts += 1
                delay_seconds = min(3600, 30 * (2 ** min(state.attempts - 1, 7)))
                state.next_retry_at = _utcnow() + timedelta(seconds=delay_seconds)
                state.last_error = (
                    str(exc)[:100] if isinstance(exc, VectorStoreError) else type(exc).__name__[:100]
                )
                stats["failed"] = int(stats["failed"]) + 1
                logger.warning(
                    "News vector indexing failed: news_id=%s error=%s",
                    news_id,
                    state.last_error,
                )
                await session.commit()
        return stats
    finally:
        if owns_session:
            await session.close()


async def mark_vector_index_for_rebuild() -> int:
    """将现有索引状态标成待处理；未建过索引的新闻由周期对账自动发现。"""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(NewsVectorIndexState).values(
                status="pending",
                attempts=0,
                next_retry_at=None,
                last_error=None,
            )
        )
        await db.commit()
        return max(0, int(result.rowcount or 0))


async def vector_index_status() -> dict[str, Any]:
    """返回管理端查看重建进度所需的索引状态统计。"""
    from sqlalchemy import func

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(NewsVectorIndexState.status, func.count()).group_by(NewsVectorIndexState.status)
        )
        return {status: count for status, count in result.all()}
