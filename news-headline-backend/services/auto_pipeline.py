"""定时 RSS 采集、规则门禁与自动审核发布流程。"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text

from agent.review import review_news_content
from cache.news_cache import invalidate_news_caches
from config.cache_conf import redis_client
from config.db_confing import AsyncSessionLocal
from config.settings import get_settings
from models.news import AutoPublishCategory, AutoReviewLog, Category, News, NewsFeedSource
from models.operations import CollectionJob, SourceQualityScore
from services.news_collection import FeedCollectionError, collect_feed
from services.orchestration import RETRYABLE_COLLECTION_ERRORS, retry_delay_seconds, serialize_job_result
from services.source_quality import evaluate_source_quality
from utils.metrics import metrics

logger = logging.getLogger(__name__)

# 单轮限制处理数量，避免来源激增或模型响应慢时占满 API 进程。
MAX_AUTO_FEEDS_PER_TICK = 5
MAX_AUTO_REVIEWS_PER_TICK = 10
AUTO_REVIEW_ERROR_RETRY_SECONDS = 15 * 60
MAX_COLLECTION_ATTEMPTS = 5
MAX_REVIEW_ATTEMPTS = 5
MIN_AUTO_PUBLISH_CONTENT_CHARS = 120
SOURCE_LOCK_TTL_SECONDS = 180
REVIEW_LOCK_TTL_SECONDS = 180
_RELEASE_LOCK_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def _now_utc_naive() -> datetime:
    """MySQL DATETIME 使用无时区值，统一写入 UTC 时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _eligible_since(created_at: datetime | None, enabled_at: datetime | None) -> bool:
    """只让两个管理员开关都打开之后新采集的稿件进入自动发布流程。"""
    return bool(created_at and enabled_at and created_at >= enabled_at)


def apply_auto_publish_mode(decision: dict[str, Any], mode: str) -> dict[str, Any]:
    """影子模式记录可放行候选，但始终不触碰稿件发布状态。"""
    if decision.get("decision") == "auto_published" and mode == "shadow":
        return {"decision": "shadow_pass", "reasons": ["shadow_mode_no_publication"]}
    return decision


def decide_auto_publication(
    news: News,
    source: NewsFeedSource,
    category_policy: AutoPublishCategory | None,
    review_result: dict[str, Any],
    source_quality: SourceQualityScore | None = None,
) -> dict[str, Any]:
    """模型建议只能参与审核；所有来源、分类和确定性风险条件还需同时通过。"""
    reasons: list[str] = []
    if news.status != "draft":
        reasons.append("article_not_draft")
    if not source.is_active:
        reasons.append("source_inactive")
    if not source.auto_collect_enabled:
        reasons.append("automatic_collection_disabled")
    if not source.trusted_for_auto_publish:
        reasons.append("source_not_trusted")
    if getattr(source, "source_type", "other") not in {"official_original", "independent_media"}:
        reasons.append("source_provenance_unverified")
    if not getattr(source, "publisher_group", None):
        reasons.append("publisher_group_missing")
    if source_quality is not None and source_quality.tier != "high":
        reasons.append("source_quality_below_auto_publish_threshold")
    if not _eligible_since(news.created_at, source.auto_publish_enabled_at):
        reasons.append("article_predates_source_approval")
    if (
        category_policy is None
        or not category_policy.enabled
        or not _eligible_since(news.created_at, category_policy.enabled_at)
    ):
        reasons.append("category_not_allowlisted")
    if len((news.content or "").strip()) < MIN_AUTO_PUBLISH_CONTENT_CHARS:
        reasons.append("article_too_short")
    if not (news.source_url or "").strip():
        reasons.append("missing_source_url")

    suggestions = review_result.get("suggestions") or {}
    findings = review_result.get("findings") or {}
    if review_result.get("ai_status") != "available":
        reasons.append("ai_review_unavailable")
    if suggestions.get("suggested_category_id") != news.category_id:
        reasons.append("category_suggestion_mismatch")
    factual_check = suggestions.get("factual_check") or {}
    if factual_check.get("status") != "supported" or not factual_check.get("evidence_news_ids"):
        reasons.append("insufficient_published_news_evidence")
    claims = factual_check.get("claims") or []
    if not claims or any(claim.get("status") != "supported" for claim in claims):
        reasons.append("claim_evidence_incomplete")
    if any(
        claim.get("importance") == "key"
        and (
            claim.get("independent_source_count", 0) < 2
            or any(not citation.get("provenance_verified") for citation in claim.get("evidence", []))
        )
        for claim in claims
    ):
        reasons.append("key_claim_lacks_independent_sources")
    if findings.get("duplicates"):
        reasons.append("possible_duplicate")
    if findings.get("suspicious_links"):
        reasons.append("suspicious_link")
    for risk_flag in findings.get("sensitive_or_ad_flags") or []:
        if isinstance(risk_flag, str) and risk_flag not in reasons:
            reasons.append(risk_flag)

    return {
        "decision": "auto_published" if not reasons else "manual_review",
        "reasons": reasons,
    }


