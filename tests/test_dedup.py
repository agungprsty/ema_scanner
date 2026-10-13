import asyncio

import pytest
from fastapi import BackgroundTasks

from src import main
from src.services import cron_scheduler as cron_module
from src.services import db, redis_client
from src.strategy.macd import MacdSignal

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


def _make_signal(symbol: str = "BTCUSDT", side: str = "LONG", candle_ms: int = 1700000000000) -> MacdSignal:
    return MacdSignal(
        symbol=symbol,
        side=side,
        reason="MACD_BULLISH_CROSS",
        cross_candle_ms=candle_ms,
        cross_time="2026-10-11 00:00:00",
        cross_price=100.0,
        entry_price=100.0,
        stop_loss=95.0,
        tp1=107.5,
        risk_reward_ratio=1.5,
        macd=0.1,
        signal=0.05,
        histogram=0.05,
        close=100.0,
        bars_ago=0,
    )


class _Frame(list):
    @property
    def iloc(self):
        return self

    def copy(self):
        return self


@pytest.fixture
def scan_env(monkeypatch, fake_redis):
    state = {"save_result": {"id": 42, "inserted": True}, "saved": []}
    signals = {"BTCUSDT": _make_signal("BTCUSDT"), "ETHUSDT": _make_signal("ETHUSDT", side="SHORT")}

    async def fake_save(sig, timeframe="2h"):
        state["saved"].append(sig.symbol)
        result = state["save_result"]
        return result(sig) if callable(result) else result

    async def fake_send_alert(body):
        return None

    async def fake_evaluate(client):
        return {}

    monkeypatch.setattr(main, "_get_client", lambda: object())
    monkeypatch.setattr(main, "fetch_klines", lambda client, sym, tf, limit: _Frame([sym, sym]))
    monkeypatch.setattr(main, "compute_macd", lambda df, **kwargs: df)
    monkeypatch.setattr(main, "detect_macd_cross", lambda df, sym, **kwargs: signals.get(sym))
    monkeypatch.setattr(main, "save_macd_signal", fake_save)
    monkeypatch.setattr(main, "send_alert", fake_send_alert)
    monkeypatch.setattr(main, "evaluate_pending_signals", fake_evaluate)
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(main, "TELEGRAM_CHAT_ID", "")
    return state


async def _run_scan(symbols: str, save_db: bool = True, send_telegram: bool = True):
    background = BackgroundTasks()
    resp = await main.run_macd_scan(
        background,
        symbols=symbols,
        limit=100,
        lookback=3,
        include_current=True,
        volume_min=None,
        send_telegram=send_telegram,
        save_db=save_db,
    )
    alerts = [t for t in background.tasks if t.func is main.send_alert]
    return resp, alerts


@pytest.mark.asyncio
async def test_scan_claims_and_persists_new_signal(scan_env, fake_redis):
    resp, alerts = await _run_scan("BTCUSDT")
    assert resp.new_signals_count == 1
    assert resp.signals[0].is_new is True
    assert resp.signals[0].id == 42
    assert "macd:2h:long:BTCUSDT:1700000000000" in fake_redis.store
    assert len(alerts) == 1

    resp2, alerts2 = await _run_scan("BTCUSDT")
    assert resp2.new_signals_count == 0
    assert resp2.signals[0].is_new is False
    assert alerts2 == []
    assert scan_env["saved"] == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_scan_releases_claim_when_save_fails(scan_env, fake_redis):
    scan_env["save_result"] = lambda sig: None if sig.symbol == "BTCUSDT" else {"id": 7, "inserted": True}

    resp, alerts = await _run_scan("BTCUSDT,ETHUSDT")
    assert resp.new_signals_count == 1
    by_symbol = {s.symbol: s for s in resp.signals}
    assert by_symbol["BTCUSDT"].is_new is False
    assert by_symbol["ETHUSDT"].is_new is True
    assert resp.new_signals_count == sum(s.is_new for s in resp.signals)
    assert "macd:2h:long:BTCUSDT:1700000000000" not in fake_redis.store
    assert "macd:2h:short:ETHUSDT:1700000000000" in fake_redis.store
    assert len(alerts) == 1
    body = alerts[0].args[0]
    assert "ETHUSDT" in body
    assert "BTCUSDT" not in body

    scan_env["save_result"] = {"id": 8, "inserted": True}
    resp2, alerts2 = await _run_scan("BTCUSDT")
    assert resp2.new_signals_count == 1
    assert resp2.signals[0].id == 8
    assert "macd:2h:long:BTCUSDT:1700000000000" in fake_redis.store
    assert len(alerts2) == 1


