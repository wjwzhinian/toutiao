"""基于站内已发布新闻的证据检索服务。"""

from __future__ import annotations

import re
import logging
from html.parser import HTMLParser
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.mysql import match
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from models.news import News, NewsFeedSource
from config.settings import get_settings
from services.vector_store import VectorStoreError, search_news_vectors


MAX_EVIDENCE_LIMIT = 10
MAX_CLAIM_LENGTH = 500
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]{3,}|[\u4e00-\u9fff]{2,}")
logger = logging.getLogger(__name__)


class _TextParser(HTMLParser):
    """把新闻正文中的 HTML 标签去掉，避免把标签当成引用内容。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(value: str | None) -> str:
    parser = _TextParser()
    try:
        parser.feed(value or "")
        parser.close()
    except Exception:
        return value or ""
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _tokens(value: str) -> list[str]:
    """提取有限数量的检索词，防止用户输入造成过大的 OR 查询。"""
    result: list[str] = []
    for token in _TOKEN_PATTERN.findall(value.casefold()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            if len(token) <= 3:
                result.append(token)
            else:
                result.extend(token[index:index + 3] for index in range(len(token) - 2))
        elif len(token) >= 3:
            result.append(token)
        elif len(token) == 2:
            result.append(token)
    return list(dict.fromkeys(result))[:10]


def _quote(text: str, claim: str) -> str:
    """优先返回包含主张关键词的短原文片段，供编辑核对。"""
    normalized = " ".join(text.split())
    claim_tokens = _tokens(claim)
    for token in claim_tokens:
        position = normalized.casefold().find(token.casefold())
        if position >= 0:
            start = max(0, position - 120)
            return normalized[start:start + 360]
    return normalized[:360]


async def retrieve_evidence(
    db: AsyncSession,
    claim: str,
    *,
    category_id: int | None = None,
    exclude_news_id: int | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """只检索本地已发布稿件，不根据模型提供的 URL 发起外部请求。"""
    claim = claim.strip()[:MAX_CLAIM_LENGTH]
    tokens = _tokens(claim)
    settings = get_settings()
    if not tokens and not settings.qdrant_enabled:
        return []

    predicates = []
    for token in tokens:
        pattern = f"%{token}%"
        predicates.append(
            or_(News.title.ilike(pattern), News.description.ilike(pattern), News.content.ilike(pattern))
        )
    filters = [News.status == "published"]
    if category_id is not None:
        filters.append(News.category_id == category_id)
    if exclude_news_id is not None:
        filters.append(News.id != exclude_news_id)

    base_statement = (
        select(News, NewsFeedSource)
        .outerjoin(NewsFeedSource, NewsFeedSource.id == News.feed_source_id)
        .where(*filters)
        .order_by(News.publish_time.desc(), News.id.desc())
        .limit(100)
    )
    # MySQL ngram 全文召回与 LIKE 候选并用，后续仍按标题、摘要、正文和来源多样性重排。
    fulltext_filter = match(
        News.title,
        News.description,
        News.content,
        against=claim,
        in_natural_language_mode=True,
    )
    if tokens:
        hybrid_statement = base_statement.where(or_(or_(*predicates), fulltext_filter))
        statement = base_statement.where(or_(*predicates))
        try:
            result = await db.execute(hybrid_statement)
        except SQLAlchemyError:
        # 未执行迁移或数据库不支持 ngram 时，回退到已有关键词召回。
            await db.rollback()
            result = await db.execute(statement)
        keyword_rows = result.all()
    else:
        keyword_rows = []
    rows_by_id: dict[int, tuple[News, NewsFeedSource | None]] = {}
    for news, source in keyword_rows:
        rows_by_id[news.id] = (news, source)
    keyword_ids = list(rows_by_id)

    vector_scores: dict[int, float] = {}
    if settings.qdrant_enabled:
        try:
            vector_results = await search_news_vectors(
                claim,
                category_id=category_id,
                exclude_news_id=exclude_news_id,
                limit=max(limit * 10, 20),
            )
            vector_scores = {news_id: score for news_id, score in vector_results}
            missing_ids = [news_id for news_id in vector_scores if news_id not in rows_by_id]
            if missing_ids:
                vector_statement = (
                    select(News, NewsFeedSource)
                    .outerjoin(NewsFeedSource, NewsFeedSource.id == News.feed_source_id)
                    .where(*filters, News.id.in_(missing_ids))
                )
                vector_result = await db.execute(vector_statement)
                for news, source in vector_result.all():
                    rows_by_id[news.id] = (news, source)
            # 只用 MySQL 再次确认过的已发布新闻参与融合排序。
            vector_scores = {
                news_id: score for news_id, score in vector_scores.items() if news_id in rows_by_id
            }
        except VectorStoreError as exc:
            # Qdrant 或 Embedding 服务故障时保留 MySQL 全文和关键词检索结果。
            logger.warning("Semantic evidence search unavailable: error=%s", str(exc)[:100])
        except SQLAlchemyError:
            await db.rollback()
            logger.warning("Semantic evidence metadata lookup failed; retaining MySQL keyword results")
            vector_scores = {}
            if keyword_ids:
                # 回滚失败事务后重新加载 MySQL 结果，避免访问已过期的 ORM 实例。
                fallback_result = await db.execute(
                    select(News, NewsFeedSource)
                    .outerjoin(NewsFeedSource, NewsFeedSource.id == News.feed_source_id)
                    .where(*filters, News.id.in_(keyword_ids))
                )
                rows_by_id = {news.id: (news, source) for news, source in fallback_result.all()}

    candidates: list[tuple[float, str, News, NewsFeedSource | None, str, str]] = []
    for news, source in rows_by_id.values():
        body = plain_text(news.content)
        title_text = (news.title or "").casefold()
        summary_text = (news.description or "").casefold()
        body_text = body.casefold()
        title_hits = sum(token in title_text for token in tokens)
        summary_hits = sum(token in summary_text for token in tokens)
        body_hits = sum(token in body_text for token in tokens)
        matched = title_hits + summary_hits + body_hits
        lexical_score = (
            (title_hits * 2.0)
            + summary_hits
            + (body_hits * 0.5)
            + (matched / len(tokens) if tokens else 0.0)
        )
        semantic_score = vector_scores.get(news.id)
        if vector_scores:
            # 将余弦分数映射到 0..1，再与词面相关度按权重融合。
            normalized_semantic = max(0.0, min(1.0, (semantic_score + 1.0) / 2.0)) if semantic_score is not None else 0.0
            normalized_lexical = max(0.0, min(1.0, lexical_score / 8.0))
            relevance = 0.55 * normalized_semantic + 0.45 * normalized_lexical
            method = "hybrid" if semantic_score is not None and lexical_score > 0 else (
                "vector" if semantic_score is not None else "keyword"
            )
        else:
            relevance = lexical_score
            method = "keyword"
        group = (
            source.publisher_group.casefold()
            if source and source.publisher_group
            else f"source:{source.id}" if source else f"article:{news.id}"
        )
        candidates.append((relevance, group, news, source, body, method))

    # 先在不同稿源间取高相关材料，避免同一转载链占满证据名额。
    candidates.sort(
        key=lambda item: (
            item[0],
            item[2].publish_time.timestamp() if item[2].publish_time else 0,
            item[2].id,
        ),
        reverse=True,
    )
    max_items = max(1, min(limit, MAX_EVIDENCE_LIMIT))
    selected: list[tuple[float, str, News, NewsFeedSource | None, str, str]] = []
    used_groups: set[str] = set()
    for candidate in candidates:
        if candidate[1] in used_groups:
            continue
        selected.append(candidate)
        used_groups.add(candidate[1])
        if len(selected) >= max_items:
            break
    if len(selected) < max_items:
        selected_ids = {item[2].id for item in selected}
        selected.extend(item for item in candidates if item[2].id not in selected_ids)
        selected = selected[:max_items]

    items: list[dict[str, Any]] = []
    for relevance, _, news, source, body, method in selected:
        items.append(
            {
                "news_id": news.id,
                "title": news.title,
                "quote": _quote(" ".join(filter(None, (news.description, body))), claim),
                "source_name": source.name if source else news.author,
                "source_url": news.source_url,
                "publisher_group": source.publisher_group if source else None,
                "source_type": source.source_type if source else "other",
                "provenance_verified": bool(
                    source
                    and source.source_type in {"official_original", "independent_media"}
                    and source.publisher_group
                ),
                "publish_time": news.publish_time.isoformat() if news.publish_time else None,
                "relevance_score": round(relevance, 3),
                "retrieval_method": method,
            }
        )
    return items
