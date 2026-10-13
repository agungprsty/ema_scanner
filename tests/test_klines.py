import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from src import main
from src.main import app

pytestmark = pytest.mark.unit

SAMPLE_PAYLOAD = {
    "symbol": "BTCUSDT",
    "timeframe": "2h",
    "candles": [
        {"time": i, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0}
        for i in range(main.KLINES_FETCH_LIMIT)
    ],
    "macd": [
        {"time": i, "macd": 0.1, "signal": 0.05, "histogram": 0.05}
        for i in range(main.KLINES_FETCH_LIMIT)
    ],
}


KNOWN_SYMBOLS = ["BTCUSDT", "ETHUSDT", "AAAA1", "币安人生USDT", *[f"SYM{i}USDT" for i in range(6)]]


class FakeClient:
    def __init__(self, state):
        self.state = state

    def exchange_info(self):
        self.state["exchange_info_calls"] += 1
        if self.state["exchange_info_exc"]:
            raise self.state["exchange_info_exc"]
        return {"symbols": [{"symbol": s, "status": "TRADING"} for s in self.state["exchange_symbols"]]}


@pytest.fixture
def klines_env(monkeypatch):
    state = {
        "cache": {},
        "fetch_calls": [],
        "set_calls": [],
        "fetch_result": SAMPLE_PAYLOAD,
        "fetch_exc": None,
        "fetch_delay": 0.0,
        "exchange_symbols": KNOWN_SYMBOLS,
        "exchange_info_calls": 0,
        "exchange_info_exc": None,
    }

    async def fake_get_cached_json(key):
        return state["cache"].get(key)

    async def fake_set_cached_json(key, data, ttl=30):
        state["set_calls"].append((key, data, ttl))
        state["cache"][key] = data

    def fake_fetch(client, symbol, timeframe="2h", limit=100):
        state["fetch_calls"].append((symbol, timeframe, limit))
        if state["fetch_delay"]:
            time.sleep(state["fetch_delay"])
        if state["fetch_exc"]:
            raise state["fetch_exc"]
        return state["fetch_result"]

    monkeypatch.setattr(main, "get_cached_json", fake_get_cached_json)
    monkeypatch.setattr(main, "set_cached_json", fake_set_cached_json)
    monkeypatch.setattr(main, "fetch_candles_payload", fake_fetch)
    fake_client = FakeClient(state)
    monkeypatch.setattr(main, "_get_client", lambda: fake_client)
    monkeypatch.setattr(main, "_klines_budget", {"window": 0, "count": 0})
    monkeypatch.setattr(
        main,
        "_klines_symbols",
        {"symbols": {s: s for s in KNOWN_SYMBOLS}, "expires": float("inf"), "retry_at": 0.0},
    )
    monkeypatch.setattr(main, "_klines_inflight", {})
    return state


def _expire_symbols():
    main._klines_symbols.update(symbols=None, expires=0.0, retry_at=0.0)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _sliced(limit):
    return {
        **SAMPLE_PAYLOAD,
        "candles": SAMPLE_PAYLOAD["candles"][-limit:],
        "macd": SAMPLE_PAYLOAD["macd"][-limit:],
    }


@pytest.mark.parametrize(
    "params",
    [
        {"symbol": "BTC-USDT"},
        {"symbol": "B"},
        {"symbol": "A" * 31},
        {"symbol": "BTC USDT"},
        {"symbol": "BTC_USDT"},
        {"symbol": "币安-USDT"},
        {"symbol": "BTCUSDT/../x"},
        {"symbol": "BTCUSDT", "interval": "1m"},
        {"symbol": "BTCUSDT", "interval": "2H"},
        {"symbol": "BTCUSDT", "limit": 49},
        {"symbol": "BTCUSDT", "limit": 501},
        {"symbol": "BTCUSDT", "limit": "abc"},
        {},
    ],
)
async def test_klines_invalid_params_rejected(client, klines_env, params):
    resp = await client.get("/api/klines", params=params)
    assert resp.status_code == 422
    assert klines_env["fetch_calls"] == []


async def test_klines_cache_miss_fetches_canonical_and_caches(client, klines_env):
    resp = await client.get("/api/klines", params={"symbol": "btcusdt", "limit": 80})
    assert resp.status_code == 200
    assert resp.json() == _sliced(80)
    assert klines_env["fetch_calls"] == [("BTCUSDT", "2h", main.KLINES_FETCH_LIMIT)]
    assert len(klines_env["set_calls"]) == 1
    key, data, ttl = klines_env["set_calls"][0]
    assert key == "klines:2h:BTCUSDT"
    assert data == SAMPLE_PAYLOAD
    assert ttl == main.KLINES_CACHE_TTL