async def _collect_source(source_id: int) -> str:
    """安全采集一个到期来源；Redis 锁使多副本部署不会重复拉取。"""
    lock_key = f"news:auto-collect:source:{source_id}"
    token = secrets.token_urlsafe(24)
    try:
        acquired = await redis_client.set(
            lock_key, token, nx=True, ex=SOURCE_LOCK_TTL_SECONDS
        )
    except Exception as exc:
        logger.warning(
            "Automatic feed lock unavailable: source_id=%s error_type=%s",
            source_id,
            type(exc).__name__,
        )
        return "lock_error"
    if not acquired:
        return "locked"

    try:
        async with AsyncSessionLocal() as db:
            source = await db.get(NewsFeedSource, source_id)
            now = _now_utc_naive()
            if source is None or not source.is_active or not source.auto_collect_enabled:
                return "skipped"
            if source.last_fetched_at and source.last_fetched_at > now - timedelta(
                minutes=source.auto_collect_interval_minutes
            ) and not source.last_error:
                return "not_due"
            if source.last_error and (
                source.collection_failures >= MAX_COLLECTION_ATTEMPTS
                or source.next_retry_at is None
                or source.next_retry_at > now
            ):
                return "not_due"
            job = CollectionJob(
                source_id=source.id,
                trigger="scheduled",
                status="running",
                attempt_count=source.collection_failures + 1,
                max_attempts=MAX_COLLECTION_ATTEMPTS,
                started_at=now,
            )
            db.add(job)
            await db.flush()
            try:
                result = await collect_feed(db, source)
                job.status = "succeeded"
                job.error_code = None
                job.next_retry_at = None
                job.finished_at = _now_utc_naive()
                job.result_json = serialize_job_result(result)
                await db.commit()
                logger.info(
                    "Automatic feed collected: source_id=%s created=%s skipped=%s",
                    source_id,
                    result["created"],
                    result["skipped"],
                )
                return "success"
            except FeedCollectionError as exc:
                source.last_fetched_at = now
                source.last_error = exc.code[:100]
                source.collection_failures += 1
                retryable = (
                    exc.code in RETRYABLE_COLLECTION_ERRORS
                    and source.collection_failures < MAX_COLLECTION_ATTEMPTS
                )
                source.next_retry_at = (
                    now + timedelta(seconds=retry_delay_seconds(source.collection_failures))
                    if retryable else None
                )
                job.status = "retrying" if retryable else "failed"
                job.error_code = exc.code[:100]
                job.next_retry_at = source.next_retry_at
                job.finished_at = now
                job.result_json = serialize_job_result({"error": exc.code})
                await db.commit()
                logger.warning(
                    "Automatic feed collection failed: source_id=%s error=%s",
                    source_id,
                    exc.code,
                )
                return "failed"
            except Exception:
                await db.rollback()
                source = await db.get(NewsFeedSource, source_id)
                if source is None:
                    return "failed"
                source.last_fetched_at = now
                source.last_error = "internal_error"
                source.collection_failures += 1
                retryable = source.collection_failures < MAX_COLLECTION_ATTEMPTS
                source.next_retry_at = (
                    now + timedelta(seconds=retry_delay_seconds(source.collection_failures))
                    if retryable else None
                )
                job = CollectionJob(
                    source_id=source.id,
                    trigger="scheduled",
                    status="retrying" if retryable else "failed",
                    attempt_count=source.collection_failures,
                    max_attempts=MAX_COLLECTION_ATTEMPTS,
                    next_retry_at=source.next_retry_at,
                    started_at=now,
                    finished_at=now,
                    error_code="internal_error",
                    result_json=serialize_job_result({"error": "internal_error"}),
                )
                db.add(job)
                await db.commit()
                logger.warning(
                    "Automatic feed collection failed: source_id=%s error_type=internal",
                    source_id,
                    exc_info=False,
                )
                return "failed"
    finally:
        try:
            await redis_client.eval(_RELEASE_LOCK_SCRIPT, 1, lock_key, token)
        except Exception as exc:
            logger.warning(
                "Automatic feed lock release failed: source_id=%s error_type=%s",
                source_id,
                type(exc).__name__,
            )


