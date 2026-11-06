import unittest
import hashlib
import json
from datetime import datetime, timedelta
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from config.settings import Settings, validate_settings
from routers.admin import router as admin_router
from routers.agent import router as agent_router
from utils.auth import _extract_bearer_token
from utils.permissions import has_permission, permissions_for_role
from utils.auth import get_current_user
from crud.users import create_token
from crud.users import get_user_by_token
from models.users import UserToken
from utils.exception import general_exception_handler
from utils.auth import get_agent_user_id


class SecurityConfigTests(unittest.TestCase):
    def test_settings_repr_redacts_secrets(self):
        settings = Settings(
            jwt_secret_key="test-only-jwt-secret-with-more-than-32-chars",
            app_env="test",
            host="127.0.0.1",
            port=8001,
            database_url="mysql+aiomysql://app:secret@localhost/news_app",
            sql_echo=False,
            redis_url="redis://:redis-secret@localhost/0",
            redis_max_connections=5,
            ai_api_base_url="https://api.deepseek.com",
            ai_api_key="deepseek-secret",
            ai_model="deepseek-flash",
            ai_provider_name="deepseek",
            ai_timeout_seconds=45,
            ai_max_retries=2,
            app_timezone="Asia/Shanghai",
            frontend_origins=("http://localhost:5173",),
            allowed_hosts=("localhost",),
            agent_rate_limit=10,
            agent_rate_window_seconds=60,
            agent_lock_ttl_seconds=240,
        )
        rendered = repr(settings)
        self.assertIn("redacted", rendered)
        self.assertNotIn("deepseek-secret", rendered)
        self.assertNotIn("redis-secret", rendered)

    def test_production_validation_rejects_insecure_defaults(self):
        settings = Settings(
            jwt_secret_key="test-only-jwt-secret-with-more-than-32-chars",
            app_env="production",
            host="0.0.0.0",
            port=8001,
            database_url="mysql+aiomysql://root:password@db/news_app",
            sql_echo=True,
            redis_url="redis://localhost/0",
            redis_max_connections=5,
            ai_api_base_url="http://api.deepseek.com",
            ai_api_key="replace-with-a-secret",
            ai_model="deepseek-flash",
            ai_provider_name="deepseek",
            ai_timeout_seconds=45,
            ai_max_retries=2,
            app_timezone="Asia/Shanghai",
            frontend_origins=("http://localhost:5173",),
            allowed_hosts=("*",),
            agent_rate_limit=10,
            agent_rate_window_seconds=60,
            agent_lock_ttl_seconds=240,
        )
        with self.assertRaises(ValueError):
            validate_settings(settings)

    def test_production_validation_accepts_hardened_configuration(self):
        settings = Settings(
            jwt_secret_key="test-only-jwt-secret-with-more-than-32-chars",
            app_env="production",
            host="0.0.0.0",
            port=8001,
            database_url="mysql+aiomysql://news_app:managed-secret@db.internal/news_app",
            sql_echo=False,
            redis_url="rediss://cache.internal/0",
            redis_max_connections=50,
            ai_api_base_url="https://api.deepseek.com",
            ai_api_key="configured-provider-key",
            ai_model="deepseek-flash",
            ai_provider_name="deepseek",
            ai_timeout_seconds=45,
            ai_max_retries=2,
            app_timezone="Asia/Shanghai",
            frontend_origins=("https://news.example.com",),
            jwt_cookie_secure=True,
            allowed_hosts=("news.example.com", "api.example.com"),
            agent_rate_limit=10,
            agent_rate_window_seconds=60,
            agent_lock_ttl_seconds=240,
        )
        with patch("importlib.util.find_spec", return_value=object()):
            validate_settings(settings)

        self.assertEqual(settings.auto_publish_mode, "shadow")
        with self.assertRaisesRegex(ValueError, "AUTO_PUBLISH_MODE"):
            validate_settings(replace(settings, auto_publish_mode="automatic"))

        unsafe = (
            replace(settings, ai_api_key="replace-with-a-secret"),
            replace(settings, allowed_hosts=("*",)),
            replace(settings, frontend_origins=("http://news.example.com",)),
            replace(settings, ai_api_base_url="http://api.deepseek.com"),
            replace(settings, database_url="mysql+aiomysql://root:secret@db/news_app"),
            replace(settings, sql_echo=True),
        )
        for configuration in unsafe:
            with self.subTest(configuration=configuration.app_env), self.assertRaises(ValueError):
                validate_settings(configuration)


