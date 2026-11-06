"""Refresh Token 会话的创建、轮换和撤销。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.users import AuthRefreshSession, User
from utils.jwt_tokens import decode_access_token, new_refresh_token, refresh_expiry, token_digest


def _utcnow_naive() -> datetime:
    # 项目现有 MySQL 日期列按 UTC 保存为无时区 DATETIME。
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def get_user_for_access_token(db: AsyncSession, raw_token: str) -> User | None:
    """验证 JWT 和对应会话，再读取数据库中的最新角色与账号状态。"""
    payload = decode_access_token(raw_token)
    if not payload:
        return None
    session_result = await db.execute(
        select(AuthRefreshSession).where(
            AuthRefreshSession.session_id == payload["sid"],
            AuthRefreshSession.user_id == int(payload["sub"]),
            AuthRefreshSession.revoked_at.is_(None),
            AuthRefreshSession.expires_at > _utcnow_naive(),
        )
    )
    if not session_result.scalar_one_or_none():
        return None
    user_result = await db.execute(select(User).where(User.id == int(payload["sub"])))
    user = user_result.scalar_one_or_none()
    if not user or not getattr(user, "is_active", True):
        return None
    return user


async def create_refresh_session(db: AsyncSession, user: User) -> tuple[str, AuthRefreshSession]:
    """创建一个设备会话，数据库中不落明文 refresh 凭据。"""
    raw_token = new_refresh_token()
    session = AuthRefreshSession(
        session_id=str(uuid4()),
        user_id=user.id,
        token_hash=token_digest(raw_token),
        expires_at=refresh_expiry().replace(tzinfo=None),
    )
    db.add(session)
    await db.flush()
    return raw_token, session


async def rotate_refresh_session(
    db: AsyncSession, raw_token: str | None
) -> tuple[User, str, AuthRefreshSession] | None:
    """校验并原子轮换 Refresh Token；过期、撤销或禁用账号均拒绝。"""
    if not raw_token or len(raw_token) > 256:
        return None
    result = await db.execute(
        select(AuthRefreshSession)
        .where(AuthRefreshSession.token_hash == token_digest(raw_token))
        .with_for_update()
    )
    old_session = result.scalar_one_or_none()
    now = _utcnow_naive()
    if not old_session or old_session.revoked_at or old_session.expires_at <= now:
        return None

    user_result = await db.execute(select(User).where(User.id == old_session.user_id))
    user = user_result.scalar_one_or_none()
    if not user or not getattr(user, "is_active", True):
        old_session.revoked_at = now
        await db.flush()
        return None

    new_token = new_refresh_token()
    old_session.token_hash = token_digest(new_token)
    old_session.expires_at = refresh_expiry().replace(tzinfo=None)
    await db.flush()
    return user, new_token, old_session


async def revoke_refresh_session(db: AsyncSession, raw_token: str | None) -> bool:
    """撤销浏览器 Cookie 对应的会话。"""
    if not raw_token or len(raw_token) > 256:
        return False
    result = await db.execute(
        select(AuthRefreshSession)
        .where(AuthRefreshSession.token_hash == token_digest(raw_token))
        .with_for_update()
    )
    session = result.scalar_one_or_none()
    if not session or session.revoked_at:
        return False
    session.revoked_at = _utcnow_naive()
    await db.flush()
    return True