async def test_klines_cache_hit_skips_fetcher(client, klines_env):
    klines_env["cache"]["klines:2h:ETHUSDT"] = SAMPLE_PAYLOAD
    resp = await client.get("/api/klines", params={"symbol": "ethusdt"})
    assert resp.status_code == 200
    assert resp.json() == _sliced(100)
    assert klines_env["fetch_calls"] == []
    assert klines_env["set_calls"] == []


async def test_klines_different_limits_fetch_once(client, klines_env):
    for limit in (50, 80, 100, 250, 500):
        resp = await client.get("/api/klines", params={"symbol": "BTCUSDT", "limit": limit})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["candles"]) == min(limit, main.KLINES_FETCH_LIMIT)
        assert len(body["macd"]) == min(limit, main.KLINES_FETCH_LIMIT)
        assert body["candles"][-1] == SAMPLE_PAYLOAD["candles"][-1]
    assert len(klines_env["fetch_calls"]) == 1


async def test_klines_upstream_none_returns_502(client, klines_env):
    klines_env["fetch_result"] = None
    resp = await client.get("/api/klines", params={"symbol": "BTCUSDT"})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Failed to load candle data from upstream"
    assert [k for k, _, _ in klines_env["set_calls"]] == ["klines:2h:BTCUSDT:miss"]
    assert klines_env["set_calls"][0][2] == main.KLINES_MISS_TTL


async def test_klines_upstream_exception_returns_502_without_leaking(client, klines_env):
    klines_env["fetch_exc"] = RuntimeError("secret internal failure api_key=xyz")
    resp = await client.get("/api/klines", params={"symbol": "BTCUSDT"})
    assert resp.status_code == 502
    assert "secret" not in resp.text
    assert "klines:2h:BTCUSDT" not in klines_env["cache"]


async def test_klines_repeated_bad_symbol_fetches_once(client, klines_env):
    klines_env["fetch_result"] = None
    for limit in (50, 100, 100, 300):
        resp = await client.get("/api/klines", params={"symbol": "AAAA1", "limit": limit})
        assert resp.status_code == 502
    assert klines_env["fetch_calls"] == [("AAAA1", "2h", main.KLINES_FETCH_LIMIT)]


async def test_klines_upstream_budget_exhausted_returns_503(client, klines_env, monkeypatch):
    monkeypatch.setattr(main, "KLINES_UPSTREAM_BUDGET", 3)
    statuses = []
    for i in range(6):
        resp = await client.get("/api/klines", params={"symbol": f"SYM{i}USDT"})
        statuses.append(resp.status_code)
    assert statuses == [200, 200, 200, 503, 503, 503]
    assert len(klines_env["fetch_calls"]) == 3

    resp = await client.get("/api/klines", params={"symbol": "SYM0USDT", "limit": 60})
    assert resp.status_code == 200
    assert len(klines_env["fetch_calls"]) == 3


async def test_klines_budget_resets_next_window(klines_env, monkeypatch):
    monkeypatch.setattr(main, "KLINES_UPSTREAM_BUDGET", 1)
    now = [1_000_000.0]
    monkeypatch.setattr(main.time, "time", lambda: now[0])
    assert main._consume_klines_budget() is True
    assert main._consume_klines_budget() is False
    now[0] += main.KLINES_BUDGET_WINDOW
    assert main._consume_klines_budget() is True


async def test_klines_unknown_symbol_404_without_budget_or_fetch(client, klines_env):
    for i in range(5):
        resp = await client.get("/api/klines", params={"symbol": f"FAKE{i}USDT"})
        assert resp.status_code == 404
    assert klines_env["fetch_calls"] == []
    assert klines_env["set_calls"] == []
    assert main._klines_budget["count"] == 0


async def test_klines_non_ascii_symbol_accepted(client, klines_env):
    resp = await client.get("/api/klines", params={"symbol": " 币安人生usdt "})
    assert resp.status_code == 200
    assert klines_env["fetch_calls"] == [("币安人生USDT", "2h", main.KLINES_FETCH_LIMIT)]
    assert klines_env["set_calls"][0][0] == "klines:2h:币安人生USDT"


async def test_klines_symbols_loaded_lazily_and_cached(client, klines_env):
    _expire_symbols()
    for _ in range(3):
        resp = await client.get("/api/klines", params={"symbol": "BTCUSDT"})
        assert resp.status_code == 200
    resp = await client.get("/api/klines", params={"symbol": "NOPEUSDT"})
    assert resp.status_code == 404
    assert klines_env["exchange_info_calls"] == 1
    sym_sets = [(k, d, t) for k, d, t in klines_env["set_calls"] if k == main.KLINES_SYMBOLS_KEY]
    assert len(sym_sets) == 1
    assert sym_sets[0][1]["symbols"] == KNOWN_SYMBOLS
    assert sym_sets[0][2] == main.KLINES_SYMBOLS_TTL
    assert len(klines_env["fetch_calls"]) == 1


