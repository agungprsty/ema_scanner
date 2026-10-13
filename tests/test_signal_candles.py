import pytest
from httpx import ASGITransport, AsyncClient

from src import main

pytestmark = pytest.mark.unit

H2 = 2 * 60 * 60 * 1000
CROSS_MS = 1_760_140_800_000  # 2025-10-11T00:00:00Z, a 2h candle boundary
NOW_MS = CROSS_MS + 100 * H2

CANDLES = [{"time": i, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0} for i in range(40)]


def _signal(**overrides):
    return {
        "id": 7,
        "symbol": "BTCUSDT",
        "timeframe": "2h",
        "cross_candle_ms": CROSS_MS,
        "win_status": "SL_HIT",
        "exit_time": "2025-10-11T10:00:00Z",
        **overrides,
    }


@pytest.fixture
def env(monkeypatch):
    state = {"signal": _signal(), "cache": {}, "set_calls": [], "fetch_calls": [], "fetch_result": CANDLES}

    async def fake_get_signal(signal_id):
        return state["signal"] if signal_id == 7 else None

    async def fake_get_cached_json(key):
        return state["cache"].get(key)

    async def fake_set_cached_json(key, data, ttl=30):
        state["set_calls"].append((key, ttl))
        state["cache"][key] = data

    def fake_fetch(client, symbol, timeframe, start_ms, end_ms):
        state["fetch_calls"].append((symbol, timeframe, start_ms, end_ms))
        return state["fetch_result"]

    monkeypatch.setattr(main, "get_signal_by_id", fake_get_signal)
    monkeypatch.setattr(main, "get_cached_json", fake_get_cached_json)
    monkeypatch.setattr(main, "set_cached_json", fake_set_cached_json)
    monkeypatch.setattr(main, "fetch_candles_range", fake_fetch)
    monkeypatch.setattr(main, "_get_client", lambda: object())
    monkeypatch.setattr(main, "_klines_budget", {"window": 0, "count": 0})
    monkeypatch.setattr(main, "_klines_inflight", {})
    monkeypatch.setattr(main.time, "time", lambda: NOW_MS / 1000)
    return state


async def _get(path):
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        return await client.get(path)


async def test_closed_signal_charts_its_own_trade_window(env):
    resp = await _get("/api/signals/7/candles")

    assert resp.status_code == 200
    assert resp.json()["final"] is True
    exit_ms = CROSS_MS + 5 * H2
    assert env["fetch_calls"] == [("BTCUSDT", "2h", CROSS_MS - 30 * H2, exit_ms + 6 * H2)]
    assert env["set_calls"] == [("klines:signal:7:final", main.SIGNAL_CANDLES_FINAL_TTL)]
    assert resp.headers["cache-control"] == f"public, max-age={main.SIGNAL_CANDLES_FINAL_TTL}"


async def test_final_window_is_served_from_cache(env):
    env["cache"]["klines:signal:7:final"] = {"symbol": "BTCUSDT", "timeframe": "2h", "candles": CANDLES}
    resp = await _get("/api/signals/7/candles")
    assert resp.status_code == 200
    assert env["fetch_calls"] == []


async def test_open_signal_runs_to_now_with_short_cache(env):
    env["signal"] = _signal(win_status="PENDING", exit_time=None)
    resp = await _get("/api/signals/7/candles")

    assert resp.json()["final"] is False
    assert env["fetch_calls"] == [("BTCUSDT", "2h", CROSS_MS - 30 * H2, NOW_MS)]
    assert env["set_calls"] == [("klines:signal:7:live", main.KLINES_CACHE_TTL)]
    assert resp.headers["cache-control"] == f"public, max-age={main.KLINES_CACHE_TTL}"


async def test_recently_closed_window_is_not_final_yet(env, monkeypatch):
    monkeypatch.setattr(main.time, "time", lambda: (CROSS_MS + 8 * H2) / 1000)
    resp = await _get("/api/signals/7/candles")

    assert resp.json()["final"] is False
    assert env["fetch_calls"][0][3] == CROSS_MS + 8 * H2


def test_exit_times_without_zone_are_utc():
    assert main._parse_utc_ms("2025-10-11 10:00:00") == CROSS_MS + 5 * H2
    assert main._parse_utc_ms("2025-10-11T10:00:00Z") == CROSS_MS + 5 * H2
    assert main._parse_utc_ms("not a time") is None
    assert main._parse_utc_ms(None) is None


def test_closed_signal_without_exit_time_uses_expiry():
    sig = _signal(exit_time=None, win_status="EXPIRED")
    start, end, final = main._signal_candle_window(sig, NOW_MS)
    expiry_ms = CROSS_MS + (main.SIGNAL_EXPIRY_CANDLES + 1) * H2
    assert (start, end, final) == (CROSS_MS - 30 * H2, expiry_ms + 6 * H2, True)


async def test_unknown_signal_returns_404(env):
    resp = await _get("/api/signals/8/candles")
    assert resp.status_code == 404
    assert env["fetch_calls"] == []


async def test_upstream_failure_returns_502_and_is_not_cached_by_browsers(env):
    env["fetch_result"] = None
    resp = await _get("/api/signals/7/candles")
    assert resp.status_code == 502
    assert "cache-control" not in resp.headers
    assert env["set_calls"] == [("klines:signal:7:final:miss", main.KLINES_MISS_TTL)]
