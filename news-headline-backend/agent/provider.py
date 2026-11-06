import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx
from config.settings import get_settings


logger = logging.getLogger(__name__)
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


class ProviderError(Exception):
    """Provider failure with a safe, non-secret diagnostic code."""

    def __init__(self, code: str, status_code: int | None = None, attempts: int = 1):
        self.code = code
        self.status_code = status_code
        self.attempts = attempts
        super().__init__(code)


class OpenAICompatibleProvider:
    """Small server-side adapter for OpenAI-compatible chat completion APIs."""

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.ai_api_key
        self.base_url = settings.ai_api_base_url
        self.model = settings.ai_model
        self.timeout = settings.ai_timeout_seconds
        self.max_retries = settings.ai_max_retries
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> "OpenAICompatibleProvider":
        if not self.api_key:
            await self.client.aclose()
            raise ProviderError("provider_not_configured")
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.client.aclose()

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(min(0.5 * (2**attempt), 4.0))

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format
        for attempt in range(self.max_retries + 1):
            try:
                response = await self.client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError("response must be a JSON object")
                return body
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                retryable = status_code in RETRYABLE_STATUS_CODES
                if retryable and attempt < self.max_retries:
                    logger.warning(
                        "Model request retry: model=%s status=%s attempt=%s",
                        self.model,
                        status_code,
                        attempt + 1,
                    )
                    await self._backoff(attempt)
                    continue
                raise ProviderError(
                    f"provider_http_{status_code}", status_code, attempt + 1
                ) from exc
            except httpx.TimeoutException as exc:
                if attempt < self.max_retries:
                    logger.warning("Model request timeout; retrying: model=%s attempt=%s", self.model, attempt + 1)
                    await self._backoff(attempt)
                    continue
                raise ProviderError("provider_timeout", attempts=attempt + 1) from exc
            except httpx.RequestError as exc:
                if attempt < self.max_retries:
                    logger.warning(
                        "Model connection error; retrying: model=%s type=%s attempt=%s",
                        self.model,
                        type(exc).__name__,
                        attempt + 1,
                    )
                    await self._backoff(attempt)
                    continue
                raise ProviderError("provider_connection_error", attempts=attempt + 1) from exc
            except ValueError as exc:
                raise ProviderError("provider_invalid_response", attempts=attempt + 1) from exc

        raise ProviderError("provider_retries_exhausted", attempts=self.max_retries + 1)

    async def stream_text(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": 0.2,
        }
        for attempt in range(self.max_retries + 1):
            received_content = False
            try:
                async with self.client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=payload,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        content = (choices[0].get("delta") or {}).get("content")
                        if isinstance(content, str) and content:
                            received_content = True
                            yield content
                return
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    logger.warning(
                        "Model stream retry: model=%s status=%s attempt=%s",
                        self.model,
                        status_code,
                        attempt + 1,
                    )
                    await self._backoff(attempt)
                    continue
                raise ProviderError(
                    f"provider_http_{status_code}", status_code, attempt + 1
                ) from exc
            except httpx.TimeoutException as exc:
                if not received_content and attempt < self.max_retries:
                    logger.warning("Model stream timeout; retrying: model=%s attempt=%s", self.model, attempt + 1)
                    await self._backoff(attempt)
                    continue
                raise ProviderError("provider_stream_timeout", attempts=attempt + 1) from exc
            except httpx.RequestError as exc:
                if not received_content and attempt < self.max_retries:
                    logger.warning(
                        "Model stream connection error; retrying: model=%s type=%s attempt=%s",
                        self.model,
                        type(exc).__name__,
                        attempt + 1,
                    )
                    await self._backoff(attempt)
                    continue
                raise ProviderError("provider_stream_connection_error", attempts=attempt + 1) from exc


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


