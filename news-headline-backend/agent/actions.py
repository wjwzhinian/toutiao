"""新闻采集 Agent 的管理员专用操作：模型只能提案，实际执行需要人工确认。"""

from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select, text

from config.db_confing import AsyncSessionLocal
from models.news import AutoPublishCategory, AutoReviewLog, Category, News, NewsFeedSource
from models.operations import CollectionJob, NewsEvent, NewsEventAlert, SourceQualityScore
from services.orchestration import job_payload
from services.news_collection import (
    FeedCollectionError,
    _fetch_feed,
    collect_feed,
    parse_feed_xml,
    validate_public_feed_url,
)
from services.orchestration import fail_collection_job, finish_collection_job, start_collection_job

logger = logging.getLogger(__name__)
PENDING_ACTION_TTL_MINUTES = 10
MAX_FEEDS_PER_ACTION = 20
_RELEASE_LOCK_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


class AgentActionError(Exception):
    def __init__(self, code: str, status_code: int = 400):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _safe_action(row: Any) -> dict[str, Any]:
    return {
        "id": row.action_id,
        "type": row.action_type,
        "summary": row.summary,
        "status": row.status,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


def _clean_url_candidate(value: str) -> str:
    return value.strip().rstrip(".,!?;:，。！？；：")


def url_was_in_user_message(url: str, user_message: str) -> bool:
    """Require the proposed URL to match a complete URL typed by the user."""
    # 地址必须逐段匹配本轮用户输入，避免模型改写、拼接或复用历史轮次中的地址。
    try:
        candidate = urlsplit(_clean_url_candidate(url))
        if candidate.scheme not in {"http", "https"} or not candidate.hostname:
            return False
        for match in re.findall(r"https?://[^\s<>\"']+", user_message, flags=re.IGNORECASE):
            supplied = urlsplit(_clean_url_candidate(match))
            if (
                supplied.scheme.casefold() == candidate.scheme.casefold()
                and supplied.hostname.casefold() == candidate.hostname.casefold()
                and supplied.port == candidate.port
                and supplied.path == candidate.path
                and supplied.query == candidate.query
                and supplied.fragment == candidate.fragment
            ):
                return True
    except ValueError:
        return False
    return False


async def _category_by_name(name: str) -> Category:
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 50:
        raise AgentActionError("invalid_category")
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Category).where(Category.name.ilike(clean_name)).limit(1)
        )
        category = result.scalar_one_or_none()
    if category is None:
        raise AgentActionError("category_not_found")
    return category


async def _create_pending_action(
    user_id: int, action_type: str, parameters: dict[str, Any], summary: str
) -> dict[str, Any]:
    # 将提案与操作人、参数及过期时间一起持久化，确认接口只能按提案执行固定操作。
    action_id = str(uuid.uuid4())
    expires_at = datetime.utcnow() + timedelta(minutes=PENDING_ACTION_TTL_MINUTES)
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "INSERT INTO agent_pending_actions "
                "(action_id, user_id, action_type, parameters_json, summary, status, expires_at) "
                "VALUES (:action_id, :user_id, :action_type, :parameters_json, :summary, 'pending', :expires_at)"
            ),
            {
                "action_id": action_id,
                "user_id": user_id,
                "action_type": action_type,
                "parameters_json": _dump(parameters),
                "summary": summary[:500],
                "expires_at": expires_at,
            },
        )
        await db.commit()
    return {
        "id": action_id,
        "type": action_type,
        "summary": summary[:500],
        "status": "pending",
        "expires_at": expires_at.isoformat(),
    }


async def propose_add_feed(
    user_id: int, arguments: dict[str, Any], user_message: str
) -> dict[str, Any]:
    # 创建来源前先校验字段、分类和公网 Feed 地址；这里仅生成提案，不发起网络请求。
    name_value = arguments.get("name")
    url_value = arguments.get("url")
    category_value = arguments.get("category")
    if not all(isinstance(value, str) for value in (name_value, url_value, category_value)):
        raise AgentActionError("invalid_feed_parameters")
    name = name_value.strip()
    url = _clean_url_candidate(url_value)
    category_name = category_value.strip()
    if not name or len(name) > 100 or not category_name or len(category_name) > 50:
        raise AgentActionError("invalid_feed_parameters")
    if not url or len(url) > 1000 or not url_was_in_user_message(url, user_message):
        raise AgentActionError("feed_url_must_be_provided_by_user")
    category = await _category_by_name(category_name)
    await validate_public_feed_url(url)
    summary = f"添加采集源“{name}”，分类：{category.name}，地址：{url}"
    return await _create_pending_action(
        user_id,
        "add_feed",
        {"name": name, "url": url, "category_id": category.id},
        summary,
    )


