import asyncio

import pytest

from src.services import redis_client

pytestmark = pytest.mark.unit


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def set(self, key, value, nx=False, ex=None):
        async with self._lock:
            await asyncio.sleep(0)
            if nx and key in self.store:
                return None
            self.store[key] = value
            if ex is not None:
                self.ttls[key] = ex
            return True

    async def exists(self, *keys):
        return sum(1 for k in keys if k in self.store)

    async def delete(self, *keys):
        removed = 0
        for k in keys:
            if self.store.pop(k, None) is not None:
                removed += 1
            self.ttls.pop(k, None)
        return removed


class BrokenRedis:
    async def set(self, *args, **kwargs):
        raise ConnectionError("redis down")

    async def delete(self, *args, **kwargs):
        raise ConnectionError("redis down")


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "_redis", fake)
    return fake


@pytest.mark.asyncio
async def test_claim_cross_sets_key_with_nx_and_ttl(fake_redis):
    claimed = await redis_client.claim_cross("LONG", "btcusdt", 1700000000000, timeframe="2h", ttl=120)
    assert claimed is True
    key = "macd:2h:long:BTCUSDT:1700000000000"
    assert fake_redis.store[key] == "1"
    assert fake_redis.ttls[key] == 120


@pytest.mark.asyncio
async def test_claim_cross_only_one_concurrent_winner(fake_redis):
    results = await asyncio.gather(
        *[redis_client.claim_cross("SHORT", "ETHUSDT", 1700000000000) for _ in range(10)]
    )
    assert results.count(True) == 1
    assert results.count(False) == 9


@pytest.mark.asyncio
async def test_release_cross_allows_reclaim(fake_redis):
    assert await redis_client.claim_cross("LONG", "BTCUSDT", 1) is True
    assert await redis_client.claim_cross("LONG", "BTCUSDT", 1) is False
    await redis_client.release_cross("LONG", "BTCUSDT", 1)
    assert await redis_client.is_cross_detected("LONG", "BTCUSDT", 1) is False
    assert await redis_client.claim_cross("LONG", "BTCUSDT", 1) is True


@pytest.mark.asyncio
async def test_claim_cross_fails_open_without_redis(monkeypatch):
    monkeypatch.setattr(redis_client, "_redis", None)
    assert await redis_client.claim_cross("LONG", "BTCUSDT", 1) is True
    await redis_client.release_cross("LONG", "BTCUSDT", 1)


@pytest.mark.asyncio
async def test_claim_cross_fails_open_on_error(monkeypatch):
    monkeypatch.setattr(redis_client, "_redis", BrokenRedis())
    assert await redis_client.claim_cross("LONG", "BTCUSDT", 1) is True
    await redis_client.release_cross("LONG", "BTCUSDT", 1)
