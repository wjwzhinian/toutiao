"""Safe, manual RSS and Atom ingestion for administrator-managed feeds."""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import re
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.news import News, NewsFeedSource
from services.event_tracking import track_news_event

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_FEED_ITEMS = 100
MAX_ARTICLE_CHARS = 50_000
FETCH_TIMEOUT_SECONDS = 15


class FeedCollectionError(Exception):
    """Safe, user-displayable collection failure code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _PlainText(HTMLParser):
    BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "blockquote"}
    HIDDEN_TAGS = {"script", "style", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs):
        if tag in self.HIDDEN_TAGS:
            self.hidden_depth += 1
        elif not self.hidden_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str):
        if tag in self.HIDDEN_TAGS and self.hidden_depth:
            self.hidden_depth -= 1
        elif not self.hidden_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str):
        if not self.hidden_depth:
            self.parts.append(data)


def _plain_text(value: str | None, limit: int = MAX_ARTICLE_CHARS) -> str:
    parser = _PlainText()
    try:
        parser.feed(value or "")
        parser.close()
        value = "".join(parser.parts)
    except Exception:
        value = value or ""
    value = html.unescape(value)
    value = re.sub(r"[\t\f\v ]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return value[:limit]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def _child_text(element: ET.Element, *names: str) -> str | None:
    for name in names:
        for child in element.iter():
            if child is element:
                continue
            if _local_name(child.tag) == name.lower():
                value = "".join(child.itertext()).strip()
                if value:
                    return value
    return None


def _entry_link(element: ET.Element) -> str | None:
    for child in element.iter():
        if _local_name(child.tag) == "link":
            href = child.attrib.get("href")
            if href and child.attrib.get("rel", "alternate") in {"alternate", ""}:
                return href.strip()
            if child.text and child.text.strip():
                return child.text.strip()
    return None


def _entry_image(element: ET.Element) -> str | None:
    for child in element.iter():
        name = _local_name(child.tag)
        candidate = child.attrib.get("url") if name in {"thumbnail", "content", "enclosure"} else None
        if candidate:
            parts = urlsplit(candidate.strip())
            if parts.scheme in {"http", "https"} and parts.hostname:
                return candidate.strip()[:255]
    return None


def _entry_time(element: ET.Element) -> datetime:
    raw = _child_text(element, "published", "updated", "pubdate", "date")
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            from email.utils import parsedate_to_datetime

            try:
                parsed = parsedate_to_datetime(raw)
                return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
            except (TypeError, ValueError, OverflowError):
                pass
    return datetime.utcnow()


def _external_id(raw: str | None, fallback: str) -> str:
    value = (raw or fallback).strip()
    if len(value) > 191:
        value = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return value


def _content_hash(title: str, content: str) -> str:
    normalized = f"{title.casefold()}\n{content.casefold()}"
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_feed_xml(body: bytes) -> list[dict]:
    """Parse RSS 2/RDF and Atom feeds into safe plain-text draft records."""
    if not body or len(body) > MAX_FEED_BYTES:
        raise FeedCollectionError("feed_size_invalid")
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise FeedCollectionError("invalid_feed")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise FeedCollectionError("invalid_feed") from None

    entries = [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}]
    records: list[dict] = []
    for entry in entries[:MAX_FEED_ITEMS]:
        title = _plain_text(_child_text(entry, "title"), 255)
        if not title:
            continue
        raw_content = _child_text(entry, "encoded", "content", "description", "summary") or ""
        content = _plain_text(raw_content)
        description = _plain_text(_child_text(entry, "description", "summary"), 500) or None
        link = _entry_link(entry)
        publish_time = _entry_time(entry)
        fallback = f"{title}\n{publish_time.isoformat()}"
        records.append(
            {
                "title": title,
                "description": description,
                "content": content or description or title,
                "image": _entry_image(entry),
                "external_id": _external_id(_child_text(entry, "guid", "id") or link, fallback),
                "source_url": link[:1000] if link else None,
                "content_hash": _content_hash(title, content or description or title),
                "publish_time": publish_time,
            }
        )
    if not entries:
        raise FeedCollectionError("invalid_feed")
    return records


async def validate_public_feed_url(url: str) -> None:
    """Reject local/private destinations and non-web ports before fetching."""
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise FeedCollectionError("unsafe_url")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise FeedCollectionError("unsafe_url") from None
    if port not in {80, 443}:
        raise FeedCollectionError("unsafe_url")

    hostname = parts.hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".localhost", ".local", ".internal")):
        raise FeedCollectionError("unsafe_url")
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = {literal}
    except ValueError:
        try:
            results = await asyncio.to_thread(
                socket.getaddrinfo, hostname, port, 0, socket.SOCK_STREAM
            )
            addresses = {ipaddress.ip_address(item[4][0].split("%", 1)[0]) for item in results}
        except (OSError, ValueError):
            raise FeedCollectionError("dns_error") from None
    if not addresses or any(not address.is_global for address in addresses):
        raise FeedCollectionError("unsafe_url")


async def _fetch_feed(url: str) -> bytes:
    await validate_public_feed_url(url)
    timeout = httpx.Timeout(FETCH_TIMEOUT_SECONDS, connect=5.0)
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"},
        ) as client:
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    raise FeedCollectionError("redirect_not_allowed")
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_FEED_BYTES:
                    raise FeedCollectionError("feed_size_invalid")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_FEED_BYTES:
                        raise FeedCollectionError("feed_size_invalid")
                    chunks.append(chunk)
                return b"".join(chunks)
    except FeedCollectionError:
        raise
    except httpx.TimeoutException:
        raise FeedCollectionError("fetch_timeout") from None
    except httpx.HTTPStatusError as exc:
        raise FeedCollectionError(f"http_status_{exc.response.status_code}") from None
    except (httpx.HTTPError, ValueError):
        raise FeedCollectionError("fetch_failed") from None


async def collect_feed(db: AsyncSession, source: NewsFeedSource) -> dict:
    """Fetch and store new entries as unpublished drafts, with stable deduplication."""
    if not source.is_active:
        raise FeedCollectionError("source_inactive")
    records = parse_feed_xml(await _fetch_feed(source.url))
    if not records:
        source.last_fetched_at = datetime.utcnow()
        source.last_error = None
        source.collection_failures = 0
        source.next_retry_at = None
        return {"created": 0, "skipped": 0, "status": "success"}

    external_ids = {item["external_id"] for item in records}
    hashes = {item["content_hash"] for item in records}
    existing_result = await db.execute(
        select(News.external_id, News.content_hash).where(
            or_(
                (News.feed_source_id == source.id) & News.external_id.in_(external_ids),
                News.content_hash.in_(hashes),
            )
        )
    )
    existing = existing_result.all()
    known_external_ids = {row.external_id for row in existing if row.external_id}
    known_hashes = {row.content_hash for row in existing if row.content_hash}

    created = skipped = 0
    for item in records:
        if item["external_id"] in known_external_ids or item["content_hash"] in known_hashes:
            skipped += 1
            continue
        try:
            async with db.begin_nested():
                news = News(
                        title=item["title"],
                        description=item["description"],
                        content=item["content"],
                        image=item["image"],
                        author=source.name[:50],
                        category_id=source.category_id,
                        publish_time=item["publish_time"],
                        status="draft",
                        feed_source_id=source.id,
                        external_id=item["external_id"],
                        content_hash=item["content_hash"],
                        source_url=item["source_url"],
                        # 审批时间窗口按 UTC 比较，明确写 UTC 创建时间避免本地时区影响资格判断。
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                db.add(news)
                await db.flush()
                # 采集单元测试和离线解析器可能使用最小数据库替身；真实 AsyncSession 才启用事件归组。
                if hasattr(db, "scalar") and news.id is not None:
                    await track_news_event(db, news)
        except IntegrityError:
            skipped += 1
            continue
        known_external_ids.add(item["external_id"])
        known_hashes.add(item["content_hash"])
        created += 1

    source.last_fetched_at = datetime.utcnow()
    source.last_error = None
    source.collection_failures = 0
    source.next_retry_at = None
    return {"created": created, "skipped": skipped, "status": "success"}
