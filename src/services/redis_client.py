import logging

import redis.asyncio as aioredis

from src.config.settings import REDIS_URL

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None
CROSS_TTL = 3600
async def init_redis() -> None:
    global _redis
    if _redis is not None:
        return
    if not REDIS_URL:
        logger.warning("REDIS_URL not configured — cross-dedup disabled")
        return
    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    await _redis.ping()
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



