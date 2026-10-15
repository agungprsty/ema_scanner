import asyncio
import gc
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.services import background, redis_client
from src.services import cron_scheduler as cron_module
from src.services.cron_scheduler import MacdCronScheduler, cron_slot_ms, ping_dead_man_switch

pytestmark = pytest.mark.unit

TWO_HOURS_MS = 2 * 3600 * 1000


@pytest.fixture
def runs(monkeypatch):
    """Capture cron_runs writes and stub the job's collaborators."""
    recorded: list[dict] = []
    state = {"last_success": None, "evaluate_error": None, "pings": 0}

    async def fake_record(**kwargs):
        recorded.append(kwargs)
        return True

    async def fake_last_success():
        return state["last_success"]

    async def fake_evaluate(client):
        if state["evaluate_error"]:
            raise state["evaluate_error"]
        return {"evaluated_count": 3, "profit_count": 1, "loss_count": 2}

    async def fake_watchlist(active_only: bool = False):
        return []

    async def fake_ping(url=None):
        state["pings"] += 1
        return True

    monkeypatch.setattr(cron_module, "record_cron_run", fake_record)
    monkeypatch.setattr(cron_module, "get_last_cron_success", fake_last_success)
    monkeypatch.setattr(cron_module, "evaluate_pending_signals", fake_evaluate)
    monkeypatch.setattr(cron_module, "get_watchlist", fake_watchlist)
    monkeypatch.setattr(cron_module, "ping_dead_man_switch", fake_ping)
    monkeypatch.setattr(cron_module, "HEALTHCHECK_PING_URL", "")
    monkeypatch.setattr(redis_client, "_redis", None)
    return recorded, state


def _scheduler() -> MacdCronScheduler:
    sched = MacdCronScheduler()
    sched._client_getter = lambda: object()
    return sched


class _AliveTask:
    def __init__(self, done: bool = False):
        self._done = done

    def done(self) -> bool:
        return self._done


def test_slot_is_the_2h_boundary_the_run_follows():
    at = datetime(2026, 10, 14, 18, 0, 15, tzinfo=UTC)  # 01:00:15 WIB
    assert cron_slot_ms(at) == int(datetime(2026, 10, 14, 18, tzinfo=UTC).timestamp() * 1000)
    assert cron_slot_ms(at) % TWO_HOURS_MS == 0
    assert cron_slot_ms(at + timedelta(minutes=119)) == cron_slot_ms(at)


async def test_scheduled_run_is_recorded_ok(runs, monkeypatch):
    recorded, state = runs
    monkeypatch.setattr(cron_module, "HEALTHCHECK_PING_URL", "https://hc.example/ping/abc")
    sched = _scheduler()

    res = await sched._execute_job(is_manual=False)
    await asyncio.sleep(0)

    assert res["status"] == "success"
    assert len(recorded) == 1
    row = recorded[0]
    assert row["status"] == "ok"
    assert (row["signals_count"], row["evaluated_count"]) == (0, 3)
    assert row["slot_ms"] % TWO_HOURS_MS == 0
    assert row["started_at"] <= row["finished_at"]
    assert "error" not in row
    assert sched._last_success is not None
    assert sched.get_status()["last_status"] == "ok"
    assert state["pings"] == 1


async def test_no_ping_without_url(runs):
    _, state = runs
    await _scheduler()._execute_job(is_manual=False)
    await asyncio.sleep(0)
    assert state["pings"] == 0


async def test_failed_run_is_recorded_as_error_and_still_raises(runs):
    recorded, state = runs
    state["evaluate_error"] = RuntimeError("binance timeout")
    sched = _scheduler()

    with pytest.raises(RuntimeError):
        await sched._execute_job(is_manual=False)

    assert [r["status"] for r in recorded] == ["error"]
    assert recorded[0]["error"] == "RuntimeError: binance timeout"
    assert sched._last_success is None
    assert sched.get_status()["last_status"] == "error"
    assert state["pings"] == 0
    assert not sched._lock.locked()


async def test_run_without_client_is_recorded_as_error(runs):
    recorded, _ = runs
    sched = MacdCronScheduler()
    res = await sched._execute_job(is_manual=False)
    assert res["status"] == "error"
    assert [(r["status"], r["error"]) for r in recorded] == [("error", "Client not initialized")]


async def test_run_skipped_by_another_worker_is_recorded(runs, monkeypatch):
    recorded, _ = runs

    async def held(*args, **kwargs):
        return None

    monkeypatch.setattr(cron_module, "acquire_lock", held)
    res = await _scheduler()._execute_job(is_manual=False)
    assert res["status"] == "skipped"
    assert [r["status"] for r in recorded] == ["skipped"]


async def test_manual_trigger_is_not_recorded_and_does_not_count_as_fresh(runs):
    recorded, _ = runs
    sched = _scheduler()
    res = await sched.trigger_now()
    assert res["status"] == "success"
    assert recorded == []
    assert sched._last_success is None


# Freshness
async def test_freshness_is_none_when_cron_disabled(runs, monkeypatch):
    monkeypatch.setattr(cron_module, "CRON_ENABLED", False)
    assert await _scheduler().is_fresh() is None


