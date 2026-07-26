import json
import logging

import redis
import redis.asyncio as aioredis

from src.config.settings import REDIS_URL

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None
_redis_sync: redis.Redis | None = None
CROSS_TTL = 3600
CACHE_TTL = 15
def _get_sync_redis() -> redis.Redis | None:
    global _redis_sync
    if _redis_sync is not None:
        return _redis_sync
    if not REDIS_URL:
        return None
    _redis_sync = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_sync


async def init_redis() -> None:
    global _redis
    if _redis is not None:
        return
    if not REDIS_URL:
        logger.warning("REDIS_URL not configured — cross-dedup disabled")
        return
    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    await _redis.ping()
    _get_sync_redis()
    logger.info("Redis connected (%s)", REDIS_URL.split("@")[-1])


async def close_redis() -> None:
    global _redis
    if _redis:
        await _redis.close()
        _redis = None
        logger.info("Redis disconnected")


def _dedup_key(side: str, symbol: str, candle_ms: int) -> str:
    return f"{side.lower()}:{symbol}:{candle_ms}"


async def is_cross_detected(side: str, symbol: str, candle_ms: int) -> bool:
    if _redis is None:
        return False
    return await _redis.exists(_dedup_key(side, symbol, candle_ms)) > 0


async def mark_cross_detected(side: str, symbol: str, candle_ms: int) -> None:
    if _redis is None:
        return
    await _redis.setex(_dedup_key(side, symbol, candle_ms), CROSS_TTL, "1")


def get_cached(key: str) -> dict | None:
    r = _get_sync_redis()
    if r is None:
        return None
    try:
        raw = r.get(key)
        if raw:
            return json.loads(raw)
    except Exception as e:
        logger.warning("Cache get error: %s", e)
    return None


def set_cached(key: str, data: dict, ttl: int = CACHE_TTL) -> None:
    r = _get_sync_redis()
    if r is None:
        return
    try:
        r.setex(key, ttl, json.dumps(data, default=str))
    except Exception as e:
        logger.warning("Cache set error: %s", e)


def invalidate_trades_cache() -> None:
    r = _get_sync_redis()
    if r is None:
        return
    try:
        for pattern in ("trades:*", "summary:*"):
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor=cursor, match=pattern, count=100)
                if keys:
                    r.delete(*keys)
                if cursor == 0:
                    break
    except Exception as e:
        logger.warning("Cache invalidate error: %s", e)



