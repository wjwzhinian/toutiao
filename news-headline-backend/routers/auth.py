"""JWT 登录、刷新、注销和当前用户接口。"""

import hashlib
import logging

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from common.result import Result
from config.db_confing import get_db
from config.settings import get_settings
from config.cache_conf import redis_client
from crud import users
from crud.auth_sessions import revoke_refresh_session, rotate_refresh_session
from models.users import User
from schemas.users import UserInfoResponse, UserRequest
from services.auth_tokens import issue_token_pair
from utils.audit import record_audit_event
from utils.auth import get_current_user
from utils.jwt_tokens import issue_access_token
from utils.permissions import permissions_for_role

router = APIRouter(prefix="/api/auth", tags=["authentication"])
logger = logging.getLogger(__name__)


async def _check_login_rate_limit(request: Request, username: str) -> None:
    """基于 Redis 限制单一账号/IP 的登录尝试，Redis 故障时拒绝登录以保护认证入口。"""
    settings = get_settings()
    client_host = request.client.host if request.client else "unknown"
    account_digest = hashlib.sha256(username.strip().lower().encode("utf-8")).hexdigest()[:24]
    key = f"auth:login:{client_host}:{account_digest}"
    try:
        attempts = await redis_client.incr(key)
        if attempts == 1:
            await redis_client.expire(key, settings.auth_login_rate_window_seconds)
    except RedisError as exc:
        logger.error("Authentication rate limiter unavailable: error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="认证服务暂不可用") from exc
    if attempts > settings.auth_login_rate_limit:
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")


def _validate_cookie_origin(request: Request) -> None:
    """校验浏览器携带 Cookie 的变更请求来源，降低 CSRF 风险。"""
    origin = request.headers.get("origin")
    if not origin or origin not in get_settings().frontend_origins:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request origin")


def _set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.jwt_refresh_cookie_name,
        value=token,
        max_age=settings.jwt_refresh_token_expire_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.jwt_cookie_secure,
        samesite=settings.jwt_cookie_samesite,
        path="/api/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.jwt_refresh_cookie_name,
        httponly=True,
        secure=settings.jwt_cookie_secure,
        samesite=settings.jwt_cookie_samesite,
        path="/api/auth",
    )


@router.post("/login")
async def login(
    credentials: UserRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    await _check_login_rate_limit(request, credentials.username)
    user = await users.authenticate_user(db, credentials.username, credentials.password)
    if not user:
        await record_audit_event(
            db, actor_id=None, action="auth.login", resource_type="user", outcome="denied"
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    if not getattr(user, "is_active", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已停用")

    access_token, refresh_token = await issue_token_pair(db, user)
    _set_refresh_cookie(response, refresh_token)
    await record_audit_event(
        db, actor_id=user.id, action="auth.login", resource_type="user", resource_id=str(user.id)
    )
    return Result.success(
        {
            "token": access_token,
            "tokenType": "Bearer",
            "expiresIn": get_settings().jwt_access_token_expire_minutes * 60,
            "userInfo": UserInfoResponse.model_validate(user),
        },
        "登录成功",
    )


@router.post("/refresh")
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    refresh_token: str | None = Cookie(default=None, alias=get_settings().jwt_refresh_cookie_name),
):
    _validate_cookie_origin(request)
    rotated = await rotate_refresh_session(db, refresh_token)
    if not rotated:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh Token 无效或已过期")

    user, new_refresh_token, session = rotated
    access_token = issue_access_token(user.id, user.role, session.session_id)
    _set_refresh_cookie(response, new_refresh_token)
    return Result.success(
        {
            "token": access_token,
            "tokenType": "Bearer",
            "expiresIn": get_settings().jwt_access_token_expire_minutes * 60,
            "userInfo": UserInfoResponse.model_validate(user),
        },
        "Token 已刷新",
    )


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    refresh_token: str | None = Cookie(default=None, alias=get_settings().jwt_refresh_cookie_name),
):
    _validate_cookie_origin(request)
    revoked = await revoke_refresh_session(db, refresh_token)
    await record_audit_event(
        db,
        actor_id=None,
        action="auth.logout",
        resource_type="session",
        outcome="success" if revoked else "no_active_session",
    )
    _clear_refresh_cookie(response)
    return Result.success(message="已退出登录")


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return Result.success(
        {
            "userInfo": UserInfoResponse.model_validate(user),
            "permissions": permissions_for_role(user.role),
        }
    )

