import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from models.users import AuthRefreshSession, User, UserToken
from schemas.users import UserRequest, UserUpdateRequest
from utils import security


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def get_user_by_username(db: AsyncSession, username: str):
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: int):
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def create_user(db: AsyncSession, user_data: UserRequest):
    user = User(
        username=user_data.username,
        password=security.get_hash_password(user_data.password),
        role="user",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def create_token(db: AsyncSession, user_id: int):
    token = secrets.token_urlsafe(32)
    token_digest = _hash_token(token)
    expires_at = datetime.now() + timedelta(days=7)
    result = await db.execute(select(UserToken).where(UserToken.user_id == user_id))
    user_token = result.scalar_one_or_none()
    if user_token:
        user_token.token = token_digest
        user_token.expires_at = expires_at
    else:
        db.add(UserToken(user_id=user_id, token=token_digest, expires_at=expires_at))
    await db.commit()
    return token


async def authenticate_user(db: AsyncSession, username: str, password: str):
    user = await get_user_by_username(db, username)
    if not user or not getattr(user, "is_active", True) or not security.verify_password(password, user.password):
        return None
    return user


async def get_user_by_token(db: AsyncSession, token: str):
    if not token or len(token) > 512:
        return None

    token_digest = _hash_token(token)
    result = await db.execute(select(UserToken).where(UserToken.token == token_digest))
    db_token = result.scalar_one_or_none()

    if not db_token:
        legacy_result = await db.execute(select(UserToken).where(UserToken.token == token))
        db_token = legacy_result.scalar_one_or_none()
        if db_token and db_token.expires_at > datetime.now():
            db_token.token = token_digest
            await db.commit()

    if not db_token or db_token.expires_at <= datetime.now():
        return None
    user_result = await db.execute(select(User).where(User.id == db_token.user_id))
    return user_result.scalar_one_or_none()


async def update_user(db: AsyncSession, username: str, user_data: UserUpdateRequest):
    values = user_data.model_dump(exclude_unset=True, exclude_none=True)
    result = await db.execute(update(User).where(User.username == username).values(**values))
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return await get_user_by_username(db, username)


async def update_user_role(db: AsyncSession, user_id: int, role: str):
    result = await db.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if user.role == "admin" and role != "admin":
        admins_result = await db.execute(
            select(User.id).where(User.role == "admin", User.is_active.is_(True)).with_for_update()
        )
        if len(admins_result.scalars().all()) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="At least one administrator must remain",
            )

    user.role = role
    await db.commit()
    await db.refresh(user)
    return user


async def change_password(
    db: AsyncSession,
    user: User,
    old_password: str,
    new_password: str,
):
    if not security.verify_password(old_password, user.password):
        return False

    user.password = security.get_hash_password(new_password)
    token_result = await db.execute(select(UserToken).where(UserToken.user_id == user.id))
    for token in token_result.scalars().all():
        await db.delete(token)
    sessions_result = await db.execute(
        select(AuthRefreshSession).where(
            AuthRefreshSession.user_id == user.id,
            AuthRefreshSession.revoked_at.is_(None),
        ).with_for_update()
    )
    revoked_at = datetime.utcnow()
    for session in sessions_result.scalars().all():
        session.revoked_at = revoked_at
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return True
