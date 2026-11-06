import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent import service
from agent.provider import ProviderError


class FakeProvider:
    def __init__(self, responses=None, failure=None):
        self.responses = iter(responses or [])
        self.failure = failure
        self.tool_lists = []

    async def __aenter__(self):
        if self.failure:
            raise self.failure
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def complete(self, messages, tools):
        self.tool_lists.append(tools)
        return next(self.responses)

    async def stream_text(self, messages):
        yield "streamed"


def events(chunks):
    parsed = []
    for chunk in chunks:
        lines = chunk.strip().splitlines()
        if len(lines) == 2:
            parsed.append((lines[0].removeprefix("event: "), json.loads(lines[1][6:])))
    return parsed


class AgentServiceRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_url_scope_is_limited_to_current_user_message(self):
        # 防止 Agent 在本轮没有收到 URL 时，把旧会话中的地址带入采集工具。
        current_message = "测试 https://feeds.example.org/current.xml"

        class Result:
            def mappings(self):
                return self

            def all(self):
                return [
                    SimpleNamespace(role="user", content="旧地址 https://feeds.example.org/old.xml"),
                    SimpleNamespace(role="assistant", content="已记录旧地址"),
                    SimpleNamespace(role="user", content=current_message),
                ]

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

            async def execute(self, statement, parameters):
                return Result()

            async def commit(self):
                return None

        with patch.object(service, "AsyncSessionLocal", return_value=Session()), \
                patch.object(service, "get_settings", return_value=SimpleNamespace(
                    ai_model="test-model", ai_provider_name="test-provider",
                )):
            prepared = await service.prepare_chat(12, current_message, None, is_admin=True)

        self.assertEqual(prepared.user_supplied_text, current_message)

    async def test_admin_action_tool_only_proposes_and_emits_confirmation_event(self):
        call = {
            "id": "call-propose",
            "function": {
                "name": "propose_test_feed",
                "arguments": '{"url":"https://example.com/rss.xml"}',
            },
        }
        provider = FakeProvider(responses=[
            {"choices": [{"message": {"content": None, "tool_calls": [call]}}]},
            {"choices": [{"message": {"content": "已创建待确认提案", "tool_calls": []}}]},
        ])
        prepared = service.PreparedRun(
            "session", "run-admin", [{"role": "user", "content": "test"}],
            user_id=12, is_admin=True, user_supplied_text="请测试 https://example.com/rss.xml",
        )
        proposed = {"id": "action-1", "type": "test_feed", "summary": "测试 feed", "status": "pending"}
        with patch.object(service, "OpenAICompatibleProvider", return_value=provider), \
                patch.object(service, "execute_admin_agent_tool", new=AsyncMock(return_value={
                    "status": "awaiting_confirmation", "pending_action": proposed,
                })) as action_tool, \
                patch.object(service, "_record_tool_call", new=AsyncMock()), \
                patch.object(service, "_save_assistant_message", new=AsyncMock()), \
                patch.object(service, "_finish_run", new=AsyncMock()):
            output = [chunk async for chunk in service.stream_chat(prepared)]

        admin_names = {item["function"]["name"] for item in provider.tool_lists[0]}
        self.assertIn("propose_test_feed", admin_names)
        action_tool.assert_awaited_once_with(
            12, "propose_test_feed", {"url": "https://example.com/rss.xml"},
            "请测试 https://example.com/rss.xml",
        )
        self.assertTrue(any(chunk.startswith("event: action_proposed") for chunk in output))

    async def test_regular_user_never_receives_admin_tools(self):
        call = {
            "id": "call-admin-tool",
            "function": {"name": "propose_retry_failed_sources", "arguments": "{}"},
        }
        provider = FakeProvider(responses=[
            {"choices": [{"message": {"content": None, "tool_calls": [call]}}]},
            {"choices": [{"message": {"content": "Not available", "tool_calls": []}}]},
        ])
        prepared = service.PreparedRun("session", "run", [{"role": "user", "content": "retry"}])
        with patch.object(service, "OpenAICompatibleProvider", return_value=provider), \
                patch.object(service, "execute_admin_agent_tool", new=AsyncMock()) as admin_tool, \
                patch.object(service, "_record_tool_call", new=AsyncMock()) as record, \
                patch.object(service, "_save_assistant_message", new=AsyncMock()), \
                patch.object(service, "_finish_run", new=AsyncMock()):
            output = [chunk async for chunk in service.stream_chat(prepared)]

        self.assertTrue(all(
            "propose_retry_failed_sources" not in {
                item["function"]["name"] for item in tool_list
            }
            for tool_list in provider.tool_lists
        ))
        admin_tool.assert_not_awaited()
        self.assertEqual(record.await_args.args[3], {"error": "tool_not_allowed"})

    async def test_tool_execution_and_stream_completion(self):
        tool_call = {
            "id": "call-1",
            "function": {
                "name": "search_news",
                "arguments": '{"query":"technology","limit":2}',
            },
        }
        provider = FakeProvider(
            responses=[
                {"choices": [{"message": {"content": None, "tool_calls": [tool_call]}}]},
                {"choices": [{"message": {"content": "Found [news ID: 1]", "tool_calls": []}}]},
            ]
        )
        prepared = service.PreparedRun("session-1", "run-1", [{"role": "user", "content": "find"}])
        with patch.object(service, "OpenAICompatibleProvider", return_value=provider), \
                patch.object(service, "execute_news_tool", new=AsyncMock(return_value={
                    "items": [{"id": 1}],
                    "citations": [{"news_id": 1, "title": "A"}],
                })), \
                patch.object(service, "_record_tool_call", new=AsyncMock()) as record, \
                patch.object(service, "_save_assistant_message", new=AsyncMock()) as save, \
                patch.object(service, "_finish_run", new=AsyncMock()) as finish:
            output = [chunk async for chunk in service.stream_chat(prepared)]

        parsed = events(output)
        names = [name for name, _ in parsed]
        self.assertIn("session", names)
        self.assertIn("tool_started", names)
        self.assertIn("tool_finished", names)
        self.assertIn("citations", names)
        self.assertIn("done", names)
        self.assertEqual(record.await_count, 1)
        save.assert_awaited_once()
        finish.assert_awaited_once_with("run-1", "completed", unittest.mock.ANY)

    async def test_provider_failure_is_safe_and_persisted(self):
        prepared = service.PreparedRun("session-1", "run-2", [{"role": "user", "content": "find"}])
        provider_error = ProviderError("provider_timeout", attempts=3)
        with patch.object(
            service,
            "OpenAICompatibleProvider",
            return_value=FakeProvider(failure=provider_error),
        ), patch.object(service, "_finish_run", new=AsyncMock()) as finish:
            output = [chunk async for chunk in service.stream_chat(prepared)]

        parsed = events(output)
        error_payloads = [payload for name, payload in parsed if name == "error"]
        self.assertEqual(error_payloads[0]["code"], "provider_timeout")
        self.assertNotIn("secret", json.dumps(error_payloads))
        finish.assert_awaited_once_with("run-2", "failed", unittest.mock.ANY, "provider_timeout")

    async def test_session_messages_cannot_be_read_by_another_user(self):
        class Result:
            def scalar_one_or_none(self):
                return None

        class Session:
            def __init__(self):
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

            async def execute(self, statement, parameters):
                self.calls.append((statement, parameters))
                return Result()

        db = Session()
        with patch.object(service, "AsyncSessionLocal", return_value=db):
            with self.assertRaises(LookupError):
                await service.get_session_messages(42, "session-owned-by-someone-else")

        self.assertEqual(len(db.calls), 1)
        statement, parameters = db.calls[0]
        self.assertIn("user_id = :user_id", str(statement))
        self.assertEqual(parameters["user_id"], 42)


if __name__ == "__main__":
    unittest.main()
