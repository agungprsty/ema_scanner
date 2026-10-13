import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from src.services import cron_scheduler as cron_module
from src.services import redis_client
from src.services.cron_scheduler import MacdCronScheduler

pytestmark = pytest.mark.unit

LOCK_KEY = "lock:cron:2h_execution"


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0

    async def eval(self, script, numkeys, *args):
        keys, argv = args[:numkeys], args[numkeys:]
        if self.store.get(keys[0]) == argv[0]:
            del self.store[keys[0]]
            return 1
        return 0


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "_redis", fake)
    return fake


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr(redis_client, "_redis", None)


@pytest.fixture
def job_mocks(monkeypatch):
    calls = {"evaluate": 0}

    async def mock_evaluate(client):
        calls["evaluate"] += 1
        return {"evaluated_count": 0, "profit_count": 0, "loss_count": 0}

    async def mock_get_watchlist(active_only: bool = False):
        return []

    monkeypatch.setattr(cron_module, "evaluate_pending_signals", mock_evaluate)
    monkeypatch.setattr(cron_module, "get_watchlist", mock_get_watchlist)
    return calls


def _scheduler() -> MacdCronScheduler:
    sched = MacdCronScheduler()
    sched._client_getter = lambda: object()
    return sched


async def test_acquire_lock_returns_owner_token(fake_redis):
    token = await redis_client.acquire_lock("cron:2h_execution", timeout_seconds=180)
    assert token
    assert token != redis_client.LOCAL_LOCK_TOKEN
    assert fake_redis.store[LOCK_KEY] == token
    assert fake_redis.ttls[LOCK_KEY] == 180
    assert await redis_client.acquire_lock("cron:2h_execution", timeout_seconds=180) is None


async def test_release_lock_with_wrong_token_keeps_lock(fake_redis):
    token = await redis_client.acquire_lock("cron:2h_execution", timeout_seconds=180)
    await redis_client.release_lock("cron:2h_execution", "not-the-owner")
    assert fake_redis.store[LOCK_KEY] == token
    await redis_client.release_lock("cron:2h_execution", redis_client.LOCAL_LOCK_TOKEN)
    assert fake_redis.store[LOCK_KEY] == token
    await redis_client.release_lock("cron:2h_execution", token)
    assert LOCK_KEY not in fake_redis.store


async def test_lock_fails_open_without_redis(no_redis):
    token = await redis_client.acquire_lock("cron:2h_execution")
    assert token == redis_client.LOCAL_LOCK_TOKEN
    await redis_client.release_lock("cron:2h_execution", token)


async def test_lock_fails_open_on_redis_error(monkeypatch):
    class BrokenRedis:
        async def set(self, *args, **kwargs):
            raise ConnectionError("down")

    monkeypatch.setattr(redis_client, "_redis", BrokenRedis())
    assert await redis_client.acquire_lock("cron:2h_execution") == redis_client.LOCAL_LOCK_TOKEN


async def test_manual_trigger_busy_when_scheduled_lock_held(fake_redis, job_mocks):
    fake_redis.store[LOCK_KEY] = "scheduled-owner"
    sched = _scheduler()

    res = await sched.trigger_now()

    assert res["status"] == "busy"
    assert fake_redis.store[LOCK_KEY] == "scheduled-owner"
    assert job_mocks["evaluate"] == 0
    assert not sched._lock.locked()


async def test_scheduled_run_skipped_when_lock_held(fake_redis, job_mocks):
    fake_redis.store[LOCK_KEY] = "other-worker"
    sched = _scheduler()

    res = await sched._execute_job(is_manual=False)

    assert res["status"] == "skipped"
    assert fake_redis.store[LOCK_KEY] == "other-worker"
    assert job_mocks["evaluate"] == 0


async def test_manual_run_releases_own_lock(fake_redis, job_mocks):
    sched = _scheduler()

    res = await sched.trigger_now()

    assert res["status"] == "success"
    assert LOCK_KEY not in fake_redis.store
    assert job_mocks["evaluate"] == 1


async def test_scheduled_run_keeps_lock_until_ttl(fake_redis, job_mocks):
    sched = _scheduler()

    res = await sched._execute_job(is_manual=False)

    assert res["status"] == "success"
    assert LOCK_KEY in fake_redis.store
    assert fake_redis.ttls[LOCK_KEY] == 180


async def test_local_lock_held_for_whole_job_body(no_redis, monkeypatch):
    release = asyncio.Event()
    started = asyncio.Event()
    calls = {"evaluate": 0}

    async def slow_evaluate(client):
        calls["evaluate"] += 1
        started.set()
        await release.wait()
        return {"evaluated_count": 0, "profit_count": 0, "loss_count": 0}

    async def mock_get_watchlist(active_only: bool = False):
        return []

    monkeypatch.setattr(cron_module, "evaluate_pending_signals", slow_evaluate)
    monkeypatch.setattr(cron_module, "get_watchlist", mock_get_watchlist)
    sched = _scheduler()

    first = asyncio.create_task(sched._execute_job(is_manual=False))
    await started.wait()
    second = asyncio.create_task(sched._execute_job(is_manual=False))
    for _ in range(5):
        await asyncio.sleep(0)

    assert sched._lock.locked()
    assert calls["evaluate"] == 1
    assert not second.done()

    manual = await sched.trigger_now()
    assert manual["status"] == "busy"
    assert calls["evaluate"] == 1

    release.set()
    first_res, second_res = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    assert first_res["status"] == "success"
    assert second_res["status"] == "success"
    assert calls["evaluate"] == 2
    assert not sched._lock.locked()


async def test_trigger_endpoint_returns_409_when_busy(fake_redis, job_mocks, monkeypatch):
    from src import main

    fake_redis.store[LOCK_KEY] = "scheduled-owner"
    sched = _scheduler()
    monkeypatch.setattr(main, "cron_scheduler", sched)
    main.app.dependency_overrides[main.verify_api_key] = lambda: True
    try:
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/cron/trigger")
    finally:
        main.app.dependency_overrides.pop(main.verify_api_key, None)

    assert resp.status_code == 409
    assert "try again later" in resp.json()["detail"]
    assert fake_redis.store[LOCK_KEY] == "scheduled-owner"
    assert job_mocks["evaluate"] == 0
