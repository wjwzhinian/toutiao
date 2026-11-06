from collections.abc import Callable

from fastapi import Depends, HTTPException
from starlette import status

from models.users import User
from utils.auth import get_current_user


ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    # 兼容历史 user 角色；新用户由管理员分配 viewer/editor/reviewer。
    "user": frozenset(
        {
            "profile:read",
            "profile:write",
            "favorites:read",
            "favorites:write",
            "history:read",
            "history:write",
            "agent:use",
        }
    ),
    "viewer": frozenset(
        {
            "profile:read",
            "profile:write",
            "favorites:read",
            "favorites:write",
            "history:read",
            "history:write",
            "agent:use",
        }
    ),
    "editor": frozenset(
        {
            "profile:read",
            "profile:write",
            "favorites:read",
            "favorites:write",
            "history:read",
            "history:write",
            "agent:use",
            "news:read",
            "news:edit",
        }
    ),
    "reviewer": frozenset(
        {
            "profile:read",
            "profile:write",
            "favorites:read",
            "favorites:write",
            "history:read",
            "history:write",
            "agent:use",
            "news:read",
            "news:review",
        }
    ),
    "admin": frozenset(
        {
            "profile:read",
            "profile:write",
            "favorites:read",
            "favorites:write",
            "history:read",
            "history:write",
            "agent:use",
            "users:manage_roles",
            "news:manage",
            "news:read",
            "news:edit",
            "news:review",
            "news:publish",
            "feeds:manage",
            "operations:manage",
        }
    ),
}


def permissions_for_role(role: str | None) -> tuple[str, ...]:
    return tuple(sorted(ROLE_PERMISSIONS.get(role or "", frozenset())))


def has_permission(role: str | None, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role or "", frozenset())


def require_permission(permission: str) -> Callable:
    async def permission_dependency(
        user: User = Depends(get_current_user),
    ) -> User:
        if not has_permission(user.role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user

    return permission_dependency
