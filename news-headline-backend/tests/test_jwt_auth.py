import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers.auth import router as auth_router
from config.db_confing import get_db
from crud.auth_sessions import get_user_for_access_token, revoke_refresh_session, rotate_refresh_session
from utils.jwt_tokens import (
    _fallback_encode,
    decode_access_token,
    issue_access_token,
    token_digest,
)


class JwtTokenTests(unittest.TestCase):
    def test_access_token_contains_required_claims_and_rejects_tampering(self):
        settings = SimpleNamespace(
            jwt_secret_key="a-test-secret-with-more-than-32-characters",
            jwt_algorithm="HS256",
            jwt_access_token_expire_minutes=30,
            is_production=False,
        )
        with patch("utils.jwt_tokens.get_settings", return_value=settings):
            token = issue_access_token(17, "reviewer", "session-1")
            payload = decode_access_token(token)

        self.assertEqual(payload["sub"], "17")
        self.assertEqual(payload["role"], "reviewer")
        self.assertEqual(payload["sid"], "session-1")
        self.assertEqual(payload["typ"], "access")
        self.assertTrue(payload["jti"])
        self.assertIsNone(decode_access_token(token[:-1] + ("A" if token[-1] != "A" else "B")))

    def test_expired_and_wrong_type_tokens_are_rejected(self):
        settings = SimpleNamespace(
            jwt_secret_key="a-test-secret-with-more-than-32-characters",
            jwt_algorithm="HS256",
            jwt_access_token_expire_minutes=30,
            is_production=False,
        )
        now = int(datetime.now(timezone.utc).timestamp())
        required = {"sub": "3", "role": "viewer", "iat": now - 100, "jti": "j", "sid": "s"}
        with patch("utils.jwt_tokens.get_settings", return_value=settings):
            expired = _fallback_encode({**required, "typ": "access", "exp": now - 1}, settings.jwt_secret_key)
            refresh_as_access = _fallback_encode({**required, "typ": "refresh", "exp": now + 100}, settings.jwt_secret_key)
            self.assertIsNone(decode_access_token(expired))
            self.assertIsNone(decode_access_token(refresh_as_access))


class JwtSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_access_token_reads_current_role_and_rejects_disabled_user(self):
        active_session = SimpleNamespace(session_id="session-1", user_id=17)
        active_user = SimpleNamespace(id=17, role="editor", is_active=True)
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[
                SimpleNamespace(scalar_one_or_none=lambda: active_session),
                SimpleNamespace(scalar_one_or_none=lambda: active_user),
            ])
        )
        settings = SimpleNamespace(
            jwt_secret_key="a-test-secret-with-more-than-32-characters",
            jwt_algorithm="HS256",
            jwt_access_token_expire_minutes=30,
            is_production=False,
        )
        with patch("utils.jwt_tokens.get_settings", return_value=settings):
            token = issue_access_token(17, "viewer", "session-1")
            user = await get_user_for_access_token(db, token)
        self.assertEqual(user.role, "editor")

        disabled_db = SimpleNamespace(
            execute=AsyncMock(side_effect=[
                SimpleNamespace(scalar_one_or_none=lambda: active_session),
                SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(id=17, role="admin", is_active=False)),
            ])
        )
        with patch("utils.jwt_tokens.get_settings", return_value=settings):
            self.assertIsNone(await get_user_for_access_token(disabled_db, token))

    async def test_refresh_rotation_replaces_digest_and_rejects_disabled_user(self):
        old = SimpleNamespace(
            token_hash=token_digest("old-refresh"),
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=2),
            revoked_at=None,
            user_id=5,
        )
        user = SimpleNamespace(id=5, role="admin", is_active=True)
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[
                SimpleNamespace(scalar_one_or_none=lambda: old),
                SimpleNamespace(scalar_one_or_none=lambda: user),
            ]),
            flush=AsyncMock(),
        )
        rotated = await rotate_refresh_session(db, "old-refresh")
        self.assertIsNotNone(rotated)
        self.assertNotEqual(old.token_hash, token_digest("old-refresh"))
        self.assertEqual(old.token_hash, token_digest(rotated[1]))
        self.assertEqual(old.revoked_at, None)
        self.assertGreater(old.expires_at, datetime.now(timezone.utc).replace(tzinfo=None))

    async def test_refresh_token_for_disabled_user_is_revoked(self):
        old = SimpleNamespace(
            token_hash=token_digest("disabled-refresh"),
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=2),
            revoked_at=None,
            user_id=5,
        )
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[
                SimpleNamespace(scalar_one_or_none=lambda: old),
                SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(id=5, is_active=False)),
            ]),
            flush=AsyncMock(),
        )
        self.assertIsNone(await rotate_refresh_session(db, "disabled-refresh"))
        self.assertIsNotNone(old.revoked_at)

    async def test_logout_revokes_refresh_session_and_replay_fails(self):
        session = SimpleNamespace(
            token_hash=token_digest("logout-refresh"),
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=2),
            revoked_at=None,
            user_id=5,
        )
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[
                SimpleNamespace(scalar_one_or_none=lambda: session),
                SimpleNamespace(scalar_one_or_none=lambda: session),
            ]),
            flush=AsyncMock(),
        )
        self.assertTrue(await revoke_refresh_session(db, "logout-refresh"))
        self.assertIsNotNone(session.revoked_at)
        self.assertIsNone(await rotate_refresh_session(db, "logout-refresh"))


class JwtRouteTests(unittest.TestCase):
    def test_wrong_password_returns_401(self):
        app = FastAPI()
        app.include_router(auth_router)

        async def override_db():
            yield SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

        app.dependency_overrides[get_db] = override_db
        with patch("routers.auth._check_login_rate_limit", new=AsyncMock()), patch(
            "routers.auth.users.authenticate_user", new=AsyncMock(return_value=None)
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/api/auth/login",
                    json={"username": "editor1", "password": "wrong"},
                )
        self.assertEqual(response.status_code, 401)

    def test_login_sets_refresh_cookie_as_http_only_and_returns_short_access_token(self):
        user = SimpleNamespace(
            id=12,
            username="editor1",
            role="editor",
            is_active=True,
            nickname=None,
            avatar=None,
            gender="unknown",
            bio=None,
        )
        app = FastAPI()
        app.include_router(auth_router)

        async def override_db():
            yield object()

        app.dependency_overrides[get_db] = override_db
        with patch("routers.auth._check_login_rate_limit", new=AsyncMock()), patch(
            "routers.auth.users.authenticate_user", new=AsyncMock(return_value=user)
        ), patch(
            "routers.auth.issue_token_pair", new=AsyncMock(return_value=("access.jwt.token", "refresh-secret"))
        ), patch("routers.auth.record_audit_event", new=AsyncMock()):
            with TestClient(app) as client:
                response = client.post(
                    "/api/auth/login",
                    json={"username": "editor1", "password": "password"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["token"], "access.jwt.token")
        self.assertEqual(response.json()["data"]["expiresIn"], 1800)
        self.assertIn("httponly", response.headers["set-cookie"].lower())
        self.assertIn("path=/api/auth", response.headers["set-cookie"].lower())

    def test_me_requires_bearer_token(self):
        app = FastAPI()
        app.include_router(auth_router)
        with TestClient(app) as client:
            response = client.get("/api/auth/me")
        self.assertEqual(response.status_code, 401)

