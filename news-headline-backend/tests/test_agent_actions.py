import json
import unittest
from unittest.mock import AsyncMock, patch

from agent import actions


class AgentActionValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_url_must_match_complete_user_supplied_url(self):
        self.assertTrue(actions.url_was_in_user_message(
            "https://example.com/rss.xml", "请测试 https://example.com/rss.xml。"
        ))
        self.assertFalse(actions.url_was_in_user_message(
            "https://attacker.example/rss.xml", "请测试 https://example.com/rss.xml"
        ))
        self.assertFalse(actions.url_was_in_user_message(
            "https://example.com/other.xml", "请测试 https://example.com/rss.xml"
        ))

    async def test_test_feed_proposal_does_not_fetch_and_requires_user_url(self):
        with patch.object(actions, "validate_public_feed_url", new=AsyncMock()) as validate, \
                patch.object(actions, "_create_pending_action", new=AsyncMock(return_value={"id": "pending"})) as create:
            result = await actions.propose_test_feed(
                7,
                {"url": "https://example.com/rss.xml"},
                "测试 https://example.com/rss.xml",
            )

        self.assertEqual(result["id"], "pending")
        validate.assert_awaited_once_with("https://example.com/rss.xml")
        create.assert_awaited_once()

        with patch.object(actions, "_create_pending_action", new=AsyncMock()) as create:
            with self.assertRaises(actions.AgentActionError) as raised:
                await actions.propose_test_feed(7, {"url": "https://example.com/rss.xml"}, "测试这个地址")
        self.assertEqual(raised.exception.code, "feed_url_must_be_provided_by_user")
        create.assert_not_awaited()

    async def test_confirmation_is_owner_scoped_and_claimed_once(self):
        class Result:
            def __init__(self, row=None, rowcount=1):
                self.row = row
                self.rowcount = rowcount

            def mappings(self):
                return self

            def one_or_none(self):
                return self.row

        class Session:
            def __init__(self, results):
                self.results = iter(results)
                self.statements = []
                self.commits = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

            async def execute(self, statement, parameters):
                self.statements.append((str(statement), parameters))
                return next(self.results)

            async def commit(self):
                self.commits += 1

            async def rollback(self):
                return None

        pending = {"action_id": "action-1", "action_type": "test_feed", "parameters_json": json.dumps({"url": "https://example.com/rss"})}
        claim_db = Session([Result(pending), Result(rowcount=1)])
        finish_db = Session([Result(rowcount=1)])
        with patch.object(actions, "AsyncSessionLocal", side_effect=[claim_db, finish_db]), \
                patch.object(actions, "_execute_action", new=AsyncMock(return_value={"status": "success"})) as execute:
            result = await actions.confirm_pending_action(7, "action-1")

        self.assertEqual(result["status"], "completed")
        execute.assert_awaited_once_with("test_feed", {"url": "https://example.com/rss"})
        self.assertTrue(all("user_id = :user_id" in statement for statement, _ in claim_db.statements))
        self.assertEqual(claim_db.statements[0][1]["user_id"], 7)

    async def test_confirmation_rejects_other_owner_and_duplicate_claim(self):
        class Result:
            def __init__(self, row=None, rowcount=0):
                self.row = row
                self.rowcount = rowcount

            def mappings(self):
                return self

            def one_or_none(self):
                return self.row

        class Session:
            def __init__(self, results):
                self.results = iter(results)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

            async def execute(self, statement, parameters):
                return next(self.results)

            async def commit(self):
                return None

            async def rollback(self):
                return None

        with patch.object(actions, "AsyncSessionLocal", return_value=Session([Result()])):
            with self.assertRaises(actions.AgentActionError) as raised:
                await actions.confirm_pending_action(99, "owned-by-another-user")
        self.assertEqual(raised.exception.status_code, 404)

        pending = {"action_id": "action-2", "action_type": "test_feed", "parameters_json": "{}"}
        with patch.object(actions, "AsyncSessionLocal", return_value=Session([Result(pending), Result(rowcount=0)])), \
                patch.object(actions, "_execute_action", new=AsyncMock()) as execute:
            with self.assertRaises(actions.AgentActionError) as raised:
                await actions.confirm_pending_action(7, "action-2")
        self.assertEqual(raised.exception.status_code, 409)
        execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