async def propose_test_feed(
    user_id: int, arguments: dict[str, Any], user_message: str
) -> dict[str, Any]:
    # 测试地址同样要求来自本轮用户消息，联网解析会推迟到管理员确认之后。
    url_value = arguments.get("url")
    if not isinstance(url_value, str):
        raise AgentActionError("feed_url_must_be_provided_by_user")
    url = _clean_url_candidate(url_value)
    if not url or len(url) > 1000 or not url_was_in_user_message(url, user_message):
        raise AgentActionError("feed_url_must_be_provided_by_user")
    await validate_public_feed_url(url)
    return await _create_pending_action(
        user_id, "test_feed", {"url": url}, f"测试 RSS / Atom 地址：{url}"
    )


async def propose_collect_category(user_id: int, category_name: str) -> dict[str, Any]:
    # 保存当前启用来源的快照供确认页展示，避免确认后悄悄扩大采集范围。
    category = await _category_by_name(category_name)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                NewsFeedSource.id,
                NewsFeedSource.name,
                NewsFeedSource.url,
                NewsFeedSource.category_id,
            ).where(
                NewsFeedSource.category_id == category.id,
                NewsFeedSource.is_active.is_(True),
            ).limit(MAX_FEEDS_PER_ACTION)
        )
        sources = [dict(row._mapping) for row in result.all()]
    if not sources:
        raise AgentActionError("no_active_feeds_for_category")
    source_ids = [item["id"] for item in sources]
    names = "、".join(item["name"] for item in sources[:4])
    suffix = "等" if len(sources) > 4 else ""
    summary = f"立即采集“{category.name}”分类下的 {len(source_ids)} 个启用来源：{names}{suffix}"
    return await _create_pending_action(
        user_id,
        "collect_category",
        {"category_id": category.id, "sources": sources, "requested_by": user_id},
        summary,
    )


async def propose_retry_failed_sources(user_id: int) -> dict[str, Any]:
    # 仅提取启用且记录了失败状态的来源，并限制单次重试规模。
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                NewsFeedSource.id,
                NewsFeedSource.name,
                NewsFeedSource.url,
                NewsFeedSource.category_id,
            ).where(
                NewsFeedSource.is_active.is_(True),
                NewsFeedSource.last_error.is_not(None),
            ).order_by(NewsFeedSource.id.asc()).limit(MAX_FEEDS_PER_ACTION)
        )
        failed = result.all()
    if not failed:
        raise AgentActionError("no_failed_sources")
    sources = [dict(row._mapping) for row in failed]
    names = "、".join(item["name"] for item in sources[:5])
    suffix = "等" if len(failed) > 5 else ""
    summary = f"重新采集 {len(failed)} 个失败来源：{names}{suffix}"
    return await _create_pending_action(
        user_id, "retry_failed_sources", {"sources": sources, "requested_by": user_id}, summary
    )


async def get_recent_collection_failures(limit: int = 5) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 10))
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(NewsFeedSource).where(
                NewsFeedSource.last_error.is_not(None)
            ).order_by(NewsFeedSource.last_fetched_at.desc(), NewsFeedSource.id.desc()).limit(limit)
        )
        return [
            {
                "source_id": item.id,
                "name": item.name,
                "last_error": item.last_error,
                "last_fetched_at": item.last_fetched_at.isoformat() if item.last_fetched_at else None,
            }
            for item in result.scalars().all()
        ]


async def get_collection_jobs(limit: int = 10, status: str | None = None) -> list[dict[str, Any]]:
    """查询采集编排任务，只返回安全错误码和状态。"""
    async with AsyncSessionLocal() as db:
        query = select(CollectionJob).order_by(CollectionJob.created_at.desc()).limit(max(1, min(limit, 20)))
        if status:
            query = query.where(CollectionJob.status == status)
        result = await db.execute(query)
        return [job_payload(item) for item in result.scalars().all()]