async def test_klines_symbols_read_from_redis_without_upstream(client, klines_env):
    _expire_symbols()
    klines_env["cache"][main.KLINES_SYMBOLS_KEY] = {"symbols": ["XRPUSDT"], "fetched_at": time.time()}
    resp = await client.get("/api/klines", params={"symbol": "BTCUSDT"})
    assert resp.status_code == 404
    resp = await client.get("/api/klines", params={"symbol": "xrpusdt"})
    assert resp.status_code == 200
    assert klines_env["exchange_info_calls"] == 0
    assert klines_env["fetch_calls"] == [("XRPUSDT", "2h", main.KLINES_FETCH_LIMIT)]


async def test_klines_symbols_expire_and_refresh(client, klines_env, monkeypatch):
    _expire_symbols()
    now = [1_000_000.0]
    monkeypatch.setattr(main.time, "time", lambda: now[0])
    resp = await client.get("/api/klines", params={"symbol": "NEWUSDT"})
    assert resp.status_code == 404
    klines_env["exchange_symbols"] = [*KNOWN_SYMBOLS, "NEWUSDT"]
    klines_env["cache"].pop(main.KLINES_SYMBOLS_KEY)
    now[0] += main.KLINES_SYMBOLS_TTL
    resp = await client.get("/api/klines", params={"symbol": "NEWUSDT"})
    assert resp.status_code == 200
    assert klines_env["exchange_info_calls"] == 2


async def test_klines_symbols_unavailable_falls_back(client, klines_env):
    _expire_symbols()
    klines_env["exchange_info_exc"] = RuntimeError("boom")
    resp = await client.get("/api/klines", params={"symbol": "ANYUSDT"})
    assert resp.status_code == 200
    resp = await client.get("/api/klines", params={"symbol": "OTHERUSDT"})
    assert resp.status_code == 200
    assert klines_env["exchange_info_calls"] == 1
    assert [c[0] for c in klines_env["fetch_calls"]] == ["ANYUSDT", "OTHERUSDT"]
    assert main.KLINES_SYMBOLS_KEY not in klines_env["cache"]


async def test_klines_symbols_refresh_failure_keeps_stale_copy(client, klines_env):
    main._klines_symbols["expires"] = 0.0
    klines_env["exchange_info_exc"] = RuntimeError("boom")
    resp = await client.get("/api/klines", params={"symbol": "FAKEUSDT"})
    assert resp.status_code == 404
    resp = await client.get("/api/klines", params={"symbol": "BTCUSDT"})
    assert resp.status_code == 200
    assert klines_env["exchange_info_calls"] == 1


async def test_klines_concurrent_requests_share_one_fetch(client, klines_env):
    klines_env["fetch_delay"] = 0.2
    limits = [50, 100, 100, 200, 500, 80]
    resps = await asyncio.gather(
        *(client.get("/api/klines", params={"symbol": "BTCUSDT", "limit": lim}) for lim in limits)
    )
    assert [r.status_code for r in resps] == [200] * len(limits)
    assert [len(r.json()["candles"]) for r in resps] == [min(lim, main.KLINES_FETCH_LIMIT) for lim in limits]
    assert len(klines_env["fetch_calls"]) == 1
    assert main._klines_budget["count"] == 1
    assert main._klines_inflight == {}


async def test_klines_concurrent_failures_share_one_fetch(client, klines_env):
    klines_env["fetch_delay"] = 0.2
    klines_env["fetch_result"] = None
    resps = await asyncio.gather(*(client.get("/api/klines", params={"symbol": "ETHUSDT"}) for _ in range(5)))
    assert [r.status_code for r in resps] == [502] * 5
    assert len(klines_env["fetch_calls"]) == 1
    assert main._klines_budget["count"] == 1
    assert main._klines_inflight == {}


async def test_klines_concurrent_symbol_refresh_single_flight(client, klines_env, monkeypatch):
    _expire_symbols()
    real_to_thread = asyncio.to_thread

    async def slow_to_thread(fn, *args, **kwargs):
        await asyncio.sleep(0.05)
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(main.asyncio, "to_thread", slow_to_thread)
    resps = await asyncio.gather(
        *(client.get("/api/klines", params={"symbol": s}) for s in ("BTCUSDT", "ETHUSDT", "FAKEUSDT"))
    )
    assert [r.status_code for r in resps] == [200, 200, 404]
    assert klines_env["exchange_info_calls"] == 1
    assert main._klines_inflight == {}
