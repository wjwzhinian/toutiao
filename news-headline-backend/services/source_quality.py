"""新闻来源质量评分。评分是可解释规则，模型不能直接改变结果。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.news import AutoReviewLog, News, NewsFeedSource, NewsReviewFeedback
from models.operations import CollectionJob, SourceQualityScore


def quality_tier(
    score: float,
    feedback_samples: int = 0,
    has_error: bool = False,
    tested: bool = True,
) -> str:
    """将分数映射为自动发布门禁使用的等级。"""
    if score >= 80 and feedback_samples >= 3 and not has_error and tested:
        return "high"
    if score >= 60:
        return "medium"
    return "low"


def calculate_quality_score(
    *,
    source_type: str,
    is_active: bool,
    tested: bool,
    has_error: bool,
    total_articles: int,
    unique_articles: int,
    feedback_samples: int,
    accurate_feedback: int,
    safe_feedback: int = 0,
) -> tuple[float, dict[str, Any]]:
    """根据来源、稳定性、重复率和人工反馈计算 0 到 100 分。"""
    provenance = 30 if source_type == "official_original" else 24 if source_type == "independent_media" else 10
    stability = 20 if is_active and tested and not has_error else 8 if is_active and tested else 0
    uniqueness = 25 if total_articles == 0 else round(25 * min(1, unique_articles / total_articles), 2)
    feedback = (
        25
        if feedback_samples == 0
        else round(
            15 * accurate_feedback / feedback_samples
            + 10 * safe_feedback / feedback_samples,
            2,
        )
    )
    score = round(min(100.0, provenance + stability + uniqueness + feedback), 2)
    return score, {
        "provenance": provenance,
        "stability": stability,
        "uniqueness": uniqueness,
        "human_feedback": feedback,
        "total_articles": total_articles,
        "unique_articles": unique_articles,
        "feedback_samples": feedback_samples,
    }


async def evaluate_source_quality(db: AsyncSession, source: NewsFeedSource) -> SourceQualityScore:
    """计算并保存单个来源的最新评分快照。"""
    article_result = await db.execute(
        select(func.count(News.id), func.count(func.distinct(News.content_hash))).where(
            News.feed_source_id == source.id
        )
    )
    total_articles, unique_articles = (int(value or 0) for value in article_result.one())
    collection_result = await db.execute(
        select(CollectionJob.result_json).where(CollectionJob.source_id == source.id)
    )
    fetched_items = skipped_items = 0
    for raw_result in collection_result.scalars().all():
        try:
            metrics = json.loads(raw_result or "{}")
        except (TypeError, ValueError):
            continue
        fetched_items += int(metrics.get("created", 0) or 0) + int(metrics.get("skipped", 0) or 0)
        skipped_items += int(metrics.get("skipped", 0) or 0)
    if fetched_items:
        total_articles = fetched_items
        unique_articles = max(0, fetched_items - skipped_items)
    feedback_result = await db.execute(
        select(
            func.count(NewsReviewFeedback.id),
            func.sum(case((NewsReviewFeedback.evidence_accurate.is_(True), 1), else_=0)),
            func.sum(case((NewsReviewFeedback.safe_to_publish.is_(True), 1), else_=0)),
        )
        .select_from(NewsReviewFeedback)
        .join(AutoReviewLog, AutoReviewLog.id == NewsReviewFeedback.review_log_id)
        .where(AutoReviewLog.source_id == source.id)
    )
    feedback_samples, accurate_feedback, safe_feedback = (int(value or 0) for value in feedback_result.one())
    score, metrics = calculate_quality_score(
        source_type=source.source_type,
        is_active=source.is_active,
        tested=source.last_tested_at is not None and not source.last_test_error,
        has_error=bool(source.last_error),
        total_articles=total_articles,
        unique_articles=unique_articles,
        feedback_samples=feedback_samples,
        accurate_feedback=accurate_feedback,
        safe_feedback=safe_feedback,
    )
    tested = source.last_tested_at is not None and not source.last_test_error
    snapshot = await db.get(SourceQualityScore, source.id)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if snapshot is None:
        snapshot = SourceQualityScore(
            source_id=source.id,
            score=score,
            tier=quality_tier(score, feedback_samples, bool(source.last_error), tested),
            metrics_json="{}",
            evaluated_at=now,
        )
        db.add(snapshot)
    snapshot.score = score
    snapshot.tier = quality_tier(score, feedback_samples, bool(source.last_error), tested)
    snapshot.metrics_json = json.dumps(metrics, ensure_ascii=False)
    snapshot.evaluated_at = now
    return snapshot
