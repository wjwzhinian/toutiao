import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from models.base import Base
from models.news import NewsReviewFeedback
from models.users import User
from routers.admin import get_auto_review_evaluation, save_auto_review_feedback
from schemas.news_management import NewsReviewFeedbackCreate


class ReviewFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def test_feedback_foreign_key_uses_shared_model_metadata(self):
        self.assertIs(NewsReviewFeedback.metadata, Base.metadata)
        self.assertIs(User.metadata, Base.metadata)
        reviewer_fk = next(iter(NewsReviewFeedback.__table__.c.reviewer_id.foreign_keys))
        self.assertEqual(reviewer_fk.column.table.name, "user")

    async def test_feedback_is_saved_with_editor_label_and_outcomes(self):
        log = SimpleNamespace(id=17, decision="shadow_pass")
        db = SimpleNamespace(
            get=AsyncMock(return_value=log),
            scalar=AsyncMock(return_value=None),
            add=Mock(),
            commit=AsyncMock(),
        )
        actor = SimpleNamespace(id=4)
        request = NewsReviewFeedbackCreate(
            label="modified",
            safeToPublish=True,
            evidenceAccurate=False,
            reason="Claim lacks a second independent source.",
        )

        await save_auto_review_feedback(17, request, actor, db)

        saved = db.add.call_args.args[0]
        self.assertIsInstance(saved, NewsReviewFeedback)
        self.assertEqual(saved.review_log_id, 17)
        self.assertEqual(saved.reviewer_id, 4)
        self.assertEqual(saved.label, "modified")
        self.assertTrue(saved.safe_to_publish)
        self.assertFalse(saved.evidence_accurate)
        self.assertEqual(saved.reason, "Claim lacks a second independent source.")
        db.commit.assert_awaited_once()

    async def test_evaluation_returns_rates_from_human_labeled_samples(self):
        db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
            one=lambda: (10, 4, 1, 6, 2, 8, 7)
        )))
        with patch("routers.admin.get_settings", return_value=SimpleNamespace(auto_publish_mode="shadow")):
            response = await get_auto_review_evaluation(SimpleNamespace(id=1), db)

        self.assertEqual(response.data["mode"], "shadow")
        self.assertEqual(response.data["falseAllowRate"], 0.25)
        self.assertEqual(response.data["falseBlockRate"], round(2 / 6, 4))
        self.assertEqual(response.data["evidenceCitationAccuracy"], 0.875)


if __name__ == "__main__":
    unittest.main()
