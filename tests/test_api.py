import pytest
from httpx import ASGITransport, AsyncClient

from src.config import settings
from src.main import app
from src.services import db

pytestmark = pytest.mark.unit

TEST_API_KEY = "test_api_secret_key"
AUTH_HEADERS = {"X-API-KEY": TEST_API_KEY}

SIGNAL = {
    "id": 1,
    "symbol": "BTCUSDT",
    "side": "LONG",
    "timeframe": "1h",
    "signal_candle_ms": 1_760_140_800_000,
    "status": "TP_HIT",
    "journal_status": None,
}


@pytest.fixture(autouse=True)
def api_secret_key(monkeypatch):
    monkeypatch.setattr("src.main.API_SECRET_KEY", TEST_API_KEY)
    return TEST_API_KEY


@pytest.fixture(autouse=True)
def mock_db_services(monkeypatch):
    watchlist_store = {
        "BTCUSDT": {"symbol": "BTCUSDT", "is_active": True, "notes": "Default"},
        "ETHUSDT": {"symbol": "ETHUSDT", "is_active": True, "notes": "Default"},
    }
    calls = {"signals": []}

    async def mock_is_db_connected():
        return True

    async def mock_is_redis_connected():
        return True

    async def mock_get_watchlist(active_only: bool = False):
        if active_only:
            return [v for v in watchlist_store.values() if v["is_active"]]
        return list(watchlist_store.values())

    async def mock_add_watchlist_symbol(symbol: str, notes: str = ""):
        clean_sym = symbol.strip().upper()
        if not clean_sym.endswith("USDT"):
            clean_sym += "USDT"
        item = {"id": len(watchlist_store) + 1, "symbol": clean_sym, "is_active": True, "notes": notes}
        watchlist_store[clean_sym] = item
        return item

    async def mock_delete_watchlist_symbol(symbol: str):
        clean_sym = symbol.strip().upper()
        return bool(watchlist_store.pop(clean_sym, None))

    async def mock_toggle_watchlist_symbol(symbol: str, is_active: bool):
        clean_sym = symbol.strip().upper()
        if clean_sym in watchlist_store:
            watchlist_store[clean_sym]["is_active"] = is_active
            return True
        return False

    async def mock_signal_stats(conditions=()):
        counts = {"total": 3, "long": 2, "short": 1, "symbols": 2, "TP_HIT": 1, "NOT_FILLED": 1, "OPEN": 1}
        return db.build_stats(counts, 1.9, [1.9])

    async def mock_list_signals(**kwargs):
        calls["signals"].append(kwargs)
        db.signal_conditions(**{k: v for k, v in kwargs.items() if k not in ("limit", "offset")})  # 422 path
        summary = await mock_signal_stats()
        return [SIGNAL], 1, summary

    monkeypatch.setattr("src.main.is_db_connected", mock_is_db_connected)
    monkeypatch.setattr("src.main.is_redis_connected", mock_is_redis_connected)
    monkeypatch.setattr("src.main.get_watchlist", mock_get_watchlist)
    monkeypatch.setattr("src.main.add_watchlist_symbol", mock_add_watchlist_symbol)
    monkeypatch.setattr("src.main.delete_watchlist_symbol", mock_delete_watchlist_symbol)
    monkeypatch.setattr("src.main.toggle_watchlist_symbol", mock_toggle_watchlist_symbol)
    monkeypatch.setattr("src.main.signal_stats", mock_signal_stats)
    monkeypatch.setattr("src.main.list_signals", mock_list_signals)
    return calls


@pytest.mark.asyncio
async def test_health_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["database"] is True
        assert data["redis"] is True
        assert "cron" in data
        assert data["timeframe"] == settings.TIMEFRAME
        assert data["version"] == "3.0.0"


@pytest.mark.asyncio
async def test_info_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "3.0.0"
        assert data["timeframe"] == settings.TIMEFRAME
        assert data["interval_ms"] == settings.TIMEFRAME_MS
        assert data["strategy"] == {
            "rsi_period": settings.RSI_PERIOD,
            "oversold": settings.RSI_OVERSOLD,
            "overbought": settings.RSI_OVERBOUGHT,
            "tp_percent": settings.TP_PERCENT,
            "sl_percent": settings.SL_PERCENT,
            "fill_window_candles": settings.FILL_WINDOW_CANDLES,
            "max_hold_candles": settings.MAX_HOLD_CANDLES,
            "fee_percent": settings.ROUND_TRIP_FEE_PERCENT,
        }
        assert "cron" in data
        assert "max_symbols" in data


@pytest.mark.asyncio
async def test_watchlist_lifecycle():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        # 1. List
        resp = await client.get("/api/watchlist")
        assert resp.status_code == 200
        assert "watchlist" in resp.json()

        # 2. Add
        add_resp = await client.post("/api/watchlist", json={"symbol": "TESTCOINUSDT", "notes": "Automated Test"})
        assert add_resp.status_code == 200
        assert add_resp.json()["status"] == "success"

        # 3. Toggle
        toggle_resp = await client.patch("/api/watchlist/TESTCOINUSDT/toggle", json={"is_active": False})
        assert toggle_resp.status_code == 200
        assert toggle_resp.json()["is_active"] is False

        # 4. Delete
        del_resp = await client.delete("/api/watchlist/TESTCOINUSDT")
        assert del_resp.status_code == 200


