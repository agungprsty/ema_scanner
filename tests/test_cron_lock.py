import asyncio
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from src.services import cron_scheduler as cron_module
from src.services import redis_client
from src.services.cron_scheduler import TIMEFRAME, TIMEFRAME_MS, Scheduler

pytestmark = pytest.mark.unit

BOUNDARY = 1_792_540_800_000  # an exact multiple of every supported interval
BOUNDARY_KEY = f"lock:job:{TIMEFRAME}:{BOUNDARY}"
MANUAL_KEY = f"lock:job:{TIMEFRAME}:manual"


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
def runs(monkeypatch):
    calls = []

    async def fake_run_job(client):
        calls.append(client)
        return {"new_signals": 0, "failed_symbols": []}

    monkeypatch.setattr(cron_module, "run_job", fake_run_job)
    return calls


def _scheduler() -> Scheduler:
    sched = Scheduler()
    sched._client_getter = lambda: object()
    return sched


def _scheduled(sched: Scheduler):
    return sched._execute_job(f"job:{TIMEFRAME}:{BOUNDARY}", TIMEFRAME_MS // 1000, is_manual=False)


async def test_acquire_lock_returns_owner_token(fake_redis):
    token = await redis_client.acquire_lock("job:1h:1", timeout_seconds=180)
    assert token
    assert token != redis_client.LOCAL_LOCK_TOKEN
    assert fake_redis.store["lock:job:1h:1"] == token
    assert fake_redis.ttls["lock:job:1h:1"] == 180
    assert await redis_client.acquire_lock("job:1h:1", timeout_seconds=180) is None


async def test_release_lock_with_wrong_token_keeps_lock(fake_redis):
    token = await redis_client.acquire_lock("job:1h:1")
    await redis_client.release_lock("job:1h:1", "not-the-owner")
    assert fake_redis.store["lock:job:1h:1"] == token
    await redis_client.release_lock("job:1h:1", redis_client.LOCAL_LOCK_TOKEN)
    assert fake_redis.store["lock:job:1h:1"] == token
    await redis_client.release_lock("job:1h:1", token)
    assert "lock:job:1h:1" not in fake_redis.store


async def test_lock_fails_open_without_redis(no_redis):
    token = await redis_client.acquire_lock("job:1h:1")
    assert token == redis_client.LOCAL_LOCK_TOKEN
    await redis_client.release_lock("job:1h:1", token)


async def test_lock_fails_open_on_redis_error(monkeypatch):
    class BrokenRedis:
        async def set(self, *args, **kwargs):
            raise ConnectionError("down")

    monkeypatch.setattr(redis_client, "_redis", BrokenRedis())
    assert await redis_client.acquire_lock("job:1h:1") == redis_client.LOCAL_LOCK_TOKEN


async def test_loop_runs_the_boundary_with_a_kept_per_boundary_lock(fake_redis, monkeypatch):
    delay_ms = cron_module.CRON_SAFETY_DELAY_SECONDS * 1000
    now = BOUNDARY + delay_ms - 1000
    sleeps = []
    sched = _scheduler()
    sched._is_active = True

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def fake_run_job(client):
        sched._is_active = False
        return {"new_signals": 0}

    monkeypatch.setattr(cron_module, "now_ms", lambda: now)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(cron_module, "run_job", fake_run_job)

    await sched._cron_loop()

    assert sleeps == [1.0]
    assert BOUNDARY_KEY in fake_redis.store
    assert fake_redis.ttls[BOUNDARY_KEY] == TIMEFRAME_MS // 1000
    assert sched.get_status()["next_run"].endswith("+07:00")
    assert sched.get_status()["last_status"] == "success"


async def test_scheduled_run_skipped_when_boundary_taken_by_other_worker(fake_redis, runs):
    fake_redis.store[BOUNDARY_KEY] = "other-worker"

    res = await _scheduled(_scheduler())

    assert res["status"] == "skipped"
    assert fake_redis.store[BOUNDARY_KEY] == "other-worker"
    assert runs == []


async def test_scheduled_run_skipped_while_a_job_is_running(no_redis, runs):
    sched = _scheduler()
    await sched._lock.acquire()
    try:
        res = await _scheduled(sched)
    finally:
        sched._lock.release()

    assert res["status"] == "skipped"
    assert runs == []


async def test_manual_run_busy_while_scheduled_job_runs(fake_redis, monkeypatch):
    release = asyncio.Event()
    started = asyncio.Event()
    calls = []

    async def slow_run_job(client):
        calls.append(client)
        started.set()
        await release.wait()
        return {"new_signals": 0}

    monkeypatch.setattr(cron_module, "run_job", slow_run_job)
    sched = _scheduler()

    first = asyncio.create_task(_scheduled(sched))
    await started.wait()
    manual = await sched.trigger_now()
    assert manual["status"] == "busy"
    assert MANUAL_KEY not in fake_redis.store
    assert len(calls) == 1

    release.set()
    assert (await asyncio.wait_for(first, timeout=5))["status"] == "success"
    assert not sched._lock.locked()


async def test_manual_run_releases_its_lock(fake_redis, runs):
    res = await _scheduler().trigger_now()

    assert res["status"] == "success"
    assert fake_redis.ttls[MANUAL_KEY] == cron_module.MANUAL_LOCK_SECONDS
    assert MANUAL_KEY not in fake_redis.store
    assert len(runs) == 1


async def test_manual_run_busy_when_other_worker_runs_it(fake_redis, runs):
    fake_redis.store[MANUAL_KEY] = "other-worker"

    res = await _scheduler().trigger_now()

    assert res["status"] == "busy"
    assert fake_redis.store[MANUAL_KEY] == "other-worker"
    assert runs == []


async def test_client_getter_kept_when_cron_disabled(monkeypatch):
    monkeypatch.setattr(cron_module, "CRON_ENABLED", False)
    sched = Scheduler()

    def getter():
        return None

    sched.start(getter)

    assert sched._client_getter is getter
    assert sched._task is None


async def test_failed_run_is_logged_and_shown_in_status(no_redis, monkeypatch, caplog):
    async def broken_run_job(client):
        raise RuntimeError("Database not ready")

    monkeypatch.setattr(cron_module, "run_job", broken_run_job)
    sched = _scheduler()

    with caplog.at_level(logging.ERROR, logger=cron_module.__name__):
        res = await sched.trigger_now()

    assert res == {"status": "error", "message": "Scan failed, see server logs"}
    status = sched.get_status()
    assert status["last_status"] == "failed"
    assert status["last_error"] == "RuntimeError"
    assert "Database not ready" in caplog.text
    assert not sched._lock.locked()


async def test_trigger_endpoint_returns_409_when_busy(fake_redis, runs, monkeypatch):
    from src import main
    from src.api.security import Principal

    fake_redis.store[MANUAL_KEY] = "other-worker"
    monkeypatch.setattr(main, "cron_scheduler", _scheduler())
    main.app.dependency_overrides[main.require_admin] = lambda: Principal(id=None, username="admin", role="administrator")
    try:
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/cron/trigger")
    finally:
        main.app.dependency_overrides.pop(main.require_admin, None)

    assert resp.status_code == 409
    assert "try again later" in resp.json()["message"]
    assert fake_redis.store[MANUAL_KEY] == "other-worker"
    assert runs == []
