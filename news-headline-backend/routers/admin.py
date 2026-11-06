import json
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from common.result import Result
from agent.actions import AgentActionError
from agent.operations import interpret_admin_command
from agent.review import review_news_content
from cache.news_cache import invalidate_news_caches
from config.cache_conf import redis_client
from config.db_confing import get_db
from config.settings import get_settings
from crud import users
from models.users import AuthRefreshSession, User
from models.news import AutoPublishCategory, AutoReviewLog, Category, News, NewsFeedSource, NewsReviewFeedback
from models.operations import CollectionJob, NewsEvent, NewsEventAlert, NewsEventItem, SourceQualityScore
from schemas.admin import AdminOperationRequest, RoleUpdateRequest, UserActiveUpdateRequest
from schemas.news_management import (
    FeedSourceCreate,
    FeedSourceUpdate,
    NewsContentCreate,
    NewsContentUpdate,
    NewsReviewAssistRequest,
    NewsStatusUpdate,
    AutoPublishCategoryUpdate,
    AutoPublishWithdrawRequest,
    NewsReviewFeedbackCreate,
)
from services.news_collection import FeedCollectionError, _fetch_feed, collect_feed, parse_feed_xml
from services.event_tracking import track_news_event
from services.evidence import retrieve_evidence
from services.orchestration import fail_collection_job, finish_collection_job, job_payload, start_collection_job
from services.source_quality import evaluate_source_quality
from utils.permissions import permissions_for_role, require_permission
from utils.audit import record_audit_event


router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = logging.getLogger(__name__)


def _feed_payload(source: NewsFeedSource) -> dict:
    return {
        "id": source.id,
        "name": source.name,
        "url": source.url,
        "categoryId": source.category_id,
        "sourceType": source.source_type,
        "publisherGroup": source.publisher_group,
        "isActive": source.is_active,
        "autoCollectEnabled": source.auto_collect_enabled,
        "autoCollectIntervalMinutes": source.auto_collect_interval_minutes,
        "trustedForAutoPublish": source.trusted_for_auto_publish,
        "lastTestedAt": source.last_tested_at,
        "lastTestError": source.last_test_error,
        "lastFetchedAt": source.last_fetched_at,
        "lastError": source.last_error,
        "collectionFailures": source.collection_failures,
        "nextRetryAt": source.next_retry_at,
        "createdAt": source.created_at,
        "updatedAt": source.updated_at,
    }


def _news_payload(item: News) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "description": item.description,
        "content": item.content,
        "image": item.image,
        "author": item.author,
        "categoryId": item.category_id,
        "views": item.views,
        "publishTime": item.publish_time,
        "status": item.status,
        "feedSourceId": item.feed_source_id,
        "sourceUrl": item.source_url,
        "externalId": item.external_id,
        "reviewedBy": item.reviewed_by,
        "reviewedAt": item.reviewed_at,
        "createdAt": item.created_at,
        "updatedAt": item.updated_at,
    }


async def _category_exists(db: AsyncSession, category_id: int) -> bool:
    return await db.get(Category, category_id) is not None


@router.get("/access")
async def access(user: User = Depends(require_permission("users:manage_roles"))):
    return Result.success(
        {
            "role": user.role,
            "permissions": permissions_for_role(user.role),
        }
    )


@router.get("/audit-logs")
async def list_audit_logs(
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_permission("users:manage_roles")),
    db: AsyncSession = Depends(get_db),
):
    """查询最近审计记录，便于排查权限和自动化操作。"""
    rows = await db.execute(
        text(
            "SELECT id, actor_id, action, resource_type, resource_id, outcome, "
            "details_json, request_id, created_at FROM audit_logs "
            "ORDER BY id DESC LIMIT :limit"
        ),
        {"limit": limit},
    )
    return Result.success([dict(row) for row in rows.mappings().all()])


@router.post("/operations/command")
async def admin_operation_command(
    request: AdminOperationRequest,
    actor: User = Depends(require_permission("operations:manage")),
    db: AsyncSession = Depends(get_db),
):
    """执行受限管理员命令；变更类命令只创建待确认 action。"""
    try:
        result = await interpret_admin_command(actor.id, request.command)
    except AgentActionError as exc:
        status_code = 422 if exc.code in {"invalid_command", "category_required", "unsupported_command"} else exc.status_code
        raise HTTPException(status_code=status_code, detail=exc.code) from exc
    await record_audit_event(
        db, actor_id=actor.id, action="admin.operation.command",
        resource_type="agent_operation", outcome=result.get("status", "success"),
        details={"intent": result.get("intent")},
    )
    return Result.success(result)


