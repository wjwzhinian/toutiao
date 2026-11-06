import json
import logging
from typing import Any

import redis.asyncio as redis

from config.settings import get_settings


logger = logging.getLogger(__name__)
settings = get_settings()
redis_client = redis.from_url(
    settings.redis_url,
    decode_responses=True,
    max_connections=settings.redis_max_connections,
    socket_connect_timeout=5,
    socket_timeout=10,
)


async def get_cache(key: str):
    try:
        return await redis_client.get(key)
    except Exception as exc:
        logger.warning("Redis cache read failed: error_type=%s", type(exc).__name__)
        return None


async def get_json_cache(key: str):
    try:
        data = await redis_client.get(key)
        return json.loads(data) if data else None
    except Exception as exc:
        logger.warning("Redis JSON cache read failed: error_type=%s", type(exc).__name__)
        return None


async def set_cache(key: str, value: Any, expire: int = 3600):
    try:
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        await redis_client.setex(key, expire, value)
        return True
    except Exception as exc:
        logger.warning("Redis cache write failed: error_type=%s", type(exc).__name__)
        return False
