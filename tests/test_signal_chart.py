import math

import pytest
from httpx import ASGITransport, AsyncClient

from src import main

pytestmark = pytest.mark.unit

H1 = 60 * 60 * 1000
M15 = 15 * 60 * 1000
SIGNAL_MS = 1_760_140_800_000  # 2025-10-11T00:00:00Z, a 1h candle boundary
NOW_MS = SIGNAL_MS + 100 * H1
WARMUP = main.SIGNAL_RSI_WARMUP


def _signal(**overrides):
    return {
        "id": 7,
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "signal_candle_ms": SIGNAL_MS,
        "status": "SL_HIT",
        "closed_ms": SIGNAL_MS + 5 * H1,
        **overrides,
    }


def _candles(start_ms, end_ms, interval_ms):
    """Every candle opening in [start_ms, end_ms], with a zig-zag close so RSI is defined."""
    count = (end_ms - start_ms) // interval_ms + 1
    return [
        {
            "time": (start_ms + i * interval_ms) // 1000,
            "open": 100.0,
            "high": 102.0,
            "low": 98.0,
            "close": 100.0 + math.sin(i / 3) * 2,
            "volume": 1.0,
            "buy_volume": 0.75 if i % 2 else 0.25,
        }
        for i in range(count)
    ]


@pytest.fixture
def env(monkeypatch):
    state = {"signal": _signal(), "cache": {}, "set_calls": [], "fetch_calls": [], "fail": False}

    async def fake_get_signal(signal_id):
        return state["signal"] if signal_id == 7 else None

    async def fake_get_cached_json(key):
        return state["cache"].get(key)

    async def fake_set_cached_json(key, data, ttl=30):
        state["set_calls"].append((key, ttl))
        state["cache"][key] = data

    def fake_fetch(client, symbol, timeframe, start_ms, end_ms):
        state["fetch_calls"].append((symbol, timeframe, start_ms, end_ms))
        if state["fail"]:
            return None
        return _candles(start_ms, end_ms, main.INTERVAL_MS[timeframe])

    async def fake_get_session_user(token_hash):
        return {"id": 1, "username": "admin", "role": "administrator", "email": "admin@example.com"}

    monkeypatch.setattr(main, "get_signal", fake_get_signal)
    monkeypatch.setattr(main, "get_cached_json", fake_get_cached_json)
    monkeypatch.setattr(main, "set_cached_json", fake_set_cached_json)
    monkeypatch.setattr(main, "fetch_candles_range", fake_fetch)
    monkeypatch.setattr(main, "_get_client", lambda: object())
    monkeypatch.setattr(main, "_klines_budget", {"window": 0, "count": 0})
    monkeypatch.setattr(main, "_klines_inflight", {})
    monkeypatch.setattr(main.time, "time", lambda: NOW_MS / 1000)
    monkeypatch.setattr("src.api.security.get_session_user", fake_get_session_user)
    return state


async def _get(path, auth=True):
    cookies = {"st_session": "chart_test_session"} if auth else {}
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test", cookies=cookies) as client:
        return await client.get(path)


async def test_closed_signal_charts_its_own_trade_window_with_warmup(env):
    resp = await _get("/api/signals/7/chart")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["final"] is True
    start_ms = SIGNAL_MS - 30 * H1
    end_ms = SIGNAL_MS + 5 * H1 + 6 * H1
    assert env["fetch_calls"] == [("BTCUSDT", "1h", start_ms - WARMUP * H1, end_ms)]
    # Warm-up bars are fetched for RSI but not charted
    assert data["candles"][0]["time"] == start_ms // 1000
    assert data["candles"][-1]["time"] == end_ms // 1000
    assert [p["time"] for p in data["rsi"]] == [c["time"] for c in data["candles"]]
    assert all(0 <= p["value"] <= 100 for p in data["rsi"])
    # EMA 50 settles inside the warm-up, so it covers every charted candle
    assert [p["time"] for p in data["ema"]] == [c["time"] for c in data["candles"]]
    closes = [c["close"] for c in _candles(start_ms - WARMUP * H1, end_ms, H1)]
    ema = closes[0]
    for close in closes[1:]:
        ema += (close - ema) * 2 / (main.SIGNAL_CHART_EMA + 1)
    assert data["ema"][-1]["value"] == pytest.approx(ema)
    # Volume delta is taker buys minus taker sells: 2 * buy - volume
    assert [p["time"] for p in data["volume_delta"]] == [c["time"] for c in data["candles"]]
    assert {p["value"] for p in data["volume_delta"]} == {0.5, -0.5}
    assert data["volume_delta"][0]["value"] == 2 * data["candles"][0]["buy_volume"] - data["candles"][0]["volume"]
    assert env["set_calls"] == [("chart:v2:7:final", main.SIGNAL_CANDLES_FINAL_TTL)]
    assert resp.headers["cache-control"] == f"public, max-age={main.SIGNAL_CANDLES_FINAL_TTL}"


