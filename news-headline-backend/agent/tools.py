import re
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select

from config.db_confing import AsyncSessionLocal
from config.settings import get_settings
from models.news import Category, News
from services.evidence import retrieve_evidence


MAX_NEWS_CONTENT_CHARS = 3000
MAX_TOOL_QUERY_CHARS = 100
MAX_TOOL_LIMIT = 10


def _today() -> date:
    zone_name = get_settings().app_timezone
    try:
        timezone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        timezone = (
            datetime_timezone(timedelta(hours=8))
            if zone_name == "Asia/Shanghai"
            else datetime_timezone.utc
        )
    return datetime.now(timezone).date()


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


async def search_news(arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"items": [], "citations": [], "error": "query_required"}

    query = query.strip()[:MAX_TOOL_QUERY_CHARS]
    terms = re.findall(r"[\w\u4e00-\u9fff]{2,}", query, flags=re.UNICODE)[:8]
    if not terms:
        terms = [query]

    predicates = []
    for term in terms:
        pattern = f"%{term.lower()}%"
        predicates.append(
            or_(
                func.lower(News.title).like(pattern),
                func.lower(News.description).like(pattern),
                func.lower(News.content).like(pattern),
            )
        )

    filters = [News.status == "published", or_(*predicates)]
    for field in ("date_from", "date_to"):
        raw_date = arguments.get(field)
        if raw_date is None:
            continue
        try:
            parsed_date = date.fromisoformat(raw_date)
        except (TypeError, ValueError):
            return {"items": [], "citations": [], "error": "invalid_date"}
        bound = datetime.combine(
            parsed_date,
            time.min if field == "date_from" else time.max,
        )
        if field == "date_from":
            filters.append(News.publish_time >= bound)
        else:
            filters.append(News.publish_time <= bound)

    try:
        limit = max(1, min(int(arguments.get("limit", 5)), 5))
    except (TypeError, ValueError):
        limit = 5
    statement = (
        select(News)
        .where(*filters)
        .order_by(News.publish_time.desc(), News.id.desc())
        .limit(limit)
    )

    async with AsyncSessionLocal() as db:
        result = await db.execute(statement)
        rows = result.scalars().all()

    items = [
        {
            "id": row.id,
            "title": row.title,
            "description": row.description,
            "content": (row.content or "")[:MAX_NEWS_CONTENT_CHARS],
            "author": row.author,
            "publish_time": _iso(row.publish_time),
        }
        for row in rows
    ]
    citations = [
        {
            "news_id": row.id,
            "title": row.title,
            "source": row.author,
            "publish_time": _iso(row.publish_time),
        }
        for row in rows
    ]
    return {"items": items, "citations": citations}


async def search_evidence(arguments: dict[str, Any]) -> dict[str, Any]:
    """检索站内已发布稿件，为事实主张返回原文引句和来源信息。"""
    claim = arguments.get("claim")
    if not isinstance(claim, str) or not 2 <= len(claim.strip()) <= 500:
        return {"items": [], "citations": [], "error": "invalid_claim"}
    category_id = arguments.get("category_id")
    if category_id is not None and (
        isinstance(category_id, bool) or not isinstance(category_id, int) or category_id <= 0
    ):
        return {"items": [], "citations": [], "error": "invalid_category_id"}
    limit = arguments.get("limit", 5)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        return {"items": [], "citations": [], "error": "invalid_limit"}
    async with AsyncSessionLocal() as db:
        items = await retrieve_evidence(db, claim, category_id=category_id, limit=limit)
    return {"items": items, "citations": items}


async def get_news_detail(arguments: dict[str, Any]) -> dict[str, Any]:
    news_id = arguments.get("news_id")
    if isinstance(news_id, bool) or not isinstance(news_id, int) or news_id <= 0:
        return {"item": None, "citations": [], "error": "invalid_news_id"}

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(News).where(News.id == news_id, News.status == "published")
        )
        row = result.scalar_one_or_none()

    if row is None:
        return {"item": None, "citations": [], "error": "news_not_found"}

    item = {
        "id": row.id,
        "title": row.title,
        "description": row.description,
        "content": (row.content or "")[:MAX_NEWS_CONTENT_CHARS],
        "author": row.author,
        "publish_time": _iso(row.publish_time),
    }
    citation = {
        "news_id": row.id,
        "title": row.title,
        "source": row.author,
        "publish_time": _iso(row.publish_time),
    }
    return {"item": item, "citations": [citation]}


def _parse_date(raw_value: Any, default: date) -> date:
    if raw_value is None:
        return default
    try:
        return date.fromisoformat(str(raw_value))
    except (TypeError, ValueError):
        raise ValueError("invalid_date") from None


def _news_item(row: News) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "description": row.description,
        "content": (row.content or "")[:MAX_NEWS_CONTENT_CHARS],
        "author": row.author,
        "category_id": row.category_id,
        "views": row.views,
        "publish_time": _iso(row.publish_time),
    }


def _citation(row: News) -> dict[str, Any]:
    return {
        "news_id": row.id,
        "title": row.title,
        "source": row.author,
        "publish_time": _iso(row.publish_time),
    }