async def test_dead_or_unstarted_scheduler_is_not_fresh(runs):
    _, state = runs
    now = datetime.now(UTC)
    state["last_success"] = now
    sched = _scheduler()
    assert await sched.is_fresh(now) is False
    sched._task = _AliveTask(done=True)
    assert await sched.is_fresh(now) is False


async def test_freshness_uses_the_database_with_the_configured_window(runs, monkeypatch):
    _, state = runs
    monkeypatch.setattr(cron_module, "CRON_STALE_SECONDS", 7500)
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    sched = _scheduler()
    sched._task = _AliveTask()

    state["last_success"] = now - timedelta(seconds=7500)
    assert await sched.is_fresh(now) is True
    state["last_success"] = now - timedelta(seconds=7501)
    assert await sched.is_fresh(now) is False

    monkeypatch.setattr(cron_module, "CRON_STALE_SECONDS", 9000)
    assert await sched.is_fresh(now) is True


async def test_freshness_falls_back_to_memory_then_start_time(runs):
    _, state = runs
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    sched = _scheduler()
    sched._task = _AliveTask()

    # Nothing has ever succeeded: a fresh deploy gets one window from its start
    sched._started_at = now - timedelta(hours=1)
    assert await sched.is_fresh(now) is True
    sched._started_at = now - timedelta(hours=3)
    assert await sched.is_fresh(now) is False

    # Database unavailable: this process's own last success decides
    sched._last_success = now - timedelta(minutes=30)
    assert await sched.is_fresh(now) is True

    # The newer of the two wins
    state["last_success"] = now - timedelta(hours=5)
    assert await sched.is_fresh(now) is True


def test_public_status_has_no_error_text():
    sched = MacdCronScheduler()
    sched._last_error = "OperationalError: could not connect to postgres:5432"
    assert "last_error" not in sched.get_status()
    assert sched.get_status(include_error=True)["last_error"].startswith("OperationalError")


# Dead-man ping
async def test_ping_hits_the_url_with_a_short_timeout(monkeypatch):
    seen = {}

    async def fake_get(self, url, **kwargs):
        seen["url"] = url
        seen["timeout"] = self.timeout
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert await ping_dead_man_switch("https://hc.example/ping/abc") is True
    assert seen["url"] == "https://hc.example/ping/abc"
    assert seen["timeout"].read == 5.0


async def test_ping_failures_are_only_logged(monkeypatch, caplog):
    async def boom(self, url, **kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    with caplog.at_level(logging.WARNING):
        assert await ping_dead_man_switch("https://hc.example/ping/secret-uuid") is False
    assert "Dead-man ping failed" in caplog.text
    assert "secret-uuid" not in caplog.text

    async def server_error(self, url, **kwargs):
        return httpx.Response(500)

    monkeypatch.setattr(httpx.AsyncClient, "get", server_error)
    assert await ping_dead_man_switch("https://hc.example/ping/abc") is False
    assert await ping_dead_man_switch("") is False


# Background tasks
async def test_background_tasks_are_held_until_done():
    release = asyncio.Event()
    done = []

    async def job():
        await release.wait()
        done.append(True)

    task = background.run_in_background(job(), name="test-job")
    del task
    gc.collect()
    assert background.pending_background_tasks() == 1

    release.set()
    for _ in range(3):
        await asyncio.sleep(0)
    assert done == [True]
    assert background.pending_background_tasks() == 0


async def test_failed_background_task_is_logged_and_released(caplog):
    async def job():
        raise ValueError("send failed")

    with caplog.at_level(logging.ERROR):
        background.run_in_background(job(), name="failing-job")
        for _ in range(3):
            await asyncio.sleep(0)
    assert background.pending_background_tasks() == 0
    assert "failing-job" in caplog.text


async def test_cron_telegram_alert_runs_as_a_held_background_task(runs, monkeypatch):
    spawned = []

    def fake_spawn(coro, name=None):
        spawned.append(name)
        coro.close()

    monkeypatch.setattr(cron_module, "run_in_background", fake_spawn)
    monkeypatch.setattr(cron_module, "TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(cron_module, "TELEGRAM_CHAT_ID", "c")

    class Sig:
        side, symbol, cross_candle_ms, bars_ago = "LONG", "BTCUSDT", 0, 0

    async def watchlist(active_only=False):
        return [{"symbol": "BTCUSDT"}]

    async def detect_thread(fn, *args):
        return "df"

    async def claim(*args, **kwargs):
        return True

    async def save(*args, **kwargs):
        return {"id": 1, "inserted": True}

    monkeypatch.setattr(cron_module, "get_watchlist", watchlist)
    monkeypatch.setattr(cron_module.asyncio, "to_thread", detect_thread)
    monkeypatch.setattr(
        cron_module, "detect_strategy_signals", lambda strategy, *a, **kw: [Sig()] if strategy.status == "live" else []
    )
    monkeypatch.setattr(cron_module, "claim_cross", claim)
    monkeypatch.setattr(cron_module, "save_macd_signal", save)
    monkeypatch.setattr(cron_module, "is_late_signal", lambda *a: False)
    monkeypatch.setattr(cron_module, "format_signal_alert", lambda sig, late=False: "alert")

    res = await _scheduler()._execute_job(is_manual=False)
    assert res["new_signals_count"] == 1
    assert spawned == ["cron-telegram-alert"]