async def _due_source_ids() -> list[int]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT id FROM news_feed_source "
                "WHERE is_active = 1 AND auto_collect_enabled = 1 "
                "AND ((last_error IS NULL AND (last_fetched_at IS NULL OR last_fetched_at <= "
                "TIMESTAMPADD(MINUTE, -auto_collect_interval_minutes, UTC_TIMESTAMP()))) "
                "OR (last_error IS NOT NULL AND collection_failures < :max_attempts "
                "AND next_retry_at IS NOT NULL AND next_retry_at <= UTC_TIMESTAMP())) "
                "ORDER BY last_fetched_at ASC, id ASC LIMIT :limit"
            ),
            {"limit": MAX_AUTO_FEEDS_PER_TICK, "max_attempts": MAX_COLLECTION_ATTEMPTS},
        )
        return [int(value) for value in result.scalars().all()]


async def _pending_review_ids() -> list[int]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT n.id FROM news n "
                "JOIN news_feed_source s ON s.id = n.feed_source_id "
                "JOIN news_auto_publish_categories p ON p.category_id = n.category_id "
                "LEFT JOIN news_auto_review_logs l ON l.news_id = n.id "
                "WHERE n.status = 'draft' AND s.is_active = 1 AND s.auto_collect_enabled = 1 "
                "AND s.trusted_for_auto_publish = 1 AND s.auto_publish_enabled_at IS NOT NULL "
                "AND s.source_type IN ('official_original', 'independent_media') "
                "AND s.publisher_group IS NOT NULL AND s.publisher_group <> '' "
                "AND n.created_at >= s.auto_publish_enabled_at "
                "AND p.enabled = 1 AND n.created_at >= p.enabled_at "
                "AND (l.id IS NULL OR (l.decision = 'review_error' AND l.attempts < :max_attempts AND "
                "l.last_attempt_at <= TIMESTAMPADD(SECOND, -:retry_seconds, UTC_TIMESTAMP()))) "
                "ORDER BY n.created_at ASC, n.id ASC LIMIT :limit"
            ),
            {
                "retry_seconds": AUTO_REVIEW_ERROR_RETRY_SECONDS,
                "max_attempts": MAX_REVIEW_ATTEMPTS,
                "limit": MAX_AUTO_REVIEWS_PER_TICK,
            },
        )
        return [int(value) for value in result.scalars().all()]


async def _save_review_error(news_id: int) -> None:
    """模型/数据库异常时只记错误并保留草稿，不将异常路径自动放行。"""
    async with AsyncSessionLocal() as db:
        news = await db.get(News, news_id)
        if news is None:
            return
        log = await db.scalar(
            select(AutoReviewLog).where(AutoReviewLog.news_id == news_id)
        )
        now = _now_utc_naive()
        if log is None:
            source = await db.get(NewsFeedSource, news.feed_source_id) if news.feed_source_id else None
            category = await db.get(Category, news.category_id)
            log = AutoReviewLog(
                news_id=news.id,
                source_id=source.id if source else None,
                source_name=source.name if source else None,
                category_id=news.category_id,
                category_name=category.name if category else None,
                title_snapshot=news.title,
                decision="review_error",
                reasons_json=json.dumps(["review_execution_error"]),
                attempts=1,
                last_attempt_at=now,
            )
            db.add(log)
        else:
            log.decision = "review_error"
            log.reasons_json = json.dumps(["review_execution_error"])
            log.attempts += 1
            log.last_attempt_at = now
        await db.commit()