async def get_hot_news(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return the most viewed stored articles for hot-topic questions."""
    try:
        limit = max(1, min(int(arguments.get("limit", 5)), MAX_TOOL_LIMIT))
        today = _today()
        date_from = _parse_date(arguments.get("date_from"), today - timedelta(days=29))
        date_to = _parse_date(arguments.get("date_to"), today)
    except (TypeError, ValueError):
        return {"items": [], "citations": [], "error": "invalid_date_or_limit"}
    if date_from > date_to:
        return {"items": [], "citations": [], "error": "invalid_date_range"}

    filters = [
        News.status == "published",
        News.publish_time >= datetime.combine(date_from, time.min),
        News.publish_time <= datetime.combine(date_to, time.max),
    ]
    category_id = arguments.get("category_id")
    if category_id is not None:
        try:
            filters.append(News.category_id == int(category_id))
        except (TypeError, ValueError):
            return {"items": [], "citations": [], "error": "invalid_category_id"}

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(News).where(*filters).order_by(News.views.desc(), News.publish_time.desc()).limit(limit)
        )
        rows = result.scalars().all()
    return {"items": [_news_item(row) for row in rows], "citations": [_citation(row) for row in rows]}


async def get_news_trend(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return daily volume and category counts from the local news database."""
    today = _today()
    try:
        date_from = _parse_date(arguments.get("date_from"), today - timedelta(days=6))
        date_to = _parse_date(arguments.get("date_to"), today)
    except ValueError:
        return {"error": "invalid_date"}
    if date_from > date_to or (date_to - date_from).days > 366:
        return {"error": "invalid_date_range"}

    date_filters = (
        News.status == "published",
        News.publish_time >= datetime.combine(date_from, time.min),
        News.publish_time <= datetime.combine(date_to, time.max),
    )
    async with AsyncSessionLocal() as db:
        daily_result = await db.execute(
            select(func.date(News.publish_time).label("day"), func.count(News.id).label("count"))
            .where(*date_filters)
            .group_by(func.date(News.publish_time))
            .order_by(func.date(News.publish_time))
        )
        category_result = await db.execute(
            select(
                Category.id.label("category_id"),
                Category.name.label("category_name"),
                func.count(News.id).label("count"),
            )
            .select_from(News)
            .join(Category, Category.id == News.category_id)
            .where(*date_filters)
            .group_by(Category.id, Category.name)
            .order_by(func.count(News.id).desc())
        )
        samples_result = await db.execute(
            select(News).where(*date_filters).order_by(News.publish_time.desc()).limit(MAX_TOOL_LIMIT)
        )
        samples = samples_result.scalars().all()

    daily = {
        row.day.isoformat() if hasattr(row.day, "isoformat") else str(row.day): row.count
        for row in daily_result.mappings().all()
    }
    categories = category_result.mappings().all()
    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "total": sum(daily.values()),
        "daily_counts": [
            {
                "date": (date_from + timedelta(days=i)).isoformat(),
                "count": daily.get((date_from + timedelta(days=i)).isoformat(), 0),
            }
            for i in range((date_to - date_from).days + 1)
        ],
        "category_counts": [
            {
                "category_id": row.category_id,
                "category_name": row.category_name,
                "count": row.count,
            }
            for row in categories
        ],
        "citations": [_citation(row) for row in samples],
    }


async def compare_news(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_ids = arguments.get("news_ids")
    if not isinstance(raw_ids, list) or not 2 <= len(raw_ids) <= 5:
        return {"items": [], "citations": [], "error": "news_ids_required"}
    try:
        if any(isinstance(value, bool) for value in raw_ids):
            raise ValueError("boolean is not a news id")
        ids = list(dict.fromkeys(int(value) for value in raw_ids))
    except (TypeError, ValueError):
        return {"items": [], "citations": [], "error": "invalid_news_ids"}
    if len(ids) < 2:
        return {"items": [], "citations": [], "error": "news_ids_required"}
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(News).where(News.id.in_(ids), News.status == "published")
        )
        rows_by_id = {row.id: row for row in result.scalars().all()}
    rows = [rows_by_id[item_id] for item_id in ids if item_id in rows_by_id]
    return {
        "items": [_news_item(row) for row in rows],
        "missing_ids": [item_id for item_id in ids if item_id not in rows_by_id],
        "citations": [_citation(row) for row in rows],
        "error": None if len(rows) >= 2 else "news_not_found",
    }


async def execute_news_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Explicit allowlist: model output can only reach these read-only functions."""
    if name == "search_news":
        return await search_news(arguments)
    if name == "search_evidence":
        return await search_evidence(arguments)
    if name == "get_news_detail":
        return await get_news_detail(arguments)
    if name == "get_hot_news":
        return await get_hot_news(arguments)
    if name == "get_news_trend":
        return await get_news_trend(arguments)
    if name == "summarize_news":
        return await search_news(arguments)
    if name == "compare_news":
        return await compare_news(arguments)
    return {"error": "tool_not_allowed"}