class AuthAndPermissionTests(unittest.TestCase):
    def test_bearer_parser_rejects_missing_or_malformed_header(self):
        for value in (None, "", "Basic abc", "Bearer", "Bearer a b"):
            with self.subTest(value=value), self.assertRaises(HTTPException) as raised:
                _extract_bearer_token(value)
            self.assertEqual(raised.exception.status_code, 401)

    def test_permissions_are_least_privilege_by_role(self):
        self.assertTrue(has_permission("user", "agent:use"))
        self.assertFalse(has_permission("user", "users:manage_roles"))
        self.assertTrue(has_permission("admin", "users:manage_roles"))
        self.assertTrue(has_permission("editor", "news:edit"))
        self.assertFalse(has_permission("editor", "feeds:manage"))
        self.assertTrue(has_permission("reviewer", "news:review"))
        self.assertFalse(has_permission("reviewer", "news:publish"))
        self.assertTrue(has_permission("viewer", "history:read"))
        self.assertEqual(permissions_for_role("unknown"), ())

    def test_admin_endpoint_returns_forbidden_for_regular_user(self):
        app = FastAPI()
        app.include_router(admin_router)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=8, username="reader", role="user"
        )
        with TestClient(app) as client:
            response = client.get("/api/admin/access")
        self.assertEqual(response.status_code, 403)

    def test_admin_endpoint_returns_effective_permissions_for_admin(self):
        app = FastAPI()
        app.include_router(admin_router)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=1, username="operator", role="admin"
        )
        with TestClient(app) as client:
            response = client.get("/api/admin/access")
        self.assertEqual(response.status_code, 200)
        self.assertIn("users:manage_roles", response.json()["data"]["permissions"])

    def test_agent_sessions_requires_bearer_token(self):
        app = FastAPI()
        app.include_router(agent_router)
        with TestClient(app) as client:
            response = client.get("/api/agent/sessions")
        self.assertEqual(response.status_code, 401)


class AgentAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_rejects_user_without_agent_permission(self):
        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

        db = Session()
        with patch("utils.auth.AsyncSessionLocal", return_value=db), patch(
            "utils.auth.users.get_user_by_token",
            new=AsyncMock(return_value=SimpleNamespace(id=9, role="unrecognized")),
        ):
            with self.assertRaises(HTTPException) as raised:
                await get_agent_user_id(
                    HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid-token")
                )
        self.assertEqual(raised.exception.status_code, 403)


class TokenStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_issued_token_is_returned_once_but_only_digest_is_stored(self):
        query_result = SimpleNamespace(scalar_one_or_none=lambda: None)
        db = SimpleNamespace(
            execute=AsyncMock(return_value=query_result),
            add=Mock(),
            commit=AsyncMock(),
        )

        raw_token = await create_token(db, user_id=7)
        stored_token = db.add.call_args.args[0]

        self.assertIsInstance(stored_token, UserToken)
        self.assertNotEqual(stored_token.token, raw_token)
        self.assertEqual(stored_token.token, hashlib.sha256(raw_token.encode()).hexdigest())
        self.assertGreaterEqual(len(raw_token), 40)

    async def test_legacy_plaintext_token_is_upgraded_on_use(self):
        legacy = UserToken(
            user_id=7,
            token="legacy-token",
            expires_at=datetime.now() + timedelta(days=1),
        )
        user = SimpleNamespace(id=7, role="user")
        results = [
            SimpleNamespace(scalar_one_or_none=lambda: None),
            SimpleNamespace(scalar_one_or_none=lambda: legacy),
            SimpleNamespace(scalar_one_or_none=lambda: user),
        ]
        db = SimpleNamespace(execute=AsyncMock(side_effect=results), commit=AsyncMock())

        found = await get_user_by_token(db, "legacy-token")

        self.assertIs(found, user)
        self.assertEqual(
            legacy.token,
            hashlib.sha256("legacy-token".encode()).hexdigest(),
        )
        db.commit.assert_awaited_once()

    async def test_internal_exception_message_is_not_returned_to_client(self):
        request = SimpleNamespace(url=SimpleNamespace(path="/api/private"))
        response = await general_exception_handler(
            request,
            RuntimeError("mysql+aiomysql://operator:db-secret@private-host/news"),
        )
        body = json.loads(response.body)
        self.assertNotIn("db-secret", response.body.decode())
        self.assertEqual(body["message"], "Internal server error")
