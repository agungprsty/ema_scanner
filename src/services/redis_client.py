import json
import logging
import secrets

import redis.asyncio as aioredis

from src.config.settings import REDIS_URL

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None
SCAN_CACHE_TTL = 30
LOCAL_LOCK_TOKEN = "local"
_RELEASE_LOCK_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
)


async def init_redis() -> bool:
    global _redis
    if _redis is not None:
        return True

    if not REDIS_URL:
        logger.warning("REDIS_URL not configured — Redis cache and locks disabled")
        return False

    try:
        _redis = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
        )
        await _redis.ping()
        logger.info("Redis connected successfully")
        return True
    except Exception as e:
        logger.warning("Failed to connect to Redis: %s", e)
        _redis = None
        return False


async def close_redis() -> None:
    global _redis
    if _redis:
        try:
            await _redis.close()
        except Exception as e:
            logger.debug("Error closing Redis connection: %s", e)
        _redis = None
        logger.info("Redis connection closed")


async def is_redis_connected() -> bool:
    if _redis is None:
        return False
    try:
        return await _redis.ping()
    except Exception:
        return False


async def get_cached_json(key: str) -> dict | None:
    if _redis is None:
        return None
    try:
        data = await _redis.get(key)
        if data:
            return json.loads(data)
    except Exception as e:
        logger.warning("Redis get error for key %s: %s", key, e)
    return None


async def set_cached_json(key: str, data: dict, ttl: int = SCAN_CACHE_TTL) -> None:
    if _redis is None:
        return
    try:
        await _redis.setex(key, ttl, json.dumps(data, default=str))
    except Exception as e:
        logger.warning("Redis set error for key %s: %s", key, e)


async def acquire_lock(lock_name: str, timeout_seconds: int = 300) -> str | None:
    if _redis is None:
        return LOCAL_LOCK_TOKEN
    token = secrets.token_hex(16)
    try:
        res = await _redis.set(f"lock:{lock_name}", token, nx=True, ex=timeout_seconds)
        return token if res else None
    except Exception as e:
        logger.warning("Failed to acquire Redis lock %s: %s", lock_name, e)
        return LOCAL_LOCK_TOKEN


async def release_lock(lock_name: str, token: str | None) -> None:
    if _redis is None or not token or token == LOCAL_LOCK_TOKEN:
        return
    try:
        await _redis.eval(_RELEASE_LOCK_SCRIPT, 1, f"lock:{lock_name}", token)
    except Exception as e:
        logger.warning("Failed to release Redis lock %s: %s", lock_name, e)
