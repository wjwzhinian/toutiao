"""事件归组、更新检测和更正告警服务。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.news import News, NewsFeedSource
from models.operations import NewsEvent, NewsEventAlert, NewsEventItem


CORRECTION_WORDS = ("更正", "纠正", "此前报道有误", "辟谣", "修订", "最新通报")


def event_key(title: str) -> str:
    """对标题做稳定归一化，生成不暴露原文的事件键。"""
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", title.casefold())
    return hashlib.sha256(normalized[:180].encode("utf-8")).hexdigest()


def event_similarity(left: str, right: str) -> float:
    """返回标题相似度，供解释事件归组结果。"""
    clean = lambda value: re.sub(r"\s+", "", value.casefold())
    return round(SequenceMatcher(None, clean(left), clean(right)).ratio(), 3)


async def track_news_event(db: AsyncSession, news: News) -> dict[str, Any]:
    """为一篇稿件建立事件归属，并在检测到更正词时创建人工告警。"""
    direct_key = event_key(news.title)
    event = await db.scalar(select(NewsEvent).where(NewsEvent.event_key == direct_key))
    similarity = 1.0
    if event is None:
        candidates = await db.scalars(
            select(NewsEvent)
            .where(NewsEvent.last_seen_at >= datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30))
            .order_by(NewsEvent.last_seen_at.desc())
            .limit(50)
        )
        best_similarity = 0.72
        for candidate in candidates:
            value = event_similarity(news.title, candidate.canonical_title)
            if value >= best_similarity:
                event, similarity = candidate, value
                best_similarity = value
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    is_update = event is not None
    if event is None:
        event = NewsEvent(
            event_key=direct_key,
            canonical_title=news.title[:255],
            latest_news_id=news.id,
            first_seen_at=now,
            last_seen_at=now,
            update_count=1,
            needs_review=False,
        )
        db.add(event)
        await db.flush()
    else:
        previous_news = None
        if event.latest_news_id:
            previous_news = await db.get(News, event.latest_news_id)
        if not previous_news or previous_news.status != "published":
            previous_news_id = await db.scalar(
                select(News.id)
                .join(NewsEventItem, NewsEventItem.news_id == News.id)
                .where(NewsEventItem.event_id == event.id, News.status == "published")
                .order_by(News.publish_time.desc(), News.id.desc())
                .limit(1)
            )
            previous_news = await db.get(News, previous_news_id) if previous_news_id else None
        source = await db.get(NewsFeedSource, news.feed_source_id) if news.feed_source_id else None
        correction = any(word in news.title for word in CORRECTION_WORDS)
        should_alert = bool(
            previous_news
            and previous_news.status == "published"
            and (correction or (source and source.source_type == "official_original"))
        )
        event.latest_news_id = news.id
        event.last_seen_at = now
        event.update_count += 1
        if should_alert:
            event.needs_review = True
            event.correction_summary = news.title[:500]
            db.add(
                NewsEventAlert(
                    event_id=event.id,
                    news_id=news.id,
                    alert_type="correction" if correction else "event_update",
                    status="pending",
                    summary=f"事件出现可能更正或官方更新，请编辑复核：{news.title[:420]}",
                    evidence_json=json.dumps(
                        {
                            "previousNewsId": previous_news.id if previous_news else None,
                            "latestNewsId": news.id,
                            "similarity": similarity,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
    existing = await db.scalar(
        select(NewsEventItem).where(NewsEventItem.news_id == news.id)
    )
    if existing is None:
        db.add(
            NewsEventItem(
                event_id=event.id,
                news_id=news.id,
                relation_type="update" if is_update else "original",
                similarity=similarity,
            )
        )
    return {"event_id": event.id, "is_update": is_update, "similarity": similarity}
