import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.event_tracking import event_key, event_similarity
from services.evidence import retrieve_evidence
from services.orchestration import next_job_state, retry_delay_seconds
from services.source_quality import calculate_quality_score, quality_tier
from agent import operations


class OrchestrationPolicyTests(unittest.TestCase):
    def test_retry_backoff_is_bounded(self):
        self.assertEqual(retry_delay_seconds(1), 60)
        self.assertEqual(retry_delay_seconds(3), 240)
        self.assertEqual(retry_delay_seconds(20), 3600)

    def test_retryable_failure_is_scheduled_but_permanent_failure_stops(self):
        status, retry_at = next_job_state(
            current="running", error_code="fetch_timeout", attempt_count=2, max_attempts=3
        )
        self.assertEqual(status, "retrying")
        self.assertIsNotNone(retry_at)
        status, retry_at = next_job_state(
            current="running", error_code="unsafe_url", attempt_count=1, max_attempts=3
        )
        self.assertEqual(status, "failed")
        self.assertIsNone(retry_at)


class SourceQualityTests(unittest.TestCase):
    def test_source_tier_requires_human_review_samples(self):
        score, metrics = calculate_quality_score(
            source_type="official_original",
            is_active=True,
            tested=True,
            has_error=False,
            total_articles=10,
            unique_articles=10,
            feedback_samples=0,
            accurate_feedback=0,
        )
        self.assertGreaterEqual(score, 80)
        self.assertEqual(quality_tier(score, metrics["feedback_samples"]), "medium")
        self.assertEqual(quality_tier(90, 3), "high")
        self.assertEqual(quality_tier(90, 3, tested=False), "medium")

    def test_low_reliability_and_duplicates_reduce_score(self):
        score, metrics = calculate_quality_score(
            source_type="other",
            is_active=False,
            tested=False,
            has_error=True,
            total_articles=10,
            unique_articles=2,
            feedback_samples=5,
            accurate_feedback=1,
        )
        self.assertLess(score, 60)
        self.assertEqual(quality_tier(score, 5), "low")
        self.assertEqual(metrics["unique_articles"], 2)


class EventTrackingTests(unittest.TestCase):
    def test_event_key_is_stable_and_similar_titles_cluster(self):
        self.assertEqual(event_key("科技 重大进展！"), event_key("科技重大进展"))
        self.assertGreaterEqual(event_similarity("某市发布科技创新计划", "某市发布科技创新计划最新进展"), 0.72)


class EvidenceRetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_evidence_is_relevance_ranked_and_source_diverse(self):
        rows = [
            (
                SimpleNamespace(id=1, title="科技新闻进展", description="人工智能计划", content="技术研发进展详情", source_url="https://a.example/1", publish_time=datetime(2025, 1, 2), author="A"),
                SimpleNamespace(id=10, name="A", publisher_group="publisher-a", source_type="independent_media"),
            ),
            (
                SimpleNamespace(id=2, title="科技新闻进展", description="人工智能计划", content="另一篇相同转载", source_url="https://a.example/2", publish_time=datetime(2025, 1, 3), author="A"),
                SimpleNamespace(id=11, name="A copy", publisher_group="publisher-a", source_type="syndicated"),
            ),
            (
                SimpleNamespace(id=3, title="人工智能科技计划更新", description="最新科技研发", content="科技新闻进展的原文细节", source_url="https://b.example/3", publish_time=datetime(2025, 1, 1), author="B"),
                SimpleNamespace(id=12, name="B", publisher_group="publisher-b", source_type="official_original"),
            ),
        ]

        class FakeResult:
            def all(self):
                return rows

        class FakeDb:
            async def execute(self, statement):
                return FakeResult()

        items = await retrieve_evidence(FakeDb(), "科技新闻人工智能计划", limit=2)
        self.assertEqual(len(items), 2)
        self.assertEqual({item["publisher_group"] for item in items}, {"publisher-a", "publisher-b"})
        self.assertTrue(all(item["quote"] for item in items))
        self.assertTrue(all("source_url" in item and "publish_time" in item for item in items))


class AdminNaturalLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_and_manual_review_commands_create_confirmation_proposals(self):
        with patch.object(operations, "propose_pause_category", new=AsyncMock(return_value={"id": "pause-1"})) as pause:
            result = await operations.interpret_admin_command(8, "暂停科技来源")
        self.assertEqual(result["status"], "awaiting_confirmation")
        pause.assert_awaited_once_with(8, "科技")

        with patch.object(operations, "propose_manual_review_category", new=AsyncMock(return_value={"id": "review-1"})) as manual:
            result = await operations.interpret_admin_command(8, "把金融新闻全部改为人工审核")
        self.assertEqual(result["status"], "awaiting_confirmation")
        manual.assert_awaited_once_with(8, "金融")

    async def test_unknown_admin_command_is_rejected(self):
        with self.assertRaises(Exception) as raised:
            await operations.interpret_admin_command(8, "执行任意 SQL")
        self.assertEqual(getattr(raised.exception, "code", None), "unsupported_command")


if __name__ == "__main__":
    unittest.main()