@pytest.mark.asyncio
async def test_scan_alerts_and_keeps_claim_when_save_fails_with_telegram(scan_env, fake_redis, monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(main, "TELEGRAM_CHAT_ID", "chat")
    scan_env["save_result"] = None

    resp, alerts = await _run_scan("BTCUSDT")
    assert resp.new_signals_count == 1
    assert resp.signals[0].is_new is True
    assert resp.signals[0].id is None
    assert "macd:2h:long:BTCUSDT:1700000000000" in fake_redis.store
    assert len(alerts) == 1
    assert "BTCUSDT" in alerts[0].args[0]

    resp2, alerts2 = await _run_scan("BTCUSDT")
    assert resp2.new_signals_count == 0
    assert alerts2 == []


@pytest.mark.asyncio
async def test_scan_releases_claim_when_save_fails_and_alerts_not_requested(scan_env, fake_redis, monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(main, "TELEGRAM_CHAT_ID", "chat")
    scan_env["save_result"] = None

    resp, alerts = await _run_scan("BTCUSDT", send_telegram=False)
    assert resp.new_signals_count == 0
    assert resp.signals[0].is_new is False
    assert fake_redis.store == {}
    assert alerts == []


@pytest.mark.asyncio
async def test_scan_treats_db_duplicate_as_not_new(scan_env, fake_redis):
    scan_env["save_result"] = lambda sig: (
        {"id": 5, "inserted": False} if sig.symbol == "BTCUSDT" else {"id": 6, "inserted": True}
    )

    resp, alerts = await _run_scan("BTCUSDT,ETHUSDT")
    by_symbol = {s.symbol: s for s in resp.signals}
    assert by_symbol["BTCUSDT"].is_new is False
    assert by_symbol["BTCUSDT"].id == 5
    assert by_symbol["ETHUSDT"].is_new is True
    assert resp.new_signals_count == 1
    assert resp.new_signals_count == sum(s.is_new for s in resp.signals)
    assert "macd:2h:long:BTCUSDT:1700000000000" in fake_redis.store
    assert len(alerts) == 1
    body = alerts[0].args[0]
    assert "ETHUSDT" in body
    assert "BTCUSDT" not in body


@pytest.mark.asyncio
async def test_scan_preview_does_not_claim(scan_env, fake_redis):
    resp, alerts = await _run_scan("BTCUSDT", save_db=False, send_telegram=False)
    assert resp.new_signals_count == 1
    assert resp.signals[0].is_new is True
    assert fake_redis.store == {}
    assert scan_env["saved"] == []
    assert alerts == []

    resp2, alerts2 = await _run_scan("BTCUSDT")
    assert resp2.new_signals_count == 1
    assert resp2.signals[0].is_new is True
    assert len(alerts2) == 1


@pytest.mark.asyncio
async def test_scan_preview_reports_existing_claim_as_duplicate(scan_env, fake_redis):
    await redis_client.claim_cross("LONG", "BTCUSDT", 1700000000000)
    resp, _ = await _run_scan("BTCUSDT", save_db=False, send_telegram=False)
    assert resp.new_signals_count == 0
    assert resp.signals[0].is_new is False
    assert scan_env["saved"] == []


@pytest.fixture
def cron_env(monkeypatch, fake_redis):
    state = {"save_result": {"id": 1, "inserted": True}, "alerts": []}
    signals = {"BTCUSDT": _make_signal("BTCUSDT")}

    async def fake_save(sig, timeframe="2h"):
        return state["save_result"]

    async def fake_send_alert(body):
        state["alerts"].append(body)

    async def fake_evaluate(client):
        return {"evaluated_count": 0}

    async def fake_get_watchlist(active_only=False):
        return [{"symbol": "BTCUSDT"}]

    async def fake_acquire_lock(*args, **kwargs):
        return True

    async def fake_release_lock(*args, **kwargs):
        return None

    monkeypatch.setattr(cron_module, "fetch_klines", lambda client, sym, tf, limit: _Frame([sym, sym]))
    monkeypatch.setattr(cron_module, "compute_macd", lambda df, **kwargs: df)
    monkeypatch.setattr(cron_module, "detect_macd_cross", lambda df, sym, **kwargs: signals.get(sym))
    monkeypatch.setattr(cron_module, "save_macd_signal", fake_save)
    monkeypatch.setattr(cron_module, "send_alert", fake_send_alert)
    monkeypatch.setattr(cron_module, "evaluate_pending_signals", fake_evaluate)
    monkeypatch.setattr(cron_module, "get_watchlist", fake_get_watchlist)
    monkeypatch.setattr(cron_module, "acquire_lock", fake_acquire_lock, raising=False)
    monkeypatch.setattr(cron_module, "release_lock", fake_release_lock, raising=False)
    monkeypatch.setattr(cron_module, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(cron_module, "TELEGRAM_CHAT_ID", "")
    return state


def _make_scheduler():
    scheduler = cron_module.MacdCronScheduler()
    scheduler._client_getter = lambda: object()
    return scheduler


@pytest.mark.asyncio
async def test_cron_releases_claim_when_save_fails(cron_env, fake_redis):
    scheduler = cron_module.MacdCronScheduler()
    scheduler._client_getter = lambda: object()

    cron_env["save_result"] = None
    res = await scheduler.trigger_now()
    assert res["new_signals_count"] == 0
    assert fake_redis.store == {}

    cron_env["save_result"] = {"id": 3, "inserted": True}
    res2 = await scheduler.trigger_now()
    assert res2["new_signals_count"] == 1
    assert "macd:2h:long:BTCUSDT:1700000000000" in fake_redis.store

    res3 = await scheduler.trigger_now()
    assert res3["new_signals_count"] == 0


@pytest.mark.asyncio
async def test_cron_alerts_and_keeps_claim_when_save_fails_with_telegram(cron_env, fake_redis, monkeypatch):
    monkeypatch.setattr(cron_module, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(cron_module, "TELEGRAM_CHAT_ID", "chat")
    scheduler = _make_scheduler()

    cron_env["save_result"] = None
    res = await scheduler.trigger_now()
    await asyncio.sleep(0)
    assert res["new_signals_count"] == 1
    assert "macd:2h:long:BTCUSDT:1700000000000" in fake_redis.store
    assert len(cron_env["alerts"]) == 1
    assert "BTCUSDT" in cron_env["alerts"][0]

    res2 = await scheduler.trigger_now()
    await asyncio.sleep(0)
    assert res2["new_signals_count"] == 0
    assert len(cron_env["alerts"]) == 1


@pytest.mark.asyncio
async def test_cron_treats_db_duplicate_as_not_new(cron_env, fake_redis):
    scheduler = _make_scheduler()

    cron_env["save_result"] = {"id": 3, "inserted": False}
    res = await scheduler.trigger_now()
    await asyncio.sleep(0)
    assert res["new_signals_count"] == 0
    assert cron_env["alerts"] == []
    assert "macd:2h:long:BTCUSDT:1700000000000" in fake_redis.store


@pytest.mark.asyncio
async def test_scan_reports_stored_outcome_for_known_signal(scan_env, fake_redis, monkeypatch):
    lookups = []

    async def fake_lookup(keys, timeframe="2h"):
        lookups.append((sorted(keys), timeframe))
        return {
            ("BTCUSDT", "LONG", 1700000000000): {
                "id": 42,
                "win_status": "TP1_HIT",
                "cross_time": "2023-11-14T22:00:00Z",
                "cross_price": 99.5,
                "close_price": 100.5,
                "entry_price": 99.5,
                "stop_loss": 97.0,
                "tp1": 103.25,
                "risk_reward_ratio": 1.5,
                "is_confirmed": False,
            },
        }

    monkeypatch.setattr(main, "get_signals_by_keys", fake_lookup)

    await _run_scan("BTCUSDT")
    resp, _ = await _run_scan("BTCUSDT,ETHUSDT")

    by_symbol = {s.symbol: s for s in resp.signals}
    assert by_symbol["BTCUSDT"].is_new is False
    assert by_symbol["BTCUSDT"].id == 42
    assert by_symbol["BTCUSDT"].win_status == "TP1_HIT"
    assert by_symbol["BTCUSDT"].entry_price == 99.5
    assert by_symbol["BTCUSDT"].stop_loss == 97.0
    assert by_symbol["BTCUSDT"].tp1 == 103.25
    assert by_symbol["BTCUSDT"].close == 100.5
    assert by_symbol["BTCUSDT"].confirmed is False
    assert by_symbol["ETHUSDT"].win_status == "PENDING"
    assert lookups[-1] == (
        [("BTCUSDT", "LONG", 1700000000000), ("ETHUSDT", "SHORT", 1700000000000)],
        "2h",
    )


@pytest.mark.asyncio
async def test_signals_by_keys_without_database_returns_empty(monkeypatch):
    monkeypatch.setattr(db, "_session_maker", None)
    assert await db.get_signals_by_keys([("BTCUSDT", "LONG", 1)]) == {}
