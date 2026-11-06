import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from services.news_collection import (
    FeedCollectionError,
    collect_feed,
    parse_feed_xml,
    validate_public_feed_url,
)


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example</title>
  <item><guid>story-1</guid><title>Technology update</title>
    <description><![CDATA[<p>New research <b>released</b>.</p><script>unsafe()</script>]]></description>
    <pubDate>Tue, 01 Jul 2025 10:00:00 GMT</pubDate><link>https://news.example/story-1</link>
  </item>
  <item><guid>story-2</guid><title>Technology update</title>
    <description><![CDATA[<p>New research <b>released</b>.</p><script>unsafe()</script>]]></description>
  </item>
</channel></rss>"""


class FeedParsingTests(unittest.TestCase):
    def test_rss_html_is_cleaned_and_identical_content_is_deduplicable(self):
        items = parse_feed_xml(RSS)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Technology update")
        self.assertEqual(items[0]["source_url"], "https://news.example/story-1")
        self.assertIn("New research released.", items[0]["content"])
        self.assertNotIn("unsafe", items[0]["content"])
        self.assertEqual(items[0]["content_hash"], items[1]["content_hash"])
        self.assertNotEqual(items[0]["external_id"], items[1]["external_id"])

    def test_atom_namespace_and_links_are_supported(self):
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <id>tag:example.com,2025:1</id><title>Atom story</title>
          <link href="https://news.example/atom-1" rel="alternate" />
          <summary>Short summary</summary><content type="html">&lt;p&gt;Full text&lt;/p&gt;</content>
        </entry></feed>"""
        items = parse_feed_xml(atom)
        self.assertEqual(items[0]["external_id"], "tag:example.com,2025:1")
        self.assertEqual(items[0]["content"], "Full text")
        self.assertEqual(items[0]["description"], "Short summary")

    def test_invalid_xml_and_oversized_feeds_are_rejected(self):
        for body, code in ((b"<rss", "invalid_feed"), (b"x" * (2 * 1024 * 1024 + 1), "feed_size_invalid")):
            with self.subTest(code=code), self.assertRaises(FeedCollectionError) as raised:
                parse_feed_xml(body)
            self.assertEqual(raised.exception.code, code)


class FeedUrlSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_and_nonstandard_port_targets_are_rejected(self):
        for url in (
            "http://127.0.0.1/rss.xml",
            "https://[::1]/feed.xml",
            "http://example.com:8080/rss.xml",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url), self.assertRaises(FeedCollectionError) as raised:
                await validate_public_feed_url(url)
            self.assertEqual(raised.exception.code, "unsafe_url")

    async def test_hostname_with_private_dns_answer_is_rejected(self):
        private_result = (None, None, None, None, ("10.0.0.12", 443))
        with patch("services.news_collection.asyncio.to_thread", new=AsyncMock(return_value=[private_result])):
            with self.assertRaises(FeedCollectionError) as raised:
                await validate_public_feed_url("https://feed.example/rss.xml")
        self.assertEqual(raised.exception.code, "unsafe_url")


class FeedCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_entries_are_drafts_and_repeated_content_is_skipped(self):
        class NestedTransaction:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

        class FakeDb:
            def __init__(self, existing):
                self.existing = existing
                self.added = []

            async def execute(self, statement):
                return SimpleNamespace(all=lambda: self.existing)

            def begin_nested(self):
                return NestedTransaction()

            def add(self, item):
                self.added.append(item)

            async def flush(self):
                return None

        source = SimpleNamespace(
            id=7,
            name="Tech feed",
            url="https://feed.example/rss.xml",
            category_id=3,
            is_active=True,
            last_fetched_at=None,
            last_error="previous_error",
        )
        with patch("services.news_collection._fetch_feed", new=AsyncMock(return_value=RSS)):
            first_db = FakeDb([])
            first = await collect_feed(first_db, source)

            self.assertEqual(first, {"created": 1, "skipped": 1, "status": "success"})
            self.assertEqual(first_db.added[0].status, "draft")
            self.assertEqual(first_db.added[0].feed_source_id, 7)
            self.assertIsNotNone(source.last_fetched_at)
            self.assertIsNone(source.last_error)

            existing = SimpleNamespace(
                external_id=first_db.added[0].external_id,
                content_hash=first_db.added[0].content_hash,
            )
            second_db = FakeDb([existing])
            second = await collect_feed(second_db, source)
        self.assertEqual(second, {"created": 0, "skipped": 2, "status": "success"})
        self.assertEqual(second_db.added, [])
