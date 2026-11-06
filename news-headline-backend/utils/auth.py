from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from config.db_confing import AsyncSessionLocal, get_db
from crud import users
from crud.auth_sessions import get_user_for_access_token
from models.users import User


bearer_scheme = HTTPBearer(auto_error=False)


def _extract_bearer_token(authorization: str | None) -> str:
    parts = authorization.split() if authorization else []
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid Bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return parts[1]


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    authorization = (
        f"{credentials.scheme} {credentials.credentials}" if credentials else None
    )
    token = _extract_bearer_token(authorization)
    user = await get_user_for_access_token(db, token)
    # 兼容迁移窗口内尚未过期的历史 opaque token。
    if not user:
        user = await users.get_user_by_token(db, token)
    if user and not getattr(user, "is_active", True):
        user = None
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def get_agent_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> int:
    """Authenticate Agent requests without holding a database session while streaming."""
    authorization = (
        f"{credentials.scheme} {credentials.credentials}" if credentials else None
    )
    token = _extract_bearer_token(authorization)
    async with AsyncSessionLocal() as db:
        user = await get_user_for_access_token(db, token)
        if not user:
            user = await users.get_user_by_token(db, token)
        if user and not getattr(user, "is_active", True):
            user = None
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        from utils.permissions import has_permission

        if not has_permission(user.role, "agent:use"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user.id