async def _review_and_maybe_publish(news_id: int) -> str:
    """审核一篇草稿，并在所有独立发布门禁通过后原子地发布和记审计。"""
    settings = get_settings()
    lock_key = f"news:auto-review:article:{news_id}"
    token = secrets.token_urlsafe(24)
    lock_ttl = max(REVIEW_LOCK_TTL_SECONDS, int(settings.ai_timeout_seconds * 2) + 30)
    try:
        acquired = await redis_client.set(lock_key, token, nx=True, ex=lock_ttl)
    except Exception as exc:
        logger.warning(
            "Automatic review lock unavailable: news_id=%s error_type=%s",
            news_id,
            type(exc).__name__,
        )
        return "lock_error"
    if not acquired:
        return "locked"

    try:
        async with AsyncSessionLocal() as db:
            news = await db.get(News, news_id)
            if news is None or news.status != "draft" or not news.feed_source_id:
                return "skipped"
            source = await db.get(NewsFeedSource, news.feed_source_id)
            category = await db.get(Category, news.category_id)
            category_policy = await db.get(AutoPublishCategory, news.category_id)
            if source is None or category is None:
                return "skipped"

            review_result = await review_news_content(
                db,
                news_id=news.id,
                title=news.title,
                description=news.description,
                content=news.content,
                category_id=news.category_id,
                source_url=news.source_url,
            )
            source_quality = await evaluate_source_quality(db, source)
            decision = decide_auto_publication(
                news, source, category_policy, review_result, source_quality
            )
            decision = apply_auto_publish_mode(decision, settings.auto_publish_mode)
            now = _now_utc_naive()
            log = await db.scalar(
                select(AutoReviewLog).where(AutoReviewLog.news_id == news.id)
            )
            if log is None:
                log = AutoReviewLog(
                    news_id=news.id,
                    attempts=1,
                    source_id=source.id,
                    source_name=source.name,
                    category_id=category.id,
                    category_name=category.name,
                    title_snapshot=news.title,
                    last_attempt_at=now,
                )
                db.add(log)
            else:
                log.attempts += 1
                log.source_id = source.id
                log.source_name = source.name
                log.category_id = category.id
                log.category_name = category.name
                log.title_snapshot = news.title
                log.last_attempt_at = now
            log.decision = decision["decision"]
            log.reasons_json = json.dumps(decision["reasons"], ensure_ascii=False)
            log.review_result_json = json.dumps(review_result, ensure_ascii=False)[:60_000]
            if decision["decision"] == "auto_published":
                # 发布操作与审计日志在同一事务提交，审核建议本身不会直接改稿。
                news.status = "published"
                news.reviewed_by = None
                news.reviewed_at = now
                log.published_at = now
            await db.commit()

            if decision["decision"] == "auto_published":
                try:
                    await invalidate_news_caches()
                except Exception as exc:
                    logger.warning(
                        "Published news cache invalidation failed: news_id=%s error_type=%s",
                        news_id,
                        type(exc).__name__,
                    )
                logger.info("News auto-published: news_id=%s source_id=%s", news.id, source.id)
                return "auto_published"
            return decision["decision"]
    except Exception:
        logger.warning("Automatic editorial review failed: news_id=%s", news_id, exc_info=False)
        try:
            await _save_review_error(news_id)
        except Exception as exc:
            logger.error(
                "Automatic editorial error could not be recorded: news_id=%s error_type=%s",
                news_id,
                type(exc).__name__,
            )
        return "review_error"
    finally:
        try:
            await redis_client.eval(_RELEASE_LOCK_SCRIPT, 1, lock_key, token)
        except Exception as exc:
            logger.warning(
                "Automatic review lock release failed: news_id=%s error_type=%s",
                news_id,
                type(exc).__name__,
            )


async def run_auto_pipeline_tick() -> dict[str, int]:
    """运行一轮到期采集和待审核稿件处理；每个副本可安全调用。"""
    stats = {
        "feeds_due": 0,
        "feeds_succeeded": 0,
        "feeds_failed": 0,
        "reviews_processed": 0,
        "auto_published": 0,
        "shadow_passes": 0,
        "manual_review": 0,
        "review_errors": 0,
    }
    source_ids = await _due_source_ids()
    stats["feeds_due"] = len(source_ids)
    for source_id in source_ids:
        result = await _collect_source(source_id)
        if result == "success":
            stats["feeds_succeeded"] += 1
        elif result == "failed":
            stats["feeds_failed"] += 1

    news_ids = await _pending_review_ids()
    for news_id in news_ids:
        result = await _review_and_maybe_publish(news_id)
        if result in {"auto_published", "shadow_pass", "manual_review", "review_error"}:
            stats["reviews_processed"] += 1
        if result == "auto_published":
            stats["auto_published"] += 1
        elif result == "shadow_pass":
            stats["shadow_passes"] += 1
        elif result == "manual_review":
            stats["manual_review"] += 1
        elif result == "review_error":
            stats["review_errors"] += 1
    # Celery Worker 与 API 是独立进程，通过 Redis 汇总流水线计数器。
    await metrics.inc_shared("news_pipeline_runs_total", status="completed")
    for metric_name, value in stats.items():
        if value:
            await metrics.inc_shared(
                "news_pipeline_items_total", value=value, kind=metric_name
            )
    return stats


async def auto_pipeline_scheduler() -> None:
    """FastAPI 生命周期中的后台轮询；每轮错误被隔离，不会终止服务。"""
    settings = get_settings()
    if not settings.auto_pipeline_enabled:
        logger.info("Automatic news pipeline is disabled")
        return
    logger.info(
        "Automatic news pipeline started: interval_seconds=%s",
        settings.auto_pipeline_interval_seconds,
    )
    while True:
        try:
            stats = await run_auto_pipeline_tick()
            if stats["feeds_due"] or stats["reviews_processed"]:
                logger.info("Automatic news pipeline tick completed: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Automatic news pipeline tick failed", exc_info=False)
        await asyncio.sleep(settings.auto_pipeline_interval_seconds)
