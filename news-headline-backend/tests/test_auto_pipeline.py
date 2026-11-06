import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from services.auto_pipeline import apply_auto_publish_mode, decide_auto_publication


class AutomaticPublicationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.enabled_at = datetime.utcnow() - timedelta(minutes=5)
        self.news = SimpleNamespace(
            id=81,
            status="draft",
            content="A detailed report with enough background and context. " * 4,
            source_url="https://trusted.example/news/81",
            category_id=7,
            created_at=datetime.utcnow(),
        )
        self.source = SimpleNamespace(
            id=12,
            is_active=True,
            auto_collect_enabled=True,
            trusted_for_auto_publish=True,
            source_type="independent_media",
            publisher_group="trusted.example",
            auto_publish_enabled_at=self.enabled_at,
            collection_failures=0,
        )
        self.category_policy = SimpleNamespace(
            enabled=True,
            enabled_at=self.enabled_at,
        )
        self.review = {
            "ai_status": "available",
            "suggestions": {
                "recommendation": "suggest_publish",
                "suggested_category_id": 7,
                "risk_flags": [],
                "factual_check": {
                    "status": "supported",
                    "evidence_news_ids": [42],
                    "claims": [{
                        "claim": "The reported event happened.",
                        "importance": "key",
                        "status": "supported",
                        "independent_source_count": 2,
                    }],
                },
            },
            "findings": {
                "duplicates": [],
                "suspicious_links": [],
                "sensitive_or_ad_flags": [],
            },
        }

    def test_publish_requires_all_independent_gates(self):
        result = decide_auto_publication(
            self.news, self.source, self.category_policy, self.review
        )
        self.assertEqual(result, {"decision": "auto_published", "reasons": []})

    def test_model_recommendation_alone_cannot_publish(self):
        self.source.trusted_for_auto_publish = False
        result = decide_auto_publication(
            self.news, self.source, self.category_policy, self.review
        )
        self.assertEqual(result["decision"], "manual_review")
        self.assertIn("source_not_trusted", result["reasons"])

    def test_model_labels_do_not_make_the_publication_decision(self):
        self.review["suggestions"]["recommendation"] = "manual_review"
        self.review["suggestions"]["risk_flags"] = ["model-only warning"]
        result = decide_auto_publication(
            self.news, self.source, self.category_policy, self.review
        )
        self.assertEqual(result["decision"], "auto_published")

    def test_source_provenance_and_independent_key_claims_are_required(self):
        self.source.source_type = "syndicated"
        result = decide_auto_publication(
            self.news, self.source, self.category_policy, self.review
        )
        self.assertIn("source_provenance_unverified", result["reasons"])

        self.source.source_type = "independent_media"
        self.review["suggestions"]["factual_check"]["claims"][0]["independent_source_count"] = 1
        result = decide_auto_publication(
            self.news, self.source, self.category_policy, self.review
        )
        self.assertIn("key_claim_lacks_independent_sources", result["reasons"])

    def test_shadow_mode_records_candidate_without_publishing(self):
        candidate = {"decision": "auto_published", "reasons": []}
        self.assertEqual(
            apply_auto_publish_mode(candidate, "shadow"),
            {"decision": "shadow_pass", "reasons": ["shadow_mode_no_publication"]},
        )
        self.assertEqual(apply_auto_publish_mode(candidate, "enforce"), candidate)

    def test_risks_or_missing_fact_evidence_force_manual_review(self):
        self.review["findings"]["suspicious_links"] = [{"reason": "link_shortener"}]
        self.review["suggestions"]["factual_check"]["evidence_news_ids"] = []
        result = decide_auto_publication(
            self.news, self.source, self.category_policy, self.review
        )
        self.assertEqual(result["decision"], "manual_review")
        self.assertIn("suspicious_link", result["reasons"])
        self.assertIn("insufficient_published_news_evidence", result["reasons"])

    def test_content_from_before_admin_approval_stays_manual(self):
        self.news.created_at = self.enabled_at - timedelta(seconds=1)
        result = decide_auto_publication(
            self.news, self.source, self.category_policy, self.review
        )
        self.assertIn("article_predates_source_approval", result["reasons"])
        self.assertEqual(result["decision"], "manual_review")

    def test_category_mismatch_forces_manual_review(self):
        self.review["suggestions"]["suggested_category_id"] = 8
        self.review["suggestions"]["risk_flags"] = ["uncertain attribution"]
        result = decide_auto_publication(
            self.news, self.source, self.category_policy, self.review
        )
        self.assertIn("category_suggestion_mismatch", result["reasons"])
        self.assertEqual(result["decision"], "manual_review")

    def test_source_below_quality_threshold_cannot_auto_publish(self):
        quality = SimpleNamespace(score=95, tier="medium")
        result = decide_auto_publication(
            self.news, self.source, self.category_policy, self.review, quality
        )
        self.assertEqual(result["decision"], "manual_review")
        self.assertIn("source_quality_below_auto_publish_threshold", result["reasons"])


if __name__ == "__main__":
    unittest.main()