async def get_event_alerts(limit: int = 5) -> list[dict[str, Any]]:
    """列出待编辑复核的事件更新和更正告警。"""
    limit = max(1, min(int(limit), 10))
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(NewsEventAlert, NewsEvent.canonical_title)
            .join(NewsEvent, NewsEvent.id == NewsEventAlert.event_id)
            .where(NewsEventAlert.status == "pending")
            .order_by(NewsEventAlert.created_at.desc(), NewsEventAlert.id.desc())
            .limit(limit)
        )
        return [
            {
                "id": alert.id,
                "event_id": alert.event_id,
                "event_title": title,
                "news_id": alert.news_id,
                "type": alert.alert_type,
                "summary": alert.summary,
                "created_at": alert.created_at.isoformat() if alert.created_at else None,
            }
            for alert, title in result.all()
        ]


async def get_auto_publish_stats(date_from: str | None = None, date_to: str | None = None) -> dict[str, Any]:
    """统计指定日期范围内自动发布和拦截的审核结果。"""
    try:
        end = date.fromisoformat(date_to) if date_to else date.today()
        start = date.fromisoformat(date_from) if date_from else end - timedelta(days=6)
    except (TypeError, ValueError) as exc:
        raise AgentActionError("invalid_date") from exc
    if start > end or (end - start).days > 366:
        raise AgentActionError("invalid_date_range")
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AutoReviewLog.decision, text("COUNT(*)")).where(
                AutoReviewLog.last_attempt_at >= datetime.combine(start, datetime.min.time()),
                AutoReviewLog.last_attempt_at <= datetime.combine(end, datetime.max.time()),
            ).group_by(AutoReviewLog.decision)
        )
        counts = {str(row[0]): int(row[1]) for row in result.all()}
    return {"date_from": start.isoformat(), "date_to": end.isoformat(), "counts": counts}


async def propose_pause_category(user_id: int, category_name: str) -> dict[str, Any]:
    """生成暂停一个分类下所有来源的确认提案。"""
    category = await _category_by_name(category_name)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(NewsFeedSource.id, NewsFeedSource.name, NewsFeedSource.category_id)
            .where(NewsFeedSource.category_id == category.id, NewsFeedSource.is_active.is_(True))
            .limit(MAX_FEEDS_PER_ACTION + 1)
        )
        sources = [dict(row._mapping) for row in result.all()]
    if len(sources) > MAX_FEEDS_PER_ACTION:
        raise AgentActionError("too_many_sources_for_single_action")
    return await _create_pending_action(
        user_id,
        "pause_category_sources",
        {"category_id": category.id, "sources": sources},
        f"暂停分类“{category.name}”下的 {len(sources)} 个新闻来源",
    )


async def propose_manual_review_category(user_id: int, category_name: str) -> dict[str, Any]:
    """生成关闭分类自动发布白名单的确认提案。"""
    category = await _category_by_name(category_name)
    return await _create_pending_action(
        user_id,
        "disable_auto_publish_category",
        {"category_id": category.id, "category_name": category.name},
        f"将“{category.name}”全部改为人工审核",
    )


async def propose_re_review_blocked(user_id: int, date_from: str | None, date_to: str | None) -> dict[str, Any]:
    """为指定日期内被拦截的稿件创建重新审核提案。"""
    try:
        end = date.fromisoformat(date_to) if date_to else date.today()
        start = date.fromisoformat(date_from) if date_from else end - timedelta(days=1)
    except (TypeError, ValueError) as exc:
        raise AgentActionError("invalid_date") from exc
    if start > end or (end - start).days > 31:
        raise AgentActionError("invalid_date_range")
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AutoReviewLog.id, AutoReviewLog.news_id, AutoReviewLog.title_snapshot)
            .join(News, News.id == AutoReviewLog.news_id)
            .where(
                AutoReviewLog.decision == "manual_review",
                AutoReviewLog.attempts < 5,
                News.status == "draft",
                AutoReviewLog.last_attempt_at >= datetime.combine(start, datetime.min.time()),
                AutoReviewLog.last_attempt_at <= datetime.combine(end, datetime.max.time()),
            ).limit(100)
        )
        logs = [dict(row._mapping) for row in result.all()]
    if not logs:
        raise AgentActionError("no_blocked_reviews")
    return await _create_pending_action(
        user_id,
        "re_review_blocked",
        {"log_ids": [item["id"] for item in logs], "date_from": start.isoformat(), "date_to": end.isoformat()},
        f"重新审核 {len(logs)} 篇被拦截稿件（{start.isoformat()} 至 {end.isoformat()}）",
    )


