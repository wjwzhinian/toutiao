import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from redis.exceptions import RedisError
from starlette import status

from agent.actions import AgentActionError, confirm_pending_action, list_pending_actions
from agent.service import get_session_messages, list_sessions, prepare_chat, stream_chat
from common.result import Result
from config.cache_conf import redis_client
from config.settings import get_settings
from models.users import User
from schemas.agent import AgentChatRequest
from utils.auth import get_agent_user_id
from utils.permissions import require_permission


router = APIRouter(prefix="/api/agent", tags=["agent"])

settings = get_settings()
RATE_LIMIT = settings.agent_rate_limit
RATE_WINDOW_SECONDS = settings.agent_rate_window_seconds
RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""
RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


async def enforce_rate_limit(user_id: int) -> None:
    key = f"agent:rate:{user_id}"
    try:
        count = await redis_client.eval(
            RATE_LIMIT_SCRIPT,
            1,
            key,
            RATE_WINDOW_SECONDS,
        )
    except RedisError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent 服务暂时不可用"
        ) from exc
    if int(count) > RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后再试"
        )


async def acquire_user_lock(user_id: int, run_id: str) -> bool:
    try:
        return bool(
            await redis_client.set(
                f"agent:lock:user:{user_id}",
                run_id,
                ex=settings.agent_lock_ttl_seconds,
                nx=True,
            )
        )
    except RedisError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent 服务暂时不可用"
        ) from exc


async def release_user_lock(user_id: int, run_id: str) -> None:
    try:
        await redis_client.eval(
            RELEASE_LOCK_SCRIPT,
            1,
            f"agent:lock:user:{user_id}",
            run_id,
        )
    except RedisError:
        # The lock has a TTL, so a Redis outage cannot leave it permanently held.
        pass


@router.post("/chat")
async def chat(
        request: AgentChatRequest,
        user: User = Depends(require_permission("agent:use"))
):
    user_id = user.id
    await enforce_rate_limit(user_id)
    lock_id = str(uuid.uuid4())
    if not await acquire_user_lock(user_id, lock_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="当前已有一条 Agent 请求正在处理"
        )
    try:
        prepared = await prepare_chat(
            user_id,
            request.message,
            request.session_id,
            is_admin=user.role == "admin",
        )
    except LookupError as exc:
        await release_user_lock(user_id, lock_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在或无权访问"
        ) from exc
    except Exception:
        await release_user_lock(user_id, lock_id)
        raise

    async def locked_stream():
        try:
            async for event in stream_chat(prepared):
                yield event
        finally:
            await release_user_lock(user_id, lock_id)

    return StreamingResponse(
        locked_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/actions/pending")
async def pending_actions(
    user: User = Depends(require_permission("news:manage")),
):
    # 只返回当前管理员自己的未过期提案，避免跨用户查看或确认其他人的操作。
    return Result.success(await list_pending_actions(user.id))


@router.post("/actions/{action_id}/confirm")
async def confirm_action(
    action_id: str,
    user: User = Depends(require_permission("news:manage")),
):
    # 权限、提案归属、过期时间和单次执行状态由依赖与服务层再次校验。
    try:
        return Result.success(await confirm_pending_action(user.id, action_id))
    except AgentActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc


@router.get("/sessions")
async def sessions(
        limit: int = Query(default=20, ge=1, le=50),
        user_id: int = Depends(get_agent_user_id)
):
    return Result.success(await list_sessions(user_id, limit))


@router.get("/sessions/{session_id}/messages")
async def session_messages(
        session_id: str,
        user_id: int = Depends(get_agent_user_id)
):
    try:
        messages = await get_session_messages(user_id, session_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="会话不存在或无权访问"
        ) from exc
    return Result.success({"session_id": session_id, "messages": messages})