@router.patch("/users/{user_id}/role")
async def update_role(
    user_id: int,
    request: RoleUpdateRequest,
    actor: User = Depends(require_permission("users:manage_roles")),
    db: AsyncSession = Depends(get_db),
):
    user = await users.update_user_role(db, user_id, request.role)
    await record_audit_event(
        db, actor_id=actor.id, action="user.role.update", resource_type="user",
        resource_id=str(user.id), details={"role": user.role},
    )
    logger.info(
        "User role changed: actor_id=%s target_id=%s role=%s",
        actor.id,
        user.id,
        user.role,
    )
    return Result.success(
        {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "permissions": permissions_for_role(user.role),
        }
    )


@router.patch("/users/{user_id}/active")
async def update_user_active(
    user_id: int,
    request: UserActiveUpdateRequest,
    actor: User = Depends(require_permission("users:manage_roles")),
    db: AsyncSession = Depends(get_db),
):
    """管理员可禁用或重新启用账号；禁用后现有访问和刷新会话立即失效。"""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == actor.id and not request.is_active:
        raise HTTPException(status_code=409, detail="You cannot disable your own account")
    if user.role == "admin" and user.is_active and not request.is_active:
        active_admins = await db.execute(
            select(User.id).where(User.role == "admin", User.is_active.is_(True)).with_for_update()
        )
        if len(active_admins.scalars().all()) <= 1:
            raise HTTPException(status_code=409, detail="At least one active administrator must remain")
    user.is_active = request.is_active
    if not user.is_active:
        await db.execute(
            update(AuthRefreshSession)
            .where(AuthRefreshSession.user_id == user.id, AuthRefreshSession.revoked_at.is_(None))
            .values(revoked_at=datetime.utcnow())
        )
    await record_audit_event(
        db,
        actor_id=actor.id,
        action="user.active.update",
        resource_type="user",
        resource_id=str(user.id),
        details={"is_active": user.is_active},
    )
    return Result.success({"id": user.id, "isActive": user.is_active})