@pytest.mark.asyncio
async def test_signals_summary_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tp_hit_count"] == 1
        assert data["open_count"] == 1
        assert data["not_filled_count"] == 1
        assert data["fill_rate"] == 66.7
        assert data["winrate"] == 100.0
        assert data["closed_trades"] == 1
        assert data["total_net_pnl"] == 1.9
        assert "watchlist_count" in data
        assert "latest_signal_time" in data


@pytest.mark.asyncio
async def test_signals_list_and_filters(mock_db_services):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/signals?status=PENDING,OPEN&side=LONG&date=2025-10-11&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["limit"] == 10
        assert data["signals"][0]["status"] == "TP_HIT"
        assert data["summary"]["total_signals"] == 3
    kwargs = mock_db_services["signals"][0]
    assert kwargs["status"] == "PENDING,OPEN"
    assert kwargs["date"] == "2025-10-11"
    assert kwargs["timeframe"] is None  # no default timeframe filter


@pytest.mark.asyncio
async def test_signals_bad_date_returns_422():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/signals?date=11-10-2025")
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_single_signal(monkeypatch):
    async def fake_get_signal(signal_id):
        return {**SIGNAL, "journal": None} if signal_id == 1 else None

    monkeypatch.setattr("src.main.get_signal", fake_get_signal)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/signals/1")
        assert resp.status_code == 200
        assert resp.json()["signal_candle_ms"] == SIGNAL["signal_candle_ms"]
        assert (await client.get("/api/signals/2")).status_code == 404


@pytest.mark.asyncio
async def test_cron_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        status_resp = await client.get("/api/cron/status")
        assert status_resp.status_code == 200
        status_data = status_resp.json()
        assert "enabled" in status_data
        assert status_data["timeframe"] == settings.TIMEFRAME
        assert status_data["interval_ms"] == settings.TIMEFRAME_MS


@pytest.mark.asyncio
async def test_cron_trigger_success_and_error(monkeypatch):
    results = iter([{"status": "success", "new_signals": 2}, {"status": "error", "message": "Database not ready"}])

    async def fake_trigger():
        return next(results)

    monkeypatch.setattr("src.main.cron_scheduler.trigger_now", fake_trigger)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        ok = await client.post("/api/cron/trigger")
        assert ok.status_code == 200
        assert ok.json()["result"]["new_signals"] == 2
        assert ok.json()["summary"]["total_signals"] == 3

        failed = await client.post("/api/cron/trigger")
        assert failed.status_code == 503
        assert failed.json()["detail"] == "Database not ready"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/history"),
        ("GET", "/api/scan"),
        ("GET", "/api/macd-scan"),
        ("GET", "/api/klines?symbol=BTCUSDT"),
        ("POST", "/api/evaluate"),
        ("POST", "/api/profitloss-history/evaluate"),
        ("GET", "/api/profitloss-history"),
        ("POST", "/api/profitloss-history"),
        ("GET", "/api/signals/1/candles"),
    ],
)
async def test_removed_routes_are_gone(method, path):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        resp = await client.request(method, path)
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_key_protection():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        fail_resp = await client.post("/api/watchlist", json={"symbol": "KEYTESTUSDT"})
        assert fail_resp.status_code == 401

        ok_resp = await client.post(
            "/api/watchlist",
            json={"symbol": "KEYTESTUSDT"},
            headers=AUTH_HEADERS,
        )
        assert ok_resp.status_code == 200

        bearer_resp = await client.post(
            "/api/watchlist",
            json={"symbol": "KEYTEST2USDT"},
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )
        assert bearer_resp.status_code == 200

        await client.delete("/api/watchlist/KEYTESTUSDT", headers=AUTH_HEADERS)
        await client.delete("/api/watchlist/KEYTEST2USDT", headers=AUTH_HEADERS)


@pytest.mark.asyncio
async def test_api_key_unset_returns_503(monkeypatch):
    monkeypatch.setattr("src.main.API_SECRET_KEY", "")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/watchlist", json={"symbol": "KEYTESTUSDT"}, headers=AUTH_HEADERS)
        assert resp.status_code == 503
        assert resp.json()["detail"] == "API key not configured on server"

        no_header_resp = await client.post("/api/cron/trigger")
        assert no_header_resp.status_code == 503


@pytest.mark.asyncio
async def test_api_key_placeholder_returns_503(monkeypatch):
    placeholder = "your_secret_api_key_here"
    monkeypatch.setattr("src.main.API_SECRET_KEY", placeholder)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/watchlist", json={"symbol": "KEYTESTUSDT"}, headers={"X-API-KEY": placeholder}
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == "API key not configured on server"


@pytest.mark.asyncio
async def test_api_key_wrong_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/watchlist",
            json={"symbol": "KEYTESTUSDT"},
            headers={"X-API-KEY": "wrong_key"},
        )
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"

        bearer_resp = await client.post(
            "/api/watchlist",
            json={"symbol": "KEYTESTUSDT"},
            headers={"Authorization": "Bearer wrong_key"},
        )
        assert bearer_resp.status_code == 401
