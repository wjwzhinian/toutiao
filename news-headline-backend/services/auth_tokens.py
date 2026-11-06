"""业务层 Token Pair 组装逻辑。"""

from sqlalchemy.ext.asyncio import AsyncSession

from crud.auth_sessions import create_refresh_session
from models.users import User
from utils.jwt_tokens import issue_access_token


async def issue_token_pair(db: AsyncSession, user: User) -> tuple[str, str]:
    refresh_token, session = await create_refresh_session(db, user)
    access_token = issue_access_token(user.id, user.role, session.session_id)
    return access_token, refresh_token