@router.get("/feeds")
async def list_feed_sources(
    _: User = Depends(require_permission("feeds:manage")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(NewsFeedSource).order_by(NewsFeedSource.id.desc()))
    return Result.success([_feed_payload(item) for item in result.scalars().all()])


@router.post("/feeds")
async def create_feed_source(
    request: FeedSourceCreate,
    actor: User = Depends(require_permission("feeds:manage")),
    db: AsyncSession = Depends(get_db),
):
    if not await _category_exists(db, request.category_id):
        raise HTTPException(status_code=404, detail="News category not found")
    source = NewsFeedSource(
        name=request.name,
        url=request.url,
        category_id=request.category_id,
        source_type=request.source_type,
        publisher_group=request.publisher_group,
        # 新来源必须先测试 RSS，再由管理员单独启用，避免跳过来源审核。
        is_active=False,
        auto_collect_enabled=request.auto_collect_enabled,
        auto_collect_interval_minutes=request.auto_collect_interval_minutes,
        # 新来源创建时不设可信标记，必须先测试并启用，再由管理员单独批准。
        trusted_for_auto_publish=False,
    )
    db.add(source)
    await db.flush()
    logger.info("News feed source created: actor_id=%s source_id=%s", actor.id, source.id)
    return Result.success(_feed_payload(source), "Feed source created")


@router.patch("/feeds/{source_id}")
async def update_feed_source(
    source_id: int,
    request: FeedSourceUpdate,
    actor: User = Depends(require_permission("feeds:manage")),
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(NewsFeedSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Feed source not found")
    values = request.model_dump(exclude_unset=True)
    if "category_id" in values and not await _category_exists(db, values["category_id"]):
        raise HTTPException(status_code=404, detail="News category not found")
    changed_identity = (
        values.get("url", source.url) != source.url
        or values.get("category_id", source.category_id) != source.category_id
        or values.get("source_type", getattr(source, "source_type", "other")) != getattr(source, "source_type", "other")
        or values.get("publisher_group", getattr(source, "publisher_group", None)) != getattr(source, "publisher_group", None)
    )
    url_changed = values.get("url", source.url) != source.url
    if url_changed and values.get("is_active") is True:
        raise HTTPException(
            status_code=409,
            detail="修改 RSS 地址后请先测试新地址，再单独启用来源",
        )
    if values.get("is_active") is True and not source.is_active:
        if url_changed or source.last_tested_at is None or source.last_test_error:
            raise HTTPException(
                status_code=409,
                detail="请先测试当前 RSS 地址并确认测试成功，再启用来源",
            )
    for field, value in values.items():
        if field != "trusted_for_auto_publish":
            setattr(source, field, value)
    if changed_identity or values.get("is_active") is False:
        # 来源 URL、分类或启用状态变化后撤销可信审批，避免旧审批沿用到新源。
        source.trusted_for_auto_publish = False
        source.auto_publish_enabled_at = None
    if url_changed:
        # 修改地址后必须重新测试，避免启用未经验证的新目标。
        source.is_active = False
        source.last_tested_at = None
        source.last_test_error = None
    elif "trusted_for_auto_publish" in values:
        if values["trusted_for_auto_publish"] and not source.is_active:
            raise HTTPException(
                status_code=409,
                detail="启用来源并确认 RSS 内容后，才能标记为可信自动发布来源",
            )
        if values["trusted_for_auto_publish"] and changed_identity:
            raise HTTPException(status_code=409, detail="来源谱系变更后请先保存，再单独重新授权自动发布")
        if values["trusted_for_auto_publish"] and (
            source.source_type not in {"official_original", "independent_media"}
            or not source.publisher_group
        ):
            raise HTTPException(status_code=409, detail="自动发布要求登记为官方原始来源或独立媒体，并填写稿源分组")
        source.trusted_for_auto_publish = values["trusted_for_auto_publish"]
        source.auto_publish_enabled_at = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            if source.trusted_for_auto_publish
            else None
        )
    logger.info("News feed source updated: actor_id=%s source_id=%s", actor.id, source.id)
    return Result.success(_feed_payload(source), "Feed source updated")


@router.delete("/feeds/{source_id}")
async def delete_feed_source(
    source_id: int,
    actor: User = Depends(require_permission("feeds:manage")),
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(NewsFeedSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Feed source not found")
    await db.delete(source)
    logger.info("News feed source deleted: actor_id=%s source_id=%s", actor.id, source_id)
    return Result.success({"id": source_id}, "Feed source deleted")


@router.post("/feeds/{source_id}/collect")
async def collect_feed_source(
    source_id: int,
    actor: User = Depends(require_permission("feeds:manage")),
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(NewsFeedSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Feed source not found")

    lock_key = f"news:feed:collecting:{source_id}"
    lock_token = secrets.token_urlsafe(24)
    try:
        acquired = await redis_client.set(lock_key, lock_token, nx=True, ex=180)
    except Exception as exc:
        logger.warning("News feed lock unavailable: error_type=%s", type(exc).__name__)
        return Result.error("Collection service temporarily unavailable", 503)
    if not acquired:
        return Result.error("This feed is already being collected", 409)

    try:
        job = start_collection_job(db, source, trigger="manual", requested_by=actor.id)
        await db.flush()
        result = await collect_feed(db, source)
        finish_collection_job(job, source, result)
        logger.info(
            "News feed collected: actor_id=%s source_id=%s created=%s skipped=%s",
            actor.id,
            source.id,
            result["created"],
            result["skipped"],
        )
        return Result.success({"source": _feed_payload(source), **result}, "Feed collection completed")
    except FeedCollectionError as exc:
        job = locals().get("job")
        if job is not None:
            fail_collection_job(job, source, exc.code)
        else:
            source.last_fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
            source.last_error = exc.code[:100]
        logger.warning(
            "News feed collection failed: actor_id=%s source_id=%s error=%s",
            actor.id,
            source.id,
            exc.code,
        )
        return Result.error(
            "Feed collection failed",
            502,
            {"status": "failed", "error": exc.code, "source": _feed_payload(source)},
        )
    finally:
        try:
            await redis_client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                lock_key,
                lock_token,
            )
        except Exception as exc:
            logger.warning("News feed lock release failed: error_type=%s", type(exc).__name__)


@router.post("/feeds/{source_id}/test")
async def test_feed_source(
    source_id: int,
    actor: User = Depends(require_permission("feeds:manage")),
    db: AsyncSession = Depends(get_db),
):
    """管理员在启用定时采集前验证 Feed；测试只读取，不保存新闻。"""
    source = await db.get(NewsFeedSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Feed source not found")
    try:
        records = parse_feed_xml(await _fetch_feed(source.url))
    except FeedCollectionError as exc:
        source.last_tested_at = datetime.now(timezone.utc).replace(tzinfo=None)
        source.last_test_error = exc.code[:100]
        await db.commit()
        logger.warning(
            "News feed test failed: actor_id=%s source_id=%s error=%s",
            actor.id,
            source.id,
            exc.code,
        )
        return Result.error("Feed test failed", 502, {"error": exc.code})
    source.last_tested_at = datetime.now(timezone.utc).replace(tzinfo=None)
    source.last_test_error = None
    await db.commit()
    logger.info("News feed tested: actor_id=%s source_id=%s", actor.id, source.id)
    return Result.success(
        {"entryCount": len(records), "sampleTitles": [item["title"] for item in records[:5]]},
        "Feed test completed",
    )


@router.get("/evidence/search")
async def search_editorial_evidence(
    claim: str = Query(..., min_length=2, max_length=500),
    category_id: int | None = Query(default=None, alias="categoryId", gt=0),
    limit: int = Query(default=5, ge=1, le=10),
    _: User = Depends(require_permission("news:review")),
    db: AsyncSession = Depends(get_db),
):
    """检索站内已发布报道，返回可核对的引用和来源信息。"""
    evidence = await retrieve_evidence(db, claim, category_id=category_id, limit=limit)
    return Result.success({"claim": claim, "items": evidence, "count": len(evidence)})


@router.get("/vector-index")
async def get_vector_index_status(
    _: User = Depends(require_permission("feeds:manage")),
):
    """查看向量检索开关、版本和异步索引队列状态。"""
    from services.vector_index import vector_index_status

    settings = get_settings()
    return Result.success(
        {
            "enabled": settings.qdrant_enabled,
            "collection": settings.qdrant_collection,
            "embeddingModel": settings.embedding_model or None,
            "embeddingVersion": settings.embedding_version,
            "counts": await vector_index_status(),
        }
    )


@router.post("/vector-index/rebuild")
async def request_vector_index_rebuild(
    actor: User = Depends(require_permission("feeds:manage")),
):
    """将全量向量重建交给 Celery Worker，接口只负责投递任务。"""
    settings = get_settings()
    if not settings.qdrant_enabled:
        raise HTTPException(status_code=409, detail="Qdrant vector search is disabled")
    try:
        from celery_app import rebuild_vector_index

        task = rebuild_vector_index.delay()
    except Exception as exc:
        logger.warning(
            "Vector index rebuild enqueue failed: actor_id=%s error_type=%s",
            actor.id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="Vector index worker is unavailable") from None
    logger.info("Vector index rebuild queued: actor_id=%s task_id=%s", actor.id, task.id)
    return Result.success({"taskId": task.id, "status": "queued"}, "Vector index rebuild queued")


@router.get("/feeds/{source_id}/quality")
async def get_source_quality(
    source_id: int,
    _: User = Depends(require_permission("feeds:manage")),
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(NewsFeedSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Feed source not found")
    snapshot = await db.get(SourceQualityScore, source_id)
    if snapshot is None:
        snapshot = await evaluate_source_quality(db, source)
        await db.commit()
    try:
        metrics = json.loads(snapshot.metrics_json)
    except (TypeError, ValueError):
        metrics = {}
    return Result.success({
        "sourceId": source_id,
        "score": snapshot.score,
        "tier": snapshot.tier,
        "metrics": metrics,
        "evaluatedAt": snapshot.evaluated_at,
    })


@router.post("/feeds/{source_id}/quality/recalculate")
async def recalculate_source_quality(
    source_id: int,
    actor: User = Depends(require_permission("feeds:manage")),
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(NewsFeedSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Feed source not found")
    snapshot = await evaluate_source_quality(db, source)
    await db.commit()
    logger.info("Feed quality recalculated: actor_id=%s source_id=%s score=%s", actor.id, source_id, snapshot.score)
    return Result.success({"sourceId": source_id, "score": snapshot.score, "tier": snapshot.tier}, "Source quality recalculated")


@router.get("/orchestration/jobs")
async def list_collection_jobs(
    status: str | None = Query(default=None, pattern="^(queued|running|succeeded|retrying|failed)$"),
    limit: int = Query(default=50, ge=1, le=100),
    _: User = Depends(require_permission("feeds:manage")),
    db: AsyncSession = Depends(get_db),
):
    query = select(CollectionJob).order_by(CollectionJob.created_at.desc(), CollectionJob.id.desc()).limit(limit)
    if status:
        query = query.where(CollectionJob.status == status)
    result = await db.execute(query)
    return Result.success([job_payload(item) for item in result.scalars().all()])


@router.get("/events")
async def list_news_events(
    needs_review: bool | None = Query(default=None, alias="needsReview"),
    limit: int = Query(default=50, ge=1, le=100),
    _: User = Depends(require_permission("news:review")),
    db: AsyncSession = Depends(get_db),
):
    query = select(NewsEvent).order_by(NewsEvent.last_seen_at.desc(), NewsEvent.id.desc()).limit(limit)
    if needs_review is not None:
        query = query.where(NewsEvent.needs_review == needs_review)
    result = await db.execute(query)
    return Result.success([
        {
            "id": item.id,
            "eventKey": item.event_key,
            "canonicalTitle": item.canonical_title,
            "latestNewsId": item.latest_news_id,
            "firstSeenAt": item.first_seen_at,
            "lastSeenAt": item.last_seen_at,
            "updateCount": item.update_count,
            "needsReview": item.needs_review,
            "correctionSummary": item.correction_summary,
        }
        for item in result.scalars().all()
    ])


@router.get("/events/alerts")
async def list_event_alerts(
    status: str = Query(default="pending", pattern="^(pending|resolved)$"),
    limit: int = Query(default=50, ge=1, le=100),
    _: User = Depends(require_permission("news:review")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(NewsEventAlert).where(NewsEventAlert.status == status)
        .order_by(NewsEventAlert.created_at.desc(), NewsEventAlert.id.desc()).limit(limit)
    )
    return Result.success([
        {
            "id": item.id,
            "eventId": item.event_id,
            "newsId": item.news_id,
            "type": item.alert_type,
            "status": item.status,
            "summary": item.summary,
            "evidence": json.loads(item.evidence_json) if item.evidence_json else {},
            "createdAt": item.created_at,
            "resolvedAt": item.resolved_at,
        }
        for item in result.scalars().all()
    ])


@router.post("/events/alerts/{alert_id}/resolve")
async def resolve_event_alert(
    alert_id: int,
    actor: User = Depends(require_permission("news:review")),
    db: AsyncSession = Depends(get_db),
):
    alert = await db.get(NewsEventAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Event alert not found")
    if alert.status == "resolved":
        raise HTTPException(status_code=409, detail="Event alert already resolved")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    alert.status = "resolved"
    alert.resolved_by = actor.id
    alert.resolved_at = now
    event = await db.get(NewsEvent, alert.event_id)
    if event:
        pending = await db.scalar(select(NewsEventAlert.id).where(
            NewsEventAlert.event_id == event.id,
            NewsEventAlert.status == "pending",
            NewsEventAlert.id != alert.id,
        ))
        event.needs_review = pending is not None
    await db.commit()
    return Result.success({"id": alert.id, "status": alert.status, "resolvedAt": now}, "Event alert resolved")


@router.get("/events/{event_id}")
async def get_news_event_detail(
    event_id: int,
    _: User = Depends(require_permission("news:review")),
    db: AsyncSession = Depends(get_db),
):
    event = await db.get(NewsEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="News event not found")
    result = await db.execute(
        select(NewsEventItem, News, NewsFeedSource)
        .join(News, News.id == NewsEventItem.news_id)
        .outerjoin(NewsFeedSource, NewsFeedSource.id == News.feed_source_id)
        .where(NewsEventItem.event_id == event_id)
        .order_by(News.publish_time.desc(), News.id.desc())
        .limit(50)
    )
    articles = [
        {
            "newsId": news.id,
            "title": news.title,
            "status": news.status,
            "sourceName": source.name if source else news.author,
            "sourceType": source.source_type if source else "other",
            "publisherGroup": source.publisher_group if source else None,
            "sourceUrl": news.source_url,
            "publishTime": news.publish_time,
            "relationType": link.relation_type,
            "similarity": link.similarity,
        }
        for link, news, source in result.all()
    ]
    return Result.success({
        "id": event.id,
        "canonicalTitle": event.canonical_title,
        "needsReview": event.needs_review,
        "updateCount": event.update_count,
        "articles": articles,
    })


@router.post("/events/{event_id}/revision-suggestion")
async def suggest_event_revision(
    event_id: int,
    actor: User = Depends(require_permission("news:review")),
    db: AsyncSession = Depends(get_db),
):
    """结合事件内已发布报道，为新进展稿生成只读的编辑建议。"""
    event = await db.get(NewsEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="News event not found")
    if not event.latest_news_id:
        raise HTTPException(status_code=404, detail="Event has no latest article")
    latest = await db.get(News, event.latest_news_id)
    if latest is None:
        raise HTTPException(status_code=404, detail="Latest event article not found")
    related_result = await db.execute(
        select(News.id, News.title, News.description, News.publish_time)
        .join(NewsEventItem, NewsEventItem.news_id == News.id)
        .where(NewsEventItem.event_id == event.id, News.status == "published", News.id != latest.id)
        .order_by(News.publish_time.desc(), News.id.desc())
        .limit(5)
    )
    related = [
        {"newsId": row.id, "title": row.title, "summary": row.description, "publishTime": row.publish_time}
        for row in related_result.all()
    ]
    suggestion = await review_news_content(
        db,
        news_id=latest.id,
        title=latest.title,
        description=latest.description,
        content=latest.content,
        category_id=latest.category_id,
        source_url=latest.source_url,
    )
    logger.info("Event revision suggestion generated: actor_id=%s event_id=%s", actor.id, event_id)
    return Result.success({
        "eventId": event.id,
        "latestNewsId": latest.id,
        "relatedPublishedNews": related,
        "suggestion": suggestion,
        "requiresEditorApproval": True,
    }, "Revision suggestion generated")


@router.get("/content")
async def list_managed_news(
    status: str | None = Query(default=None, pattern="^(draft|published|archived)$"),
    q: str | None = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100, alias="pageSize"),
    _: User = Depends(require_permission("news:read")),
    db: AsyncSession = Depends(get_db),
):
    filters = []
    if status:
        filters.append(News.status == status)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(or_(News.title.ilike(pattern), News.description.ilike(pattern)))
    count_result = await db.execute(select(func.count(News.id)).where(*filters))
    total = count_result.scalar_one()
    result = await db.execute(
        select(News)
        .where(*filters)
        .order_by(News.created_at.desc(), News.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return Result.success(
        {
            "items": [_news_payload(item) for item in result.scalars().all()],
            "total": total,
            "page": page,
            "pageSize": page_size,
        }
    )


@router.get("/content/{news_id}")
async def get_managed_news(
    news_id: int,
    _: User = Depends(require_permission("news:read")),
    db: AsyncSession = Depends(get_db),
):
    item = await db.get(News, news_id)
    if item is None:
        raise HTTPException(status_code=404, detail="News not found")
    return Result.success(_news_payload(item))


@router.post("/content/review-assist")
async def review_assist(
    request: NewsReviewAssistRequest,
    actor: User = Depends(require_permission("news:edit")),
    db: AsyncSession = Depends(get_db),
):
    # 审核建议接口要求新闻管理权限；服务只返回建议，不会替编辑保存或发布稿件。
    try:
        result = await review_news_content(
            db,
            news_id=request.news_id,
            title=request.title,
            description=request.description,
            content=request.content,
            category_id=request.category_id,
            source_url=request.source_url,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="News not found") from exc
    logger.info(
        "Editorial assist requested: actor_id=%s news_id=%s ai_status=%s",
        actor.id,
        request.news_id,
        result["ai_status"],
    )
    return Result.success(result, "Editorial suggestions generated")


@router.get("/auto-publish/categories")
async def list_auto_publish_categories(
    _: User = Depends(require_permission("news:manage")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Category, AutoPublishCategory).outerjoin(
            AutoPublishCategory, AutoPublishCategory.category_id == Category.id
        ).order_by(Category.sort_order, Category.id)
    )
    return Result.success(
        [
            {
                "id": category.id,
                "name": category.name,
                "autoPublishEnabled": bool(policy and policy.enabled),
                "enabledAt": policy.enabled_at if policy and policy.enabled else None,
            }
            for category, policy in result.all()
        ]
    )


@router.put("/auto-publish/categories/{category_id}")
async def update_auto_publish_category(
    category_id: int,
    request: AutoPublishCategoryUpdate,
    actor: User = Depends(require_permission("news:manage")),
    db: AsyncSession = Depends(get_db),
):
    category = await db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="News category not found")
    policy = await db.get(AutoPublishCategory, category_id)
    if policy is None:
        if request.enabled:
            policy = AutoPublishCategory(
                category_id=category_id,
                enabled=True,
                enabled_at=datetime.now(timezone.utc).replace(tzinfo=None),
                updated_by=actor.id,
            )
            db.add(policy)
    else:
        if request.enabled and not policy.enabled:
            # 重新加入白名单后，只处理重新启用时间之后收集的新稿件。
            policy.enabled_at = datetime.now(timezone.utc).replace(tzinfo=None)
        policy.enabled = request.enabled
        policy.updated_by = actor.id
    await db.commit()
    logger.info(
        "Automatic publish category policy changed: actor_id=%s category_id=%s enabled=%s",
        actor.id,
        category_id,
        request.enabled,
    )
    return Result.success(
        {"id": category.id, "name": category.name, "autoPublishEnabled": request.enabled}
    )


@router.get("/auto-publish/logs")
async def list_auto_publish_logs(
    limit: int = Query(default=50, ge=1, le=100),
    actor: User = Depends(require_permission("news:review")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AutoReviewLog, News.status, News.source_url, NewsReviewFeedback)
        .outerjoin(News, News.id == AutoReviewLog.news_id)
        .outerjoin(
            NewsReviewFeedback,
            and_(
                NewsReviewFeedback.review_log_id == AutoReviewLog.id,
                NewsReviewFeedback.reviewer_id == actor.id,
            ),
        )
        .where(AutoReviewLog.decision.in_(
            ["auto_published", "shadow_pass", "manual_review", "review_error"]
        ))
        .order_by(AutoReviewLog.published_at.desc(), AutoReviewLog.id.desc())
        .limit(limit)
    )
    logs = []
    for log, news_status, source_url, feedback in result.all():
        try:
            reasons = json.loads(log.reasons_json)
            review_result = json.loads(log.review_result_json) if log.review_result_json else None
        except (TypeError, ValueError):
            reasons, review_result = ["audit_data_invalid"], None
        logs.append(
            {
                "id": log.id,
                "newsId": log.news_id,
                "title": log.title_snapshot,
                "status": news_status,
                "sourceId": log.source_id,
                "sourceName": log.source_name,
                "sourceUrl": source_url,
                "categoryId": log.category_id,
                "categoryName": log.category_name,
                "decision": log.decision,
                "reasons": reasons,
                "reviewResult": review_result,
                "lastAttemptAt": log.last_attempt_at,
                "publishedAt": log.published_at,
                "withdrawnAt": log.withdrawn_at,
                "withdrawReason": log.withdraw_reason,
                "attempts": log.attempts,
                "feedback": {
                    "label": feedback.label,
                    "safeToPublish": feedback.safe_to_publish,
                    "evidenceAccurate": feedback.evidence_accurate,
                    "reason": feedback.reason,
                } if feedback else None,
            }
        )
    return Result.success(logs)


@router.get("/auto-publish/evaluation")
async def get_auto_review_evaluation(
    _: User = Depends(require_permission("news:review")),
    db: AsyncSession = Depends(get_db),
):
    """按人工标注统计自动放行、人工拦截和证据引用的回归指标。"""
    auto_candidate = AutoReviewLog.decision.in_(["auto_published", "shadow_pass"])
    manual_candidate = AutoReviewLog.decision == "manual_review"
    result = await db.execute(
        select(
            func.count(NewsReviewFeedback.id),
            func.sum(case((and_(auto_candidate, NewsReviewFeedback.safe_to_publish.is_not(None)), 1), else_=0)),
            func.sum(case((and_(auto_candidate, NewsReviewFeedback.safe_to_publish.is_(False)), 1), else_=0)),
            func.sum(case((and_(manual_candidate, NewsReviewFeedback.safe_to_publish.is_not(None)), 1), else_=0)),
            func.sum(case((and_(manual_candidate, NewsReviewFeedback.safe_to_publish.is_(True)), 1), else_=0)),
            func.sum(case((NewsReviewFeedback.evidence_accurate.is_not(None), 1), else_=0)),
            func.sum(case((NewsReviewFeedback.evidence_accurate.is_(True), 1), else_=0)),
        )
        .select_from(NewsReviewFeedback)
        .join(AutoReviewLog, AutoReviewLog.id == NewsReviewFeedback.review_log_id)
    )
    values = result.one()
    feedback_count, auto_samples, false_allow, manual_samples, false_block, evidence_samples, accurate = (
        int(value or 0) for value in values
    )
    return Result.success({
        "mode": get_settings().auto_publish_mode,
        "feedbackCount": feedback_count,
        "autoCandidateSamples": auto_samples,
        "falseAllowCount": false_allow,
        "falseAllowRate": round(false_allow / auto_samples, 4) if auto_samples else None,
        "manualReviewSamples": manual_samples,
        "falseBlockCount": false_block,
        "falseBlockRate": round(false_block / manual_samples, 4) if manual_samples else None,
        "evidenceCitationSamples": evidence_samples,
        "evidenceCitationAccuracy": round(accurate / evidence_samples, 4) if evidence_samples else None,
        "definitions": {
            "falseAllowRate": "标注为不宜发布的自动放行稿件 / 已标注自动放行稿件",
            "falseBlockRate": "标注为可发布的人工复核稿件 / 已标注人工复核稿件",
            "evidenceCitationAccuracy": "人工认为引用准确的审核样本 / 已评价引用准确性的审核样本",
        },
    })


@router.post("/auto-publish/logs/{log_id}/feedback")
async def save_auto_review_feedback(
    log_id: int,
    request: NewsReviewFeedbackCreate,
    actor: User = Depends(require_permission("news:review")),
    db: AsyncSession = Depends(get_db),
):
    log = await db.get(AutoReviewLog, log_id)
    if log is None or log.decision not in {"auto_published", "shadow_pass", "manual_review"}:
        raise HTTPException(status_code=404, detail="Review sample not found")
    feedback = await db.scalar(
        select(NewsReviewFeedback).where(
            NewsReviewFeedback.review_log_id == log_id,
            NewsReviewFeedback.reviewer_id == actor.id,
        )
    )
    if feedback is None:
        feedback = NewsReviewFeedback(review_log_id=log_id, reviewer_id=actor.id, **request.model_dump())
        db.add(feedback)
    else:
        for field, value in request.model_dump().items():
            setattr(feedback, field, value)
    source_id = getattr(log, "source_id", None)
    source = await db.get(NewsFeedSource, source_id) if source_id else None
    if source is not None:
        await evaluate_source_quality(db, source)
    await db.commit()
    logger.info(
        "Automatic review feedback saved: actor_id=%s log_id=%s label=%s safe_to_publish=%s",
        actor.id,
        log_id,
        request.label,
        request.safe_to_publish,
    )
    return Result.success({"logId": log_id, "label": request.label}, "Review feedback saved")


@router.post("/auto-publish/logs/{log_id}/withdraw")
async def withdraw_auto_published_news(
    log_id: int,
    request: AutoPublishWithdrawRequest,
    actor: User = Depends(require_permission("news:publish")),
    db: AsyncSession = Depends(get_db),
):
    log = await db.get(AutoReviewLog, log_id)
    if log is None or log.decision != "auto_published":
        raise HTTPException(status_code=404, detail="Automatic publication record not found")
    if log.withdrawn_at is not None:
        raise HTTPException(status_code=409, detail="This automatic publication was already withdrawn")
    news = await db.get(News, log.news_id) if log.news_id else None
    if news is None:
        raise HTTPException(status_code=404, detail="News item no longer exists")
    if news.status != "published":
        raise HTTPException(status_code=409, detail="News item is no longer published")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    news.status = "archived"
    news.reviewed_by = actor.id
    news.reviewed_at = now
    log.withdrawn_at = now
    log.withdrawn_by = actor.id
    log.withdraw_reason = request.reason
    await db.commit()
    await invalidate_news_caches()
    logger.warning(
        "Automatically published news withdrawn: actor_id=%s news_id=%s log_id=%s",
        actor.id,
        news.id,
        log.id,
    )
    return Result.success(
        {"logId": log.id, "newsId": news.id, "status": news.status, "withdrawnAt": now},
        "Automatically published news withdrawn",
    )


@router.post("/content")
async def create_managed_news(
    request: NewsContentCreate,
    actor: User = Depends(require_permission("news:edit")),
    db: AsyncSession = Depends(get_db),
):
    if not await _category_exists(db, request.category_id):
        raise HTTPException(status_code=404, detail="News category not found")
    item = News(
        **request.model_dump(),
        status="draft",
        publish_time=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(item)
    await db.flush()
    logger.info("News draft created: actor_id=%s news_id=%s", actor.id, item.id)
    return Result.success(_news_payload(item), "News draft created")


@router.patch("/content/{news_id}")
async def update_managed_news(
    news_id: int,
    request: NewsContentUpdate,
    actor: User = Depends(require_permission("news:edit")),
    db: AsyncSession = Depends(get_db),
):
    item = await db.get(News, news_id)
    if item is None:
        raise HTTPException(status_code=404, detail="News not found")
    values = request.model_dump(exclude_unset=True)
    if not values:
        return Result.success(_news_payload(item))
    if "category_id" in values and not await _category_exists(db, values["category_id"]):
        raise HTTPException(status_code=404, detail="News category not found")
    for field, value in values.items():
        setattr(item, field, value)
    if item.status == "published":
        item.status = "draft"
        item.reviewed_by = None
        item.reviewed_at = None
        auto_log = await db.scalar(
            select(AutoReviewLog).where(AutoReviewLog.news_id == item.id)
        )
        if auto_log and auto_log.decision == "auto_published" and not auto_log.withdrawn_at:
            auto_log.withdrawn_at = datetime.now(timezone.utc).replace(tzinfo=None)
            auto_log.withdrawn_by = actor.id
            auto_log.withdraw_reason = "管理员编辑自动发布稿件，内容已转回草稿"
    await invalidate_news_caches()
    logger.info("News content edited: actor_id=%s news_id=%s", actor.id, item.id)
    return Result.success(_news_payload(item), "News saved as draft for review")


@router.patch("/content/{news_id}/status")
async def update_managed_news_status(
    news_id: int,
    request: NewsStatusUpdate,
    actor: User = Depends(require_permission("news:publish")),
    db: AsyncSession = Depends(get_db),
):
    item = await db.get(News, news_id)
    if item is None:
        raise HTTPException(status_code=404, detail="News not found")
    if item.status != request.status:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        item.status = request.status
        item.reviewed_by = actor.id if request.status != "draft" else None
        item.reviewed_at = now if request.status != "draft" else None
        if request.status == "published":
            item.publish_time = now
        elif request.status in {"draft", "archived"}:
            auto_log = await db.scalar(
                select(AutoReviewLog).where(AutoReviewLog.news_id == item.id)
            )
            if auto_log and auto_log.decision == "auto_published" and not auto_log.withdrawn_at:
                # 管理员通过常规下架操作撤回自动发布稿时，同步记录操作者和时间。
                auto_log.withdrawn_at = now
                auto_log.withdrawn_by = actor.id
                auto_log.withdraw_reason = "管理员通过内容状态管理撤回"
        await invalidate_news_caches()
        logger.info(
            "News status changed: actor_id=%s news_id=%s status=%s",
            actor.id,
            item.id,
            item.status,
        )
    return Result.success(_news_payload(item), "News status updated")
