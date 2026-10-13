import json
import logging

import redis.asyncio as aioredis

from src.config.settings import REDIS_URL

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None
CROSS_TTL = 3600 * 24
SCAN_CACHE_TTL = 30


async def init_redis() -> bool:
    global _redis
    if _redis is not None:
        return True

    if not REDIS_URL:
        logger.warning("REDIS_URL not configured — Redis cache and deduplication disabled")
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


def _dedup_key(side: str, symbol: str, candle_ms: int, timeframe: str = "2h") -> str:
    return f"macd:{timeframe}:{side.lower()}:{symbol.upper()}:{candle_ms}"


async def is_cross_detected(
    side: str, symbol: str, candle_ms: int, timeframe: str = "2h"
) -> bool:
    if _redis is None:
        return False
    try:
        return (await _redis.exists(_dedup_key(side, symbol, candle_ms, timeframe))) > 0
    except Exception as e:
        logger.warning("Redis dedup check error: %s", e)
        return False


async def claim_cross(
    side: str,
    symbol: str,
    candle_ms: int,
    timeframe: str = "2h",
    ttl: int = CROSS_TTL,
) -> bool:
    if _redis is None:
        return True
    try:
        res = await _redis.set(_dedup_key(side, symbol, candle_ms, timeframe), "1", nx=True, ex=ttl)
        return bool(res)
    except Exception as e:
        logger.warning("Redis claim cross error: %s", e)
        return True


async def release_cross(
    side: str, symbol: str, candle_ms: int, timeframe: str = "2h"
) -> None:
    if _redis is None:
        return
    try:
        await _redis.delete(_dedup_key(side, symbol, candle_ms, timeframe))
    except Exception as e:
        logger.warning("Redis release cross error: %s", e)


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


async def acquire_lock(lock_name: str, timeout_seconds: int = 300) -> bool:
    if _redis is None:
        return True
    try:
        res = await _redis.set(f"lock:{lock_name}", "1", nx=True, ex=timeout_seconds)
        return bool(res)
    except Exception as e:
        logger.warning("Failed to acquire Redis lock %s: %s", lock_name, e)
        return True


async def release_lock(lock_name: str) -> None:
    if _redis is None:
        return
    try:
        await _redis.delete(f"lock:{lock_name}")
    except Exception as e:
        logger.warning("Failed to release Redis lock %s: %s", lock_name, e)