NEWS_TOOLS: list[dict[str, Any]] = [
    _tool(
        "search_evidence",
        "Search only already-published news stored in this system for evidence supporting a factual claim. Return source, publication time and exact article excerpt; never request external URLs.",
        {
            "claim": {"type": "string", "description": "A factual claim or event to verify, up to 500 characters."},
            "category_id": {"type": "integer", "description": "Optional existing category ID."},
            "limit": {"type": "integer", "description": "Maximum 1 to 10 evidence records."},
        },
        ["claim"],
    ),
    _tool(
        "search_news",
        "Search the local news database by keyword and optional date range. Use the returned articles as the only factual source for summaries and analysis.",
        {
            "query": {"type": "string", "description": "News topic or keywords, up to 100 characters."},
            "limit": {"type": "integer", "description": "Maximum 1 to 5 articles."},
            "date_from": {"type": "string", "description": "Optional start date in YYYY-MM-DD format."},
            "date_to": {"type": "string", "description": "Optional end date in YYYY-MM-DD format."},
        },
        ["query"],
    ),
    _tool(
        "get_news_detail",
        "Read one news article by its numeric ID.",
        {"news_id": {"type": "integer", "description": "News ID."}},
        ["news_id"],
    ),
    _tool(
        "get_hot_news",
        "Get the most viewed news articles, optionally filtered by category and date range. Use this for hot-topic requests.",
        {
            "limit": {"type": "integer", "description": "Maximum 1 to 10 articles."},
            "category_id": {"type": "integer", "description": "Optional category ID."},
            "date_from": {"type": "string", "description": "Optional start date in YYYY-MM-DD format."},
            "date_to": {"type": "string", "description": "Optional end date in YYYY-MM-DD format."},
        },
        [],
    ),
    _tool(
        "get_news_trend",
        "Calculate daily article counts and category distribution over a date range. Use for news volume trends; this is based on stored news records, not real-time web coverage.",
        {
            "date_from": {"type": "string", "description": "Optional start date in YYYY-MM-DD format; defaults to 7 days ago."},
            "date_to": {"type": "string", "description": "Optional end date in YYYY-MM-DD format; defaults to today."},
        },
        [],
    ),
    _tool(
        "summarize_news",
        "Retrieve a small set of matching articles so you can summarize them. Always cite article IDs from the returned results.",
        {
            "query": {"type": "string", "description": "Topic or keywords to summarize."},
            "limit": {"type": "integer", "description": "Maximum 1 to 5 articles."},
            "date_from": {"type": "string", "description": "Optional start date in YYYY-MM-DD format."},
            "date_to": {"type": "string", "description": "Optional end date in YYYY-MM-DD format."},
        },
        ["query"],
    ),
    _tool(
        "compare_news",
        "Retrieve up to 5 specified articles for a fact-grounded comparison. Compare only fields and claims present in the returned articles.",
        {
            "news_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 5,
                "description": "Two to five news IDs to compare.",
            }
        },
        ["news_ids"],
    ),
]


# 下列工具只由服务端提供给管理员；所有写操作名称都以 propose_ 开头并等待人工确认。
ADMIN_AGENT_TOOLS: list[dict[str, Any]] = [
    _tool(
        "propose_add_feed",
        "Propose adding an RSS/Atom source. Only call when a complete URL was explicitly provided in the current user message and a source name and category are known. This only creates a pending confirmation; it never fetches or adds immediately.",
        {
            "name": {"type": "string", "description": "Source display name, up to 100 characters."},
            "url": {"type": "string", "description": "The complete URL copied exactly from the current user message."},
            "category": {"type": "string", "description": "Existing news category name."},
        },
        ["name", "url", "category"],
    ),
    _tool(
        "propose_test_feed",
        "Propose testing an RSS/Atom URL explicitly present in the current user message. This only creates a pending confirmation and never requests the URL immediately.",
        {"url": {"type": "string", "description": "The complete URL copied exactly from the current user message."}},
        ["url"],
    ),
    _tool(
        "propose_collect_category",
        "Propose collecting active feeds in an existing category. Collection only happens after administrator confirmation and articles are saved as drafts.",
        {"category": {"type": "string", "description": "Existing news category name."}},
        ["category"],
    ),
    _tool(
        "get_collection_failures",
        "Read the most recent feed collection failures and their safe error codes. This is read-only.",
        {"limit": {"type": "integer", "description": "Number of failures, 1 to 10."}},
        [],
    ),
    _tool(
        "propose_retry_failed_sources",
        "Propose retrying currently active feed sources with recorded failures. This only creates a pending confirmation.",
        {},
        [],
    ),
    _tool(
        "get_collection_jobs",
        "Read recent collection task states, retry times, and safe failure codes. This is read-only.",
        {
            "limit": {"type": "integer", "description": "Maximum 1 to 20 jobs."},
            "status": {"type": "string", "enum": ["queued", "running", "succeeded", "retrying", "failed"]},
        },
        [],
    ),
    _tool(
        "get_event_alerts",
        "Read pending alerts about official developments or possible corrections to previously published event stories. The tool does not edit or withdraw articles.",
        {"limit": {"type": "integer", "description": "Maximum 1 to 10 alerts."}},
        [],
    ),
    _tool(
        "get_auto_publish_stats",
        "Read counts of automatic publication and manual-review decisions. Date range is limited to one year.",
        {
            "date_from": {"type": "string", "description": "Optional start date YYYY-MM-DD."},
            "date_to": {"type": "string", "description": "Optional end date YYYY-MM-DD."},
        },
        [],
    ),
    _tool(
        "propose_pause_category",
        "Propose pausing active news sources in an existing category. This creates a confirmation request; it does not change sources until an administrator confirms.",
        {"category": {"type": "string", "description": "Existing category name."}},
        ["category"],
    ),
    _tool(
        "propose_manual_review_category",
        "Propose disabling automatic publication for a category so all new items remain for human review. Requires administrator confirmation.",
        {"category": {"type": "string", "description": "Existing category name."}},
        ["category"],
    ),
    _tool(
        "propose_re_review_blocked",
        "Propose retrying previously blocked editorial reviews within a date range. The recheck remains subject to all current risk and publication gates and requires confirmation.",
        {
            "date_from": {"type": "string", "description": "Optional start date YYYY-MM-DD; defaults to yesterday."},
            "date_to": {"type": "string", "description": "Optional end date YYYY-MM-DD; defaults to today."},
        },
        [],
    ),
]