async def execute_admin_agent_tool(
    user_id: int, name: str, arguments: dict[str, Any], user_message: str
) -> dict[str, Any]:
    """Run the allow-listed, admin-only proposal/read tools."""
    # 模型工具调用采用白名单和严格参数集合，未知工具或多余参数一律拒绝。
    allowed_arguments = {
        "propose_add_feed": {"name", "url", "category"},
        "propose_test_feed": {"url"},
        "propose_collect_category": {"category"},
        "get_collection_failures": {"limit"},
        "propose_retry_failed_sources": set(),
        "get_collection_jobs": {"limit", "status"},
        "get_event_alerts": {"limit"},
        "get_auto_publish_stats": {"date_from", "date_to"},
        "propose_pause_category": {"category"},
        "propose_manual_review_category": {"category"},
        "propose_re_review_blocked": {"date_from", "date_to"},
    }
    if name not in allowed_arguments:
        raise AgentActionError("tool_not_allowed", 403)
    if set(arguments) - allowed_arguments[name]:
        raise AgentActionError("unexpected_tool_arguments")
    if name == "propose_add_feed":
        action = await propose_add_feed(user_id, arguments, user_message)
        return {"status": "awaiting_confirmation", "pending_action": action}
    if name == "propose_test_feed":
        action = await propose_test_feed(user_id, arguments, user_message)
        return {"status": "awaiting_confirmation", "pending_action": action}
    if name == "propose_collect_category":
        category = arguments.get("category")
        if not isinstance(category, str):
            raise AgentActionError("invalid_category")
        action = await propose_collect_category(user_id, category)
        return {"status": "awaiting_confirmation", "pending_action": action}
    if name == "get_collection_failures":
        limit = arguments.get("limit", 5)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise AgentActionError("invalid_limit")
        return {"failures": await get_recent_collection_failures(limit)}
    if name == "propose_retry_failed_sources":
        action = await propose_retry_failed_sources(user_id)
        return {"status": "awaiting_confirmation", "pending_action": action}
    if name == "get_collection_jobs":
        limit = arguments.get("limit", 10)
        status = arguments.get("status")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise AgentActionError("invalid_limit")
        if status is not None and status not in {"queued", "running", "succeeded", "retrying", "failed"}:
            raise AgentActionError("invalid_status")
        return {"jobs": await get_collection_jobs(limit, status)}
    if name == "get_event_alerts":
        limit = arguments.get("limit", 5)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise AgentActionError("invalid_limit")
        return {"alerts": await get_event_alerts(limit)}
    if name == "get_auto_publish_stats":
        return {"stats": await get_auto_publish_stats(arguments.get("date_from"), arguments.get("date_to"))}
    if name == "propose_pause_category":
        category = arguments.get("category")
        if not isinstance(category, str):
            raise AgentActionError("invalid_category")
        return {"status": "awaiting_confirmation", "pending_action": await propose_pause_category(user_id, category)}
    if name == "propose_manual_review_category":
        category = arguments.get("category")
        if not isinstance(category, str):
            raise AgentActionError("invalid_category")
        return {"status": "awaiting_confirmation", "pending_action": await propose_manual_review_category(user_id, category)}
    if name == "propose_re_review_blocked":
        return {"status": "awaiting_confirmation", "pending_action": await propose_re_review_blocked(user_id, arguments.get("date_from"), arguments.get("date_to"))}
    raise AgentActionError("tool_not_allowed", 403)


async def _test_feed(url: str) -> dict[str, Any]:
    # 只报告解析结果和少量标题样本，不把测试 Feed 写入新闻库。
    records = parse_feed_xml(await _fetch_feed(url))
    return {
        "status": "success",
        "entry_count": len(records),
        "sample_titles": [item["title"] for item in records[:5]],
    }


