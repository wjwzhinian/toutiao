"""Validated application settings with secret-safe representations."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlparse

from dotenv import load_dotenv


BACKEND_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, default).split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    app_env: str
    host: str
    port: int
    database_url: str = field(repr=False)
    sql_echo: bool
    redis_url: str = field(repr=False)
    redis_max_connections: int
    ai_api_base_url: str
    ai_api_key: str = field(repr=False)
    ai_model: str
    ai_provider_name: str
    ai_timeout_seconds: float
    ai_max_retries: int
    app_timezone: str
    frontend_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    agent_rate_limit: int
    agent_rate_window_seconds: int
    agent_lock_ttl_seconds: int
    auto_pipeline_enabled: bool = True
    auto_pipeline_interval_seconds: int = 60
    # 影子模式只记录自动发布候选，不会把稿件设为已发布。
    auto_publish_mode: str = "shadow"
    # JWT 配置：密钥只从环境变量读取，repr 永远不会输出密钥。
    jwt_secret_key: str = field(default="", repr=False)
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_refresh_token_expire_days: int = 14
    jwt_refresh_cookie_name: str = "news_refresh_token"
    jwt_cookie_secure: bool = False
    jwt_cookie_samesite: str = "lax"
    auth_login_rate_limit: int = 10
    auth_login_rate_window_seconds: int = 300
    # Qdrant 为可选语义索引；向量模型单独配置，避免误把聊天模型密钥用于 Embedding。
    qdrant_enabled: bool = False
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str = field(default="", repr=False)
    qdrant_collection: str = "news_evidence_v1"
    embedding_api_base_url: str = ""
    embedding_api_key: str = field(default="", repr=False)
    embedding_model: str = ""
    embedding_dimension: int = 1536
    embedding_version: str = "embedding-v1"
    vector_api_timeout_seconds: float = 30.0
    vector_index_batch_size: int = 50
    vector_search_limit: int = 50

    def __repr__(self) -> str:
        return f"Settings(app_env={self.app_env!r}, secrets=<redacted>)"

    @property
    def is_production(self) -> bool:
        return self.app_env in {"prod", "production"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        host = os.getenv("REDIS_HOST", "127.0.0.1").strip()
        port = _int("REDIS_PORT", 6379, 1, 65535)
        database = _int("REDIS_DB", 0, 0, 15)
        password = os.getenv("REDIS_PASSWORD", "")
        auth = f":{quote(password, safe='')}@" if password else ""
        redis_url = f"redis://{auth}{host}:{port}/{database}"

    try:
        ai_timeout = float(os.getenv("AI_TIMEOUT_SECONDS", "45"))
    except ValueError as exc:
        raise ValueError("AI_TIMEOUT_SECONDS must be a number") from exc
    if not 1 <= ai_timeout <= 300:
        raise ValueError("AI_TIMEOUT_SECONDS must be between 1 and 300")

    return Settings(
        app_env=os.getenv("APP_ENV", "development").strip().lower(),
        host=os.getenv("HOST", "127.0.0.1").strip(),
        port=_int("PORT", 8000, 1, 65535),
        database_url=os.getenv(
            "DATABASE_URL",
            "mysql+aiomysql://user:password@127.0.0.1:3306/news_app?charset=utf8mb4",
        ).strip(),
        sql_echo=_bool("SQL_ECHO", False),
        redis_url=redis_url,
        redis_max_connections=_int("REDIS_MAX_CONNECTIONS", 50, 1, 1000),
        ai_api_base_url=os.getenv("AI_API_BASE_URL", "https://api.deepseek.com").strip().rstrip("/"),
        ai_api_key=os.getenv("AI_API_KEY", "").strip(),
        ai_model=os.getenv("AI_MODEL", "deepseek-flash").strip(),
        ai_provider_name=os.getenv("AI_PROVIDER_NAME", "deepseek").strip(),
        ai_timeout_seconds=ai_timeout,
        ai_max_retries=_int("AI_MAX_RETRIES", 2, 0, 5),
        app_timezone=os.getenv("APP_TIMEZONE", "Asia/Shanghai").strip(),
        frontend_origins=_csv(
            "FRONTEND_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000",
        ),
        allowed_hosts=_csv("ALLOWED_HOSTS", "127.0.0.1,localhost"),
        agent_rate_limit=_int("AGENT_RATE_LIMIT", 10, 1, 10000),
        agent_rate_window_seconds=_int("AGENT_RATE_WINDOW_SECONDS", 60, 1, 86400),
        agent_lock_ttl_seconds=_int("AGENT_LOCK_TTL_SECONDS", 240, 30, 86400),
        auto_pipeline_enabled=_bool("AUTO_PIPELINE_ENABLED", True),
        auto_pipeline_interval_seconds=_int("AUTO_PIPELINE_INTERVAL_SECONDS", 60, 15, 3600),
        auto_publish_mode=os.getenv("AUTO_PUBLISH_MODE", "shadow").strip().lower(),
        jwt_secret_key=os.getenv("JWT_SECRET_KEY", "").strip(),
        jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256").strip().upper(),
        jwt_access_token_expire_minutes=_int("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", 30, 5, 120),
        jwt_refresh_token_expire_days=_int("JWT_REFRESH_TOKEN_EXPIRE_DAYS", 14, 1, 90),
        jwt_refresh_cookie_name=os.getenv("JWT_REFRESH_COOKIE_NAME", "news_refresh_token").strip(),
        jwt_cookie_secure=_bool("JWT_COOKIE_SECURE", False),
        jwt_cookie_samesite=os.getenv("JWT_COOKIE_SAMESITE", "lax").strip().lower(),
        auth_login_rate_limit=_int("AUTH_LOGIN_RATE_LIMIT", 10, 1, 1000),
        auth_login_rate_window_seconds=_int("AUTH_LOGIN_RATE_WINDOW_SECONDS", 300, 10, 86400),
        qdrant_enabled=_bool("QDRANT_ENABLED", False),
        qdrant_url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333").strip().rstrip("/"),
        qdrant_api_key=os.getenv("QDRANT_API_KEY", "").strip(),
        qdrant_collection=os.getenv("QDRANT_COLLECTION", "news_evidence_v1").strip(),
        embedding_api_base_url=os.getenv("EMBEDDING_API_BASE_URL", "").strip().rstrip("/"),
        embedding_api_key=os.getenv("EMBEDDING_API_KEY", "").strip(),
        embedding_model=os.getenv("EMBEDDING_MODEL", "").strip(),
        embedding_dimension=_int("EMBEDDING_DIMENSION", 1536, 1, 65536),
        embedding_version=os.getenv("EMBEDDING_VERSION", "embedding-v1").strip(),
        vector_api_timeout_seconds=_float("VECTOR_API_TIMEOUT_SECONDS", 30.0, 1.0, 300.0),
        vector_index_batch_size=_int("VECTOR_INDEX_BATCH_SIZE", 50, 1, 500),
        vector_search_limit=_int("VECTOR_SEARCH_LIMIT", 50, 1, 200),
    )


def validate_settings(settings: Settings | None = None) -> None:
    """Fail closed on unsafe production configuration without echoing values."""
    settings = settings or get_settings()
    if settings.app_env not in {"development", "local", "test", "prod", "production"}:
        raise ValueError("APP_ENV must be development, local, test, or production")
    if settings.auto_publish_mode not in {"shadow", "enforce"}:
        raise ValueError("AUTO_PUBLISH_MODE must be shadow or enforce")
    if settings.jwt_algorithm != "HS256":
        raise ValueError("JWT_ALGORITHM must be HS256 until an asymmetric key provider is configured")
    jwt_key_marker = settings.jwt_secret_key.lower()
    if not settings.jwt_secret_key or len(settings.jwt_secret_key) < 32 or any(
        marker in jwt_key_marker for marker in ("replace-", "your-", "change-me", "placeholder", "generate-")
    ):
        raise ValueError("JWT_SECRET_KEY must contain at least 32 characters")
    if settings.jwt_cookie_samesite not in {"lax", "strict", "none"}:
        raise ValueError("JWT_COOKIE_SAMESITE must be lax, strict, or none")
    if not settings.jwt_refresh_cookie_name or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for char in settings.jwt_refresh_cookie_name
    ):
        raise ValueError("JWT_REFRESH_COOKIE_NAME contains invalid characters")
    if settings.jwt_cookie_samesite == "none" and not settings.jwt_cookie_secure:
        raise ValueError("JWT_COOKIE_SECURE must be enabled when SameSite=None")
    if not settings.allowed_hosts:
        raise ValueError("ALLOWED_HOSTS must contain at least one host")

    if not 1 <= settings.vector_api_timeout_seconds <= 300:
        raise ValueError("VECTOR_API_TIMEOUT_SECONDS must be between 1 and 300")
    if settings.qdrant_enabled:
        qdrant_url = urlparse(settings.qdrant_url)
        embedding_url = urlparse(settings.embedding_api_base_url)
        if (
            qdrant_url.scheme not in {"http", "https"}
            or not qdrant_url.hostname
            or qdrant_url.username
            or qdrant_url.password
            or qdrant_url.query
            or qdrant_url.fragment
        ):
            raise ValueError("QDRANT_URL must be an HTTP(S) URL")
        if not settings.qdrant_collection or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for char in settings.qdrant_collection
        ):
            raise ValueError("QDRANT_COLLECTION contains invalid characters")
        if (
            embedding_url.scheme not in {"http", "https"}
            or not embedding_url.hostname
            or embedding_url.username
            or embedding_url.password
            or embedding_url.query
            or embedding_url.fragment
        ):
            raise ValueError("EMBEDDING_API_BASE_URL must be an HTTP(S) URL when Qdrant is enabled")
        embedding_key = settings.embedding_api_key.lower()
        if not settings.embedding_api_key or any(
            marker in embedding_key for marker in ("replace-", "your-", "change-me", "placeholder")
        ) or not settings.embedding_model:
            raise ValueError("EMBEDDING_API_KEY and EMBEDDING_MODEL are required when Qdrant is enabled")
        if not settings.embedding_version:
            raise ValueError("EMBEDDING_VERSION must not be empty")
        if settings.is_production and embedding_url.scheme != "https":
            raise ValueError("EMBEDDING_API_BASE_URL must use HTTPS in production")

    if settings.is_production:
        import importlib.util
        if importlib.util.find_spec("jwt") is None:
            raise ValueError("PyJWT must be installed in production")
        if not settings.jwt_cookie_secure:
            raise ValueError("JWT_COOKIE_SECURE must be enabled in production")
        placeholder_key = settings.ai_api_key.lower()
        if not settings.ai_api_key or any(
            marker in placeholder_key
            for marker in ("replace-", "your-", "change-me", "placeholder")
        ):
            raise ValueError("AI_API_KEY must be configured in production")
        if "*" in settings.allowed_hosts:
            raise ValueError("ALLOWED_HOSTS cannot contain a wildcard in production")
        parsed_origins = [urlparse(origin) for origin in settings.frontend_origins]
        if not parsed_origins or any(
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
            for origin in parsed_origins
        ):
            raise ValueError("FRONTEND_ORIGINS must use HTTPS in production")
        provider_url = urlparse(settings.ai_api_base_url)
        if provider_url.scheme != "https" or not provider_url.hostname or provider_url.username or provider_url.password:
            raise ValueError("AI_API_BASE_URL must use HTTPS in production")
        database_url = urlparse(settings.database_url)
        if (
            not database_url.hostname
            or not database_url.username
            or not database_url.password
            or database_url.scheme not in {"mysql+aiomysql", "mysql+asyncmy"}
        ):
            raise ValueError("DATABASE_URL must contain a deployment database account")
        if database_url.username.lower() == "root":
            raise ValueError("DATABASE_URL must use a least-privilege database account")
        if settings.sql_echo:
            raise ValueError("SQL_ECHO must be disabled in production")
        if "user:password@" in settings.database_url.lower():
            raise ValueError("DATABASE_URL must use deployment credentials in production")
