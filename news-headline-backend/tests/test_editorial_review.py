import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent import review
from models.news import News


class EditorialReviewTests(unittest.IsolatedAsyncioTestCase):
    def test_claim_citations_must_match_internal_evidence_and_key_claims_need_independence(self):
        evidence = [SimpleNamespace(
            id=42,
            title="A verified headline",
            description=None,
            content="Officials confirmed the bridge reopened on Tuesday.",
        )]
        metadata = {
            42: {
                "source_name": "Example News",
                "source_type": "independent_media",
                "publisher_group": "example-news",
                "source_url": "https://example.test/story",
                "publish_time": "2026-10-30T10:00:00",
                "provenance_verified": True,
                "independence_key": "example-news",
            }
        }
        result = review._grounded_claims(
            [{
                "claim": "The bridge reopened on Tuesday.",
                "importance": "key",
                "status": "supported",
                "evidence": [{
                    "news_id": 42,
                    "quote": "Officials confirmed the bridge reopened on Tuesday.",
                }],
            }],
            evidence,
            metadata,
        )
        self.assertEqual(result[0]["status"], "insufficient")
        self.assertEqual(result[0]["independent_source_count"], 1)
        self.assertTrue(result[0]["requires_more_independent_sources"])

        forged = review._grounded_claims(
            [{
                "claim": "The bridge reopened on Tuesday.",
                "importance": "normal",
                "status": "supported",
                "evidence": [{"news_id": 42, "quote": "This fabricated quote is not in the story."}],
            }],
            evidence,
            metadata,
        )
        self.assertEqual(forged[0]["status"], "insufficient")
        self.assertEqual(forged[0]["evidence"], [])

    def test_deterministic_chinese_risk_rules_are_available(self):
        self.assertIn("伤亡", review.SENSITIVE_PATTERNS)
        self.assertIn("公共安全", review.HIGH_IMPACT_RULES["public_safety"])

    def test_source_url_is_included_in_static_link_checks(self):
        findings = review.inspect_links("Article text", "https://bit.ly/short-link")
        self.assertEqual(findings[0]["reason"], "link_shortener")

    def test_static_link_inspection_flags_unsafe_links_without_fetching(self):
        findings = review.inspect_links(
            '<a href="javascript:alert(1)">x</a> '
            '<a href="http://example.com">plain http</a> '
            '<a href="https://127.0.0.1/private">local</a> '
            '<a href="https://example.org/story">safe</a>'
        )
        self.assertEqual(len(findings), 3)
        self.assertIn("unsupported_scheme_or_missing_host", {item["reason"] for item in findings})
        self.assertIn("unencrypted_http", {item["reason"] for item in findings})
        self.assertIn("non_public_ip", {item["reason"] for item in findings})

    def test_model_categories_and_citations_are_allow_listed(self):
        categories = [SimpleNamespace(id=3, name="科技")]
        result = review._validate_model_suggestions(
            {
                "suggested_title": "标题",
                "suggested_category_id": 999,
                "suggested_tags": ["AI", "标签"],
                "evidence_news_ids": [9, 99, True],
                "factual_status": "supported",
                "recommendation": "suggest_publish",
            },
            categories,
            {9},
        )
        self.assertIsNone(result["suggested_category_id"])
        self.assertEqual(result["factual_check"]["evidence_news_ids"], [9])
        self.assertEqual(result["factual_check"]["status"], "supported")

    async def test_editorial_assist_only_returns_suggestions_and_never_publishes(self):
        item = News(
            id=21,
            title="Draft headline",
            description="Draft summary",
            content="Draft text",
            category_id=1,
            status="draft",
            views=0,
        )
        db = SimpleNamespace(
            get=AsyncMock(return_value=item),
            execute=AsyncMock(return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [SimpleNamespace(id=1, name="科技")])
            )),
        )

        class Provider:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

            async def complete(self, messages, tools=None, response_format=None):
                return {"choices": [{"message": {"content": json.dumps({
                    "suggested_title": "Better headline",
                    "suggested_summary": "A summary",
                    "suggested_category_id": 1,
                    "suggested_tags": ["科技"],
                    "risk_flags": [],
                    "factual_status": "supported",
                    "evidence_news_ids": [999],
                    "recommendation": "suggest_publish",
                })}}]}

        with patch.object(review, "_published_evidence", new=AsyncMock(return_value=[])), \
                patch.object(review, "_duplicates", new=AsyncMock(return_value=[])), \
                patch.object(review, "OpenAICompatibleProvider", return_value=Provider()):
            result = await review.review_news_content(
                db,
                news_id=21,
                title=item.title,
                description=item.description,
                content=item.content,
                category_id=item.category_id,
            )

        self.assertEqual(result["suggestions"]["suggested_title"], "Better headline")
        self.assertEqual(result["suggestions"]["factual_check"]["status"], "unsupported")
        self.assertEqual(result["suggestions"]["factual_check"]["evidence_news_ids"], [])
        self.assertEqual(result["suggestions"]["recommendation"], "manual_review")
        self.assertFalse(result["publication_changed"])
        self.assertEqual(item.status, "draft")
        self.assertEqual(item.title, "Draft headline")
        db.execute.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