async def _collect_sources(
    sources: list[dict[str, Any]], retry_only: bool, requested_by: int | None = None
) -> dict[str, Any]:
    from config.cache_conf import redis_client

    created = skipped = failed = 0
    failures: list[dict[str, Any]] = []
    for snapshot in sources[:MAX_FEEDS_PER_ACTION]:
        source_id = snapshot.get("id")
        if isinstance(source_id, bool) or not isinstance(source_id, int):
            failed += 1
            failures.append({"source_id": None, "error": "invalid_source_snapshot"})
            continue
        lock_key = f"news:feed:collecting:{source_id}"
        lock_token = secrets.token_urlsafe(24)
        # 与普通采集入口共用 Redis 锁，避免两个入口同时拉取同一个来源。
        try:
            acquired = await redis_client.set(lock_key, lock_token, nx=True, ex=180)
        except Exception:
            acquired = False
        if not acquired:
            failed += 1
            failures.append({"source_id": source_id, "error": "collection_lock_unavailable"})
            continue
        try:
            job = None
            async with AsyncSessionLocal() as db:
                source = await db.get(NewsFeedSource, source_id)
                if (
                    source is None
                    or not source.is_active
                    or (retry_only and not source.last_error)
                ):
                    skipped += 1
                    continue
                if (
                    source.url != snapshot.get("url")
                    or source.name != snapshot.get("name")
                    or source.category_id != snapshot.get("category_id")
                ):
                    # 确认后来源资料若发生变化就跳过，要求管理员重新查看并确认新快照。
                    failed += 1
                    failures.append({"source_id": source_id, "error": "source_changed_after_confirmation"})
                    continue
                try:
                    job = start_collection_job(
                        db,
                        source,
                        trigger="agent",
                        requested_by=requested_by,
                    )
                    await db.flush()
                    result = await collect_feed(db, source)
                    finish_collection_job(job, source, result)
                    await db.commit()
                    created += result["created"]
                    skipped += result["skipped"]
                except FeedCollectionError as exc:
                    if job is not None:
                        fail_collection_job(job, source, exc.code)
                    else:
                        source.last_fetched_at = datetime.utcnow()
                        source.last_error = exc.code[:100]
                    await db.commit()
                    failed += 1
                    failures.append({"source_id": source_id, "error": exc.code})
                except Exception:
                    await db.rollback()
                    async with AsyncSessionLocal() as error_db:
                        error_source = await error_db.get(NewsFeedSource, source_id)
                        if error_source is not None:
                            error_job = start_collection_job(
                                error_db,
                                error_source,
                                trigger="agent",
                                requested_by=requested_by,
                            )
                            await error_db.flush()
                            fail_collection_job(error_job, error_source, "internal_error")
                            await error_db.commit()
                    failed += 1
                    failures.append({"source_id": source_id, "error": "collection_failed"})
                    logger.warning("Agent feed collection failed: source_id=%s", source_id)
        finally:
            try:
                await redis_client.eval(_RELEASE_LOCK_SCRIPT, 1, lock_key, lock_token)
            except Exception:
                logger.warning("Agent feed lock release failed: source_id=%s", source_id)
    return {"created": created, "skipped": skipped, "failed": failed, "failures": failures}


