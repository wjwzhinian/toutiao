"""JWT 签发与 Refresh Token 摘要工具。"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

try:
    import jwt as pyjwt
except ImportError:  # pragma: no cover - 本地无网络安装依赖时的轻量回退
    pyjwt = None

from config.settings import get_settings


def _b64(value: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _fallback_encode(payload: dict, secret: str) -> str:
    """仅使用标准库实现 HS256，保证精简开发环境也能启动。"""
    import base64
    import hmac
    import json
    header = {"alg": "HS256", "typ": "JWT"}
    body = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}.{_b64(json.dumps(payload, default=lambda x: int(x.timestamp()) if hasattr(x, 'timestamp') else x, separators=(',', ':')).encode())}"
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(signature)}"


def _fallback_decode(token: str, secret: str) -> dict:
    import base64
    import hmac
    import json
    import time
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("malformed jwt")
    body, signature = ".".join(parts[:2]), parts[2]
    expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise ValueError("invalid signature")
    decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    header = json.loads(decode(parts[0]))
    payload = json.loads(decode(parts[1]))
    required = {"sub", "role", "iat", "exp", "jti", "sid", "typ"}
    if header.get("alg") != "HS256" or not required.issubset(payload):
        raise ValueError("missing jwt claims")
    if not isinstance(payload["iat"], int) or not isinstance(payload["exp"], int):
        raise ValueError("invalid jwt timestamps")
    if payload["exp"] <= time.time() or payload["iat"] > time.time() + 60:
        raise ValueError("invalid jwt")
    return payload


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def issue_access_token(user_id: int, role: str, session_id: str) -> str:
    """签发短效 JWT；身份和角色会在每次请求时再与数据库核对。"""
    settings = get_settings()
    if settings.is_production and pyjwt is None:
        raise RuntimeError("PyJWT must be installed in production")
    now = _utcnow()
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_token_expire_minutes),
        "jti": str(uuid4()),
        "sid": session_id,
        "typ": "access",
    }
    if pyjwt:
        return pyjwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return _fallback_encode(payload, settings.jwt_secret_key)


def decode_access_token(token: str) -> dict | None:
    """严格校验签名、算法、用途和标准时间声明。"""
    settings = get_settings()
    if settings.is_production and pyjwt is None:
        raise RuntimeError("PyJWT must be installed in production")
    try:
        if pyjwt:
            payload = pyjwt.decode(
                token,
                settings.jwt_secret_key,
                algorithms=[settings.jwt_algorithm],
                options={"require": ["sub", "role", "iat", "exp", "jti", "sid", "typ"]},
            )
        else:
            payload = _fallback_decode(token, settings.jwt_secret_key)
    except Exception:
        return None
    if payload.get("typ") != "access":
        return None
    try:
        if int(payload["sub"]) <= 0:
            return None
    except (TypeError, ValueError):
        return None
    return payload


def new_refresh_token() -> str:
    """Refresh Token 使用高熵不透明随机值，避免把长期凭据放进 JWT。"""
    return secrets.token_urlsafe(48)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def refresh_expiry() -> datetime:
    return _utcnow() + timedelta(days=get_settings().jwt_refresh_token_expire_days)

