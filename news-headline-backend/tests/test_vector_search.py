import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.evidence import retrieve_evidence
from services.vector_index import reconcile_vector_index_batch
from services.vector_store import QdrantVectorStore, VectorStoreError
from models.news import News, NewsVectorIndexState
from config.settings import Settings, validate_settings


def vector_settings(**overrides):
    values = {
        "qdrant_url": "http://qdrant:6333",
        "qdrant_api_key": "",
        "qdrant_collection": "news_evidence_v1",
        "embedding_api_base_url": "https://embedding.example/v1",
        "embedding_api_key": "test-secret",
        "embedding_model": "test-embedding",
        "embedding_dimension": 3,
        "embedding_version": "test-v1",
        "vector_api_timeout_seconds": 10,
        "vector_search_limit": 20,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_enabling_qdrant_requires_a_separate_embedding_provider_configuration():
    settings = Settings(
        app_env="test",
        host="127.0.0.1",
        port=8001,
        database_url="mysql+aiomysql://app:secret@localhost/news_app",
        sql_echo=False,
        redis_url="redis://localhost/0",
        redis_max_connections=5,
        ai_api_base_url="https://api.example.test",
        ai_api_key="chat-secret",
        ai_model="chat-model",
        ai_provider_name="example",
        ai_timeout_seconds=30,
        ai_max_retries=1,
        app_timezone="Asia/Shanghai",
        frontend_origins=("http://localhost:5173",),
        allowed_hosts=("localhost",),
        agent_rate_limit=10,
        agent_rate_window_seconds=60,
        agent_lock_ttl_seconds=240,
        qdrant_enabled=True,
        qdrant_url="http://127.0.0.1:6333",
        jwt_secret_key="test-only-secret-key-with-more-than-32-characters",
    )

    with pytest.raises(ValueError, match="EMBEDDING_API_BASE_URL"):
        validate_settings(settings)


def test_embedding_response_is_checked_against_configured_dimension():
    async def run():
        store = QdrantVectorStore(vector_settings())
        store._request = AsyncMock(return_value={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

        assert await store.embed("evidence query") == [0.1, 0.2, 0.3]
        store._request.assert_awaited_once()

        store._request = AsyncMock(return_value={"data": [{"embedding": [0.1, 0.2]}]})
        with pytest.raises(VectorStoreError, match="embedding_dimension_mismatch"):
            await store.embed("evidence query")

    asyncio.run(run())


def test_existing_named_vector_collection_is_rejected_explicitly():
    async def run():
        store = QdrantVectorStore(vector_settings())
        store._request = AsyncMock(
            return_value={
                "result": {
                    "config": {
                        "params": {
                            "vectors": {"news": {"size": 3, "distance": "Cosine"}}
                        }
                    }
                }
            }
        )

        with pytest.raises(VectorStoreError, match="qdrant_named_vectors_require_new_collection"):
            await store.ensure_collection()

        assert store._request.await_count == 1

    asyncio.run(run())


def test_existing_collection_dimension_must_match_embedding_configuration():
    async def run():
        store = QdrantVectorStore(vector_settings())
        store._request = AsyncMock(
            return_value={
                "result": {
                    "config": {"params": {"vectors": {"size": 4, "distance": "Cosine"}}}
                }
            }
        )

        with pytest.raises(VectorStoreError, match="qdrant_dimension_mismatch_use_new_collection"):
            await store.ensure_collection()

        assert store._request.await_count == 1

    asyncio.run(run())


def test_vector_query_applies_publication_version_category_and_exclusion_filters():
    async def run():
        store = QdrantVectorStore(vector_settings())
        store.ensure_collection = AsyncMock()
        store.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
        store._request = AsyncMock(
            return_value={"result": {"points": [{"id": 9, "score": 0.87, "payload": {"news_id": 9}}]}}
        )

        result = await store.search("semiconductor supply", category_id=4, exclude_news_id=12)

        assert result == [(9, 0.87)]
        body = store._request.await_args.kwargs["json_body"]
        assert body["filter"]["must"] == [
            {"key": "status", "match": {"value": "published"}},
            {"key": "embedding_version", "match": {"value": "test-v1"}},
            {"key": "category_id", "match": {"value": 4}},
        ]
        assert body["filter"]["must_not"] == [
            {"key": "news_id", "match": {"value": 12}}
        ]

    asyncio.run(run())


def make_news(news_id, title, description, content, author="Example Source"):
    return SimpleNamespace(
        id=news_id,
        title=title,
        description=description,
        content=content,
        author=author,
        source_url=f"https://example.test/{news_id}",
        publish_time=datetime(2026, 11, 1, 12, 0),
        category_id=1,
    )


def test_evidence_search_merges_vector_only_candidates_from_mysql():
    async def run():
        keyword_news = make_news(1, "AI chip advances", "New technology", "AI chip advances this year")
        semantic_news = make_news(
            2,
            "Semiconductor factories expand",
            "Manufacturers raise capacity",
            "Global semiconductor output is expected to rise.",
        )
        source = SimpleNamespace(
            name="Independent outlet",
            publisher_group="group-1",
            source_type="independent_media",
            id=2,
        )

        class Result:
            def __init__(self, rows):
                self.rows = rows

            def all(self):
                return self.rows

        class Db:
            def __init__(self):
                self.calls = 0

            async def execute(self, statement):
                self.calls += 1
                return Result([(keyword_news, None)] if self.calls == 1 else [(semantic_news, source)])

        with (
            patch("services.evidence.get_settings", return_value=SimpleNamespace(qdrant_enabled=True)),
            patch("services.evidence.search_news_vectors", new=AsyncMock(return_value=[(2, 0.9)])),
        ):
            items = await retrieve_evidence(Db(), "AI chip advances", limit=5)

        assert {item["news_id"] for item in items} == {1, 2}
        assert items[0]["news_id"] == 2
        assert items[0]["retrieval_method"] == "vector"
        assert items[0]["source_name"] == "Independent outlet"

    asyncio.run(run())


def test_evidence_search_falls_back_to_mysql_when_vector_service_fails():
    async def run():
        keyword_news = make_news(3, "AI chip advances", "New technology", "AI chip advances this year")

        class Result:
            def all(self):
                return [(keyword_news, None)]

        class Db:
            async def execute(self, statement):
                return Result()

        with (
            patch("services.evidence.get_settings", return_value=SimpleNamespace(qdrant_enabled=True)),
            patch(
                "services.evidence.search_news_vectors",
                new=AsyncMock(side_effect=VectorStoreError("qdrant_request_failed")),
            ),
        ):
            items = await retrieve_evidence(Db(), "AI chip advances", limit=5)

        assert len(items) == 1
        assert items[0]["news_id"] == 3
        assert items[0]["retrieval_method"] == "keyword"

    asyncio.run(run())


def test_semantic_search_accepts_short_chinese_claims_without_keyword_tokens():
    async def run():
        semantic_news = make_news(4, "芯片制造扩产", "产能持续增加", "半导体产业链正在扩大生产规模。")

        class Result:
            def all(self):
                return [(semantic_news, None)]

        class Db:
            async def execute(self, statement):
                return Result()

        with (
            patch("services.evidence.get_settings", return_value=SimpleNamespace(qdrant_enabled=True)),
            patch("services.evidence.search_news_vectors", new=AsyncMock(return_value=[(4, 0.82)])),
        ):
            items = await retrieve_evidence(Db(), "芯", limit=5)

        assert len(items) == 1
        assert items[0]["news_id"] == 4
        assert items[0]["retrieval_method"] == "vector"

    asyncio.run(run())


def test_vector_index_worker_marks_successful_news_as_indexed():
    async def run():
        news = make_news(8, "News title", "News summary", "News article body")
        news.status = "published"
        news.updated_at = datetime(2026, 11, 1, 12, 0)
        state = SimpleNamespace(
            news_id=8,
            status="pending",
            updated_at=datetime(2026, 11, 1, 12, 0),
            attempts=0,
        )

        class Result:
            def __init__(self, rows):
                self.rows = rows

            def scalars(self):
                return self

            def all(self):
                return self.rows

        class Db:
            def __init__(self):
                self.calls = 0

            async def execute(self, statement):
                self.calls += 1
                return Result([state] if self.calls == 1 else [])

            async def get(self, model, news_id):
                if model is NewsVectorIndexState:
                    return state
                if model is News:
                    return news
                return None

            async def commit(self):
                return None

            async def close(self):
                return None

        class Store:
            def __init__(self, settings):
                self.upsert_news = AsyncMock()
                self.delete_news = AsyncMock()

        db = Db()
        configuration = SimpleNamespace(
            qdrant_enabled=True,
            embedding_version="test-v1",
            vector_index_batch_size=10,
        )
        with (
            patch("services.vector_index.get_settings", return_value=configuration),
            patch("services.vector_index.AsyncSessionLocal", return_value=db),
            patch("services.vector_index.QdrantVectorStore", Store),
        ):
            stats = await reconcile_vector_index_batch()

        assert stats == {"status": "ok", "indexed": 1, "deleted": 0, "failed": 0}
        assert state.status == "indexed"
        assert state.embedding_version == "test-v1"
        assert state.content_fingerprint
        assert state.attempts == 0

    asyncio.run(run())


def test_vector_index_failure_is_persisted_for_backoff_retry():
    async def run():
        news = make_news(9, "News title", "News summary", "News article body")
        news.status = "published"
        news.updated_at = datetime(2026, 11, 1, 12, 0)
        state = SimpleNamespace(
            news_id=9,
            status="pending",
            updated_at=datetime(2026, 11, 1, 12, 0),
            attempts=0,
        )

        class Result:
            def __init__(self, rows):
                self.rows = rows

            def scalars(self):
                return self

            def all(self):
                return self.rows

        class Db:
            def __init__(self):
                self.calls = 0

            async def execute(self, statement):
                self.calls += 1
                return Result([state] if self.calls == 1 else [])

            async def get(self, model, news_id):
                return state if model is NewsVectorIndexState else news

            async def commit(self):
                return None

            async def close(self):
                return None

        class Store:
            def __init__(self, settings):
                self.upsert_news = AsyncMock(side_effect=VectorStoreError("embedding_http_503"))
                self.delete_news = AsyncMock()

        db = Db()
        configuration = SimpleNamespace(
            qdrant_enabled=True,
            embedding_version="test-v1",
            vector_index_batch_size=10,
        )
        with (
            patch("services.vector_index.get_settings", return_value=configuration),
            patch("services.vector_index.AsyncSessionLocal", return_value=db),
            patch("services.vector_index.QdrantVectorStore", Store),
        ):
            stats = await reconcile_vector_index_batch()

        assert stats["failed"] == 1
        assert state.status == "failed"
        assert state.attempts == 1
        assert state.last_error == "embedding_http_503"
        assert state.next_retry_at > datetime.utcnow()

    asyncio.run(run())