async def test_window_uses_the_row_timeframe(env):
    env["signal"] = _signal(timeframe="15m", closed_ms=SIGNAL_MS + 10 * M15)
    resp = await _get("/api/signals/7/chart")

    assert resp.status_code == 200
    start_ms = SIGNAL_MS - 30 * M15
    assert env["fetch_calls"] == [("BTCUSDT", "15m", start_ms - WARMUP * M15, SIGNAL_MS + 16 * M15)]


async def test_rsi_without_enough_history_drops_nan_instead_of_zero(env, monkeypatch):
    # A fresh listing: Binance has no candles before the window, so RSI starts after its period
    monkeypatch.setattr(main, "SIGNAL_RSI_WARMUP", 0)
    resp = await _get("/api/signals/7/chart")

    data = resp.json()["data"]
    assert len(data["candles"]) == 42
    assert len(data["rsi"]) == 42 - main.RSI_PERIOD
    assert data["rsi"][0]["time"] == data["candles"][main.RSI_PERIOD]["time"]
    # 42 candles never reach the 50 needed for the EMA, so it is left out instead of guessed
    assert data["ema"] == []
    assert len(data["volume_delta"]) == 42


async def test_final_window_is_served_from_cache(env):
    env["cache"]["chart:v2:7:final"] = {"symbol": "BTCUSDT", "timeframe": "1h", "candles": [], "rsi": []}
    resp = await _get("/api/signals/7/chart")
    assert resp.status_code == 200
    assert env["fetch_calls"] == []


@pytest.mark.parametrize("status", ["PENDING", "OPEN"])
async def test_active_signal_runs_to_now_without_browser_cache(env, status):
    env["signal"] = _signal(status=status, closed_ms=None)
    resp = await _get("/api/signals/7/chart")

    assert resp.json()["data"]["final"] is False
    start_ms = SIGNAL_MS - 30 * H1
    assert env["fetch_calls"] == [("BTCUSDT", "1h", start_ms - WARMUP * H1, NOW_MS)]
    assert env["set_calls"] == [("chart:v2:7:live", main.KLINES_CACHE_TTL)]
    assert resp.headers["cache-control"] == "no-store"


async def test_recently_closed_window_is_not_final_yet(env, monkeypatch):
    monkeypatch.setattr(main.time, "time", lambda: (SIGNAL_MS + 8 * H1) / 1000)
    resp = await _get("/api/signals/7/chart")

    assert resp.json()["data"]["final"] is False
    assert env["fetch_calls"][0][3] == SIGNAL_MS + 8 * H1


def test_not_filled_window_ends_after_its_close():
    # NOT_FILLED closes at the close time of the last fill-window candle, s + 13I on 1h
    sig = _signal(status="NOT_FILLED", closed_ms=SIGNAL_MS + 13 * H1)
    assert main._signal_candle_window(sig, NOW_MS) == (SIGNAL_MS - 30 * H1, SIGNAL_MS + 19 * H1, True)


async def test_unknown_signal_returns_404(env):
    resp = await _get("/api/signals/8/chart")
    assert resp.status_code == 404
    assert env["fetch_calls"] == []


async def test_unsupported_timeframe_is_an_error_not_a_guess(env):
    env["signal"] = _signal(timeframe="1w")
    resp = await _get("/api/signals/7/chart")
    assert resp.status_code == 500
    assert resp.json()["message"] == "Signal has an unsupported timeframe"
    assert env["fetch_calls"] == []


async def test_upstream_failure_returns_502_and_is_not_cached_by_browsers(env):
    env["fail"] = True
    resp = await _get("/api/signals/7/chart")
    assert resp.status_code == 502
    assert resp.headers["cache-control"] == "no-store"
    assert env["set_calls"] == [("chart:v2:7:final:miss", main.KLINES_MISS_TTL)]


async def test_upstream_budget_exhausted_returns_503(env, monkeypatch):
    monkeypatch.setattr(main, "KLINES_UPSTREAM_BUDGET", 0)
    resp = await _get("/api/signals/7/chart")
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == str(main.KLINES_BUDGET_WINDOW)
    assert env["fetch_calls"] == []


def test_klines_budget_resets_next_window(monkeypatch):
    monkeypatch.setattr(main, "KLINES_UPSTREAM_BUDGET", 1)
    monkeypatch.setattr(main, "_klines_budget", {"window": 0, "count": 0})
    now = [1_000_000.0]
    monkeypatch.setattr(main.time, "time", lambda: now[0])
    assert main._consume_klines_budget() is True
    assert main._consume_klines_budget() is False
    now[0] += main.KLINES_BUDGET_WINDOW
    assert main._consume_klines_budget() is True


def test_candles_carry_taker_buy_volume():
    from src.data_feed.ohlcv import fetch_candles_range

    class Client:
        def klines(self, **kwargs):
            # Binance kline: open time, o, h, l, c, volume, close time, quote vol, trades, taker buy base, ...
            return [[SIGNAL_MS, "1", "2", "0.5", "1.5", "10", SIGNAL_MS + H1 - 1, "15", 7, "6.5", "9.7", "0"]]

    [candle] = fetch_candles_range(Client(), "BTCUSDT", "1h", SIGNAL_MS, SIGNAL_MS)
    assert candle["volume"] == 10.0 and candle["buy_volume"] == 6.5