async def _execute_action(action_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
    if action_type == "test_feed":
        url = str(parameters.get("url", ""))
        # 确认阶段重新验证地址，降低提案创建后配置变化带来的风险。
        await validate_public_feed_url(url)
        return await _test_feed(url)
    if action_type == "add_feed":
        url = str(parameters.get("url", ""))
        await validate_public_feed_url(url)
        # 提案确认后先解析一次订阅，保证 Agent 新增来源和管理页面遵循同一测试门槛。
        await _test_feed(url)
        async with AsyncSessionLocal() as db:
            duplicate = await db.execute(
                select(NewsFeedSource.id).where(NewsFeedSource.url == url).limit(1)
            )
            if duplicate.scalar_one_or_none() is not None:
                raise AgentActionError("feed_already_exists", 409)
            category = await db.get(Category, int(parameters.get("category_id", 0)))
            if category is None:
                raise AgentActionError("category_not_found", 409)
            source = NewsFeedSource(
                name=str(parameters.get("name", ""))[:100],
                url=url,
                category_id=category.id,
                is_active=True,
                last_tested_at=datetime.utcnow(),
            )
            db.add(source)
            await db.commit()
            await db.refresh(source)
            return {"status": "success", "source_id": source.id, "name": source.name}
    if action_type == "collect_category":
        return await _collect_sources(
            parameters.get("sources", []), retry_only=False,
            requested_by=parameters.get("requested_by"),
        )
    if action_type == "retry_failed_sources":
        return await _collect_sources(
            parameters.get("sources", []), retry_only=True,
            requested_by=parameters.get("requested_by"),
        )
    if action_type == "pause_category_sources":
        source_snapshots = parameters.get("sources", [])
        async with AsyncSessionLocal() as db:
            changed = 0
            for snapshot in source_snapshots[:MAX_FEEDS_PER_ACTION]:
                source = await db.get(NewsFeedSource, snapshot.get("id"))
                if source and source.category_id == parameters.get("category_id") and source.name == snapshot.get("name"):
                    source.is_active = False
                    source.auto_collect_enabled = False
                    source.trusted_for_auto_publish = False
                    changed += 1
            await db.commit()
        return {"status": "success", "changed": changed}
    if action_type == "disable_auto_publish_category":
        async with AsyncSessionLocal() as db:
            policy = await db.get(AutoPublishCategory, int(parameters.get("category_id", 0)))
            if policy:
                policy.enabled = False
            await db.commit()
        return {"status": "success", "category_id": parameters.get("category_id"), "enabled": False}
    if action_type == "re_review_blocked":
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AutoReviewLog).where(AutoReviewLog.id.in_(parameters.get("log_ids", []))))
            count = 0
            for log in result.scalars().all():
                if log.decision == "manual_review":
                    log.decision = "review_error"
                    # 写入可重试状态，现有编排器会在退避窗口后重新审核并继续保留人工发布门禁。
                    log.last_attempt_at = datetime.utcnow()
                    count += 1
            await db.commit()
        return {"status": "success", "queued": count}
    raise AgentActionError("unsupported_action", 400)


async def list_pending_actions(user_id: int) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT action_id, action_type, summary, status, expires_at "
                "FROM agent_pending_actions WHERE user_id = :user_id "
                "AND status = 'pending' AND expires_at > UTC_TIMESTAMP() "
                "ORDER BY created_at DESC LIMIT 20"
            ),
            {"user_id": user_id},
        )
        return [_safe_action(row) for row in result.mappings().all()]


async def confirm_pending_action(user_id: int, action_id: str) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT action_id, action_type, parameters_json FROM agent_pending_actions "
                "WHERE action_id = :action_id AND user_id = :user_id "
                "AND status = 'pending' AND expires_at > UTC_TIMESTAMP()"
            ),
            {"action_id": action_id, "user_id": user_id},
        )
        action = result.mappings().one_or_none()
        if action is None:
            raise AgentActionError("action_not_found_or_expired", 404)
        # 条件更新是原子认领：并发点击时只有一个请求能开始执行该提案。
        claimed = await db.execute(
            text(
                "UPDATE agent_pending_actions SET status = 'executing', confirmed_at = UTC_TIMESTAMP() "
                "WHERE action_id = :action_id AND user_id = :user_id "
                "AND status = 'pending' AND expires_at > UTC_TIMESTAMP()"
            ),
            {"action_id": action_id, "user_id": user_id},
        )
        if claimed.rowcount != 1:
            await db.rollback()
            raise AgentActionError("action_already_confirmed", 409)
        await db.commit()

    try:
        parameters = json.loads(action["parameters_json"])
        if not isinstance(parameters, dict):
            raise AgentActionError("invalid_action_parameters", 409)
        result = await _execute_action(action["action_type"], parameters)
        status = "completed"
    except AgentActionError as exc:
        result = {"error": exc.code}
        status = "failed"
    except FeedCollectionError as exc:
        result = {"error": exc.code}
        status = "failed"
    except Exception:
        logger.warning("Confirmed Agent action failed: action_id=%s", action_id)
        result = {"error": "action_execution_failed"}
        status = "failed"

    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "UPDATE agent_pending_actions SET status = :status, result_json = :result_json, "
                "completed_at = UTC_TIMESTAMP() WHERE action_id = :action_id AND user_id = :user_id "
                "AND status = 'executing'"
            ),
            {
                "action_id": action_id,
                "user_id": user_id,
                "status": status,
                "result_json": _dump(result),
            },
        )
        await db.commit()
    return {"id": action_id, "status": status, "result": result}
