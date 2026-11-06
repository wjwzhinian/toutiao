import json
import unittest

import httpx

from agent.provider import OpenAICompatibleProvider, ProviderError


async def no_wait(attempt: int) -> None:
    return None


class ProviderRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = OpenAICompatibleProvider()
        await self.provider.client.aclose()
        self.provider.api_key = "unit-test-key"
        self.provider.max_retries = 2
        self.request_count = 0

    async def asyncTearDown(self) -> None:
        await self.provider.client.aclose()

    def use_handler(self, handler) -> None:
        self.provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_json_assist_request_does_not_enable_model_tools(self) -> None:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

        self.use_handler(handler)
        await self.provider.complete(
            [{"role": "user", "content": "review"}],
            response_format={"type": "json_object"},
        )

        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", captured)
        self.assertNotIn("tool_choice", captured)

    async def test_complete_retries_server_error_then_succeeds(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.request_count += 1
            if self.request_count == 1:
                return httpx.Response(503, json={"error": "temporary"})
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        self.use_handler(handler)
        self.provider._backoff = no_wait

        result = await self.provider.complete([], [])

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(self.request_count, 2)

    async def test_complete_does_not_retry_client_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.request_count += 1
            return httpx.Response(401, json={"error": "unauthorized"})

        self.use_handler(handler)

        with self.assertRaises(ProviderError) as raised:
            await self.provider.complete([], [])

        self.assertEqual(raised.exception.code, "provider_http_401")
        self.assertEqual(raised.exception.attempts, 1)
        self.assertEqual(self.request_count, 1)

    async def test_stream_retries_transient_status_before_content(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.request_count += 1
            if self.request_count == 1:
                return httpx.Response(503, text="temporary")
            return httpx.Response(
                200,
                text='data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
                headers={"content-type": "text/event-stream"},
            )

        self.use_handler(handler)
        self.provider._backoff = no_wait
        parts = [part async for part in self.provider.stream_text([])]

        self.assertEqual(parts, ["ok"])
        self.assertEqual(self.request_count, 2)


if __name__ == "__main__":
    unittest.main()
