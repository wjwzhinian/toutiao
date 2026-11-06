import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import tools as agent_tools
from crud import news as news_crud
from models.news import News
from routers.admin import (
    router as admin_router,
    create_feed_source,
    list_auto_publish_logs,
    update_feed_source,
    update_managed_news,
    update_managed_news_status,
)
from routers.agent import router as agent_router
from schemas.news_management import FeedSourceCreate, FeedSourceUpdate, NewsContentUpdate, NewsStatusUpdate
from utils.auth import get_current_user
from utils.permissions import has_permission


class AdminAccessTests(unittest.TestCase):
    def test_news_management_permission_is_admin_only(self):
        self.assertTrue(has_permission("admin", "news:manage"))
        self.assertFalse(has_permission("user", "news:manage"))

    def test_regular_user_cannot_list_feed_sources(self):
        app = FastAPI()
        app.include_router(admin_router)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=9, username="reader", role="user"
        )
        with TestClient(app) as client:
            response = client.get("/api/admin/feeds")
        self.assertEqual(response.status_code, 403)

    def test_regular_user_cannot_request_editorial_assist(self):
        app = FastAPI()
        app.include_router(admin_router)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=9, username="reader", role="user"
        )
        with TestClient(app) as client:
            response = client.post("/api/admin/content/review-assist", json={
                "title": "Draft",
                "description": "Summary",
                "content": "Content",
                "categoryId": 1,
            })
        self.assertEqual(response.status_code, 403)

    def test_regular_user_cannot_change_automatic_publish_categories(self):
        app = FastAPI()
        app.include_router(admin_router)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=9, username="reader", role="user"
        )
        with TestClient(app) as client:
            response = client.put(
                "/api/admin/auto-publish/categories/1", json={"enabled": True}
            )
        self.assertEqual(response.status_code, 403)

    def test_regular_user_cannot_list_or_confirm_agent_actions(self):
        app = FastAPI()
        app.include_router(agent_router)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=9, username="reader", role="user"
        )
        with TestClient(app) as client:
            pending = client.get("/api/agent/actions/pending")
            confirm = client.post("/api/agent/actions/action-id/confirm")
        self.assertEqual(pending.status_code, 403)
        self.assertEqual(confirm.status_code, 403)


class EditorialStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_review_log_query_includes_manual_review_outcomes(self):
        db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
        await list_auto_publish_logs(50, SimpleNamespace(id=3), db)
        statement = db.execute.call_args.args[0]
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("'manual_review'", compiled)
        self.assertIn("'review_error'", compiled)

    async def test_new_feed_cannot_be_activated_before_it_is_tested(self):
        db = SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(id=1)),
            add=Mock(),
            flush=AsyncMock(),
        )
        await create_feed_source(
            FeedSourceCreate(
                name="Unreviewed feed",
                url="https://feeds.example.org/rss.xml",
                categoryId=1,
                isActive=True,
                autoCollectEnabled=True,
            ),
            SimpleNamespace(id=3),
            db,
        )
        source = db.add.call_args.args[0]
        self.assertFalse(source.is_active)
        self.assertTrue(source.auto_collect_enabled)

    async def test_source_must_pass_feed_test_before_activation(self):
        source = SimpleNamespace(
            id=6,
            is_active=False,
            url="https://feeds.example.org/rss.xml",
            category_id=1,
            last_tested_at=None,
            last_test_error=None,
        )
        db = SimpleNamespace(get=AsyncMock(return_value=source))
        with self.assertRaises(Exception) as raised:
            await update_feed_source(
                6,
                FeedSourceUpdate(isActive=True),
                SimpleNamespace(id=3),
                db,
            )
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)

    async def test_publishing_records_reviewer_and_timestamp(self):
        item = News(
            id=4,
            title="A draft",
            description="Summary",
            content="Article text",
            category_id=1,
            views=0,
            publish_time=None,
            status="draft",
        )
        db = SimpleNamespace(
            get=AsyncMock(return_value=item),
            scalar=AsyncMock(return_value=None),
        )
        actor = SimpleNamespace(id=2)
        with patch("routers.admin.invalidate_news_caches", new=AsyncMock()):
            await update_managed_news_status(
                4, NewsStatusUpdate(status="published"), actor, db
            )
        self.assertEqual(item.status, "published")
        self.assertEqual(item.reviewed_by, 2)
        self.assertIsNotNone(item.reviewed_at)

    async def test_editing_published_news_returns_it_to_draft(self):
        item = News(
            id=5,
            title="Published title",
            description="Summary",
            content="Article text",
            category_id=1,
            views=4,
            publish_time=None,
            status="published",
            reviewed_by=2,
        )
        db = SimpleNamespace(
            get=AsyncMock(return_value=item),
            scalar=AsyncMock(return_value=None),
        )
        actor = SimpleNamespace(id=2)
        with patch("routers.admin.invalidate_news_caches", new=AsyncMock()):
            await update_managed_news(5, NewsContentUpdate(title="Revised title"), actor, db)
        self.assertEqual(item.title, "Revised title")
        self.assertEqual(item.status, "draft")
        self.assertIsNone(item.reviewed_by)

    async def test_public_news_query_filters_unpublished_rows(self):
        result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        db = SimpleNamespace(execute=AsyncMock(return_value=result))
        with patch("crud.news.get_cache_news_list", new=AsyncMock(return_value=None)):
            await news_crud.get_news_list(db, category_id=1)
        statement = db.execute.call_args.args[0]
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("news.status = 'published'", compiled)

    async def test_cached_public_detail_is_rechecked_against_current_status(self):
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
            )
        )
        cached = {
            "id": 11,
            "title": "Old published title",
            "content": "Old content",
            "category_id": 1,
            "views": 1,
        }
        with patch("crud.news.get_cached_news_detail", new=AsyncMock(return_value=cached)):
            result = await news_crud.get_news_detail(db, 11)
        self.assertIsNone(result)

    async def test_cached_public_list_drops_unpublished_rows(self):
        cached = [{
            "id": 12,
            "title": "Old title",
            "description": None,
            "image": None,
            "author": None,
            "category_id": 1,
            "views": 0,
            "publish_time": None,
        }]
        result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        db = SimpleNamespace(execute=AsyncMock(return_value=result))
        with patch("crud.news.get_cache_news_list", new=AsyncMock(return_value=cached)):
            rows = await news_crud.get_news_list(db, category_id=1)
        self.assertEqual(rows, [])

    async def test_agent_news_queries_filter_unpublished_rows(self):
        class FakeResult:
            def scalars(self):
                return self

            def all(self):
                return []

            def scalar_one_or_none(self):
                return None

            def mappings(self):
                return self

        class FakeSession:
            def __init__(self):
                self.statements = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

            async def execute(self, statement):
                self.statements.append(statement)
                return FakeResult()

        session = FakeSession()
        with patch("agent.tools.AsyncSessionLocal", return_value=session):
            await agent_tools.search_news({"query": "technology"})
            await agent_tools.get_news_detail({"news_id": 1})
            await agent_tools.get_hot_news({})
            await agent_tools.get_news_trend({})
            await agent_tools.compare_news({"news_ids": [1, 2]})

        compiled = [str(item.compile(compile_kwargs={"literal_binds": True})) for item in session.statements]
        self.assertGreaterEqual(len(compiled), 7)
        self.assertTrue(all("news.status = 'published'" in statement for statement in compiled))
