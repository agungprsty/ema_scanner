import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from src.config import settings
from src.main import app
from src.services import db

pytestmark = pytest.mark.unit

AUTH_COOKIES = {"st_session": "test_api_session_token"}

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
def mock_db_services(monkeypatch):
    watchlist_store = {
        "BTCUSDT": {"symbol": "BTCUSDT", "is_active": True, "notes": "Default"},
        "ETHUSDT": {"symbol": "ETHUSDT", "is_active": True, "notes": "Default"},
    }
    calls = {"signals": [], "stats": []}

    async def mock_is_db_connected():
        return True

    async def fake_get_session_user(token_hash):
        if token_hash == hashlib.sha256(b"test_api_session_token").hexdigest():
            return {"id": 1, "username": "admin", "role": "administrator", "email": "admin@example.com"}
        return None

    monkeypatch.setattr("src.api.security.get_session_user", fake_get_session_user)

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
        calls["stats"].append(conditions)
        counts = {"total": 3, "long": 2, "short": 1, "symbols": 2, "TP_HIT": 1, "NOT_FILLED": 1, "OPEN": 1}
        return db.build_stats(counts, 1.9, [1.9])

    async def mock_list_signals(**kwargs):
        calls["signals"].append(kwargs)
        return [SIGNAL], 101, {}

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
        assert '"status":"healthy"' in resp.text  # the deploy gate greps the compact body
        body = resp.json()
        assert body["code"] == 200 and body["status"] == "success"
        assert body["meta"]["path"] == "/api/health"
        data = body["data"]
        assert data["status"] == "healthy"
        assert data["database"] is True
        assert data["redis"] is True
        assert "cron" in data
        assert data["timeframe"] == settings.TIMEFRAME
        assert data["version"] == "3.0.0"


@pytest.mark.asyncio
async def test_health_degraded_still_returns_200(monkeypatch):
    async def down():
        return False

    monkeypatch.setattr("src.main.is_redis_connected", down)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "degraded"
        assert '"status":"healthy"' not in resp.text


@pytest.mark.asyncio
async def test_info_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/info")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["app"] == "Simpletrade"
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
    async with AsyncClient(transport=transport, base_url="http://test", cookies=AUTH_COOKIES) as client:
        # 1. List
        resp = await client.get("/api/watchlist")
        assert resp.status_code == 200
        assert [w["symbol"] for w in resp.json()["data"]] == ["BTCUSDT", "ETHUSDT"]

        # 2. Add
        add_resp = await client.post("/api/watchlist", json={"symbol": "TESTCOINUSDT", "notes": "Automated Test"})
        assert add_resp.status_code == 200
        assert add_resp.json()["status"] == "success"
        assert add_resp.json()["data"]["symbol"] == "TESTCOINUSDT"

        # 3. Toggle
        toggle_resp = await client.patch("/api/watchlist/TESTCOINUSDT/toggle", json={"is_active": False})
        assert toggle_resp.status_code == 200
        assert toggle_resp.json()["data"] == {"symbol": "TESTCOINUSDT", "is_active": False}

        # 4. Delete
        del_resp = await client.delete("/api/watchlist/TESTCOINUSDT")
        assert del_resp.status_code == 200
        assert del_resp.json()["data"] == {"symbol": "TESTCOINUSDT"}

        # 5. Gone
        missing = await client.delete("/api/watchlist/TESTCOINUSDT")
        assert missing.status_code == 404
        assert missing.json()["message"] == "Symbol not found in watchlist"


@pytest.mark.asyncio
async def test_signals_summary_endpoint(mock_db_services):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/summary")).json()["data"]["total_signals"] == 3
        assert mock_db_services["stats"][-1] == []  # no filters: every signal

        resp = await client.get("/api/summary?status=TP_HIT&side=LONG&start_date=2025-10-01")
        assert resp.status_code == 200
        assert len(mock_db_services["stats"][-1]) == 3
        data = resp.json()["data"]
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
        resp = await client.get("/api/signals?status=PENDING,OPEN&side=LONG&date=2025-10-11&page=2&per_page=10")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"][0]["status"] == "TP_HIT"
        assert "summary" not in body
        meta = body["meta"]
        assert (meta["total_data"], meta["total_pages"], meta["current_page"], meta["per_page"]) == (101, 11, 2, 10)
        assert meta["path"] == "/api/signals"
    kwargs = mock_db_services["signals"][0]
    assert kwargs["status"] == "PENDING,OPEN"
    assert kwargs["date"] == "2025-10-11"
    assert kwargs["timeframe"] is None  # no default timeframe filter
    assert (kwargs["limit"], kwargs["offset"]) == (10, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/signals", "/api/summary"])
async def test_signals_bad_date_returns_422(path, mock_db_services):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"{path}?date=11-10-2025")
        assert resp.status_code == 422
        assert resp.json()["message"] == "Dates must be YYYY-MM-DD"
    assert mock_db_services["signals"] == [] and mock_db_services["stats"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["page=0", "page=1000001", "per_page=0", "per_page=201", "limit=10&offset=-1&page=x"]
)
async def test_signals_rejects_bad_paging(query):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/signals?{query}")
        assert resp.status_code == 422
        assert resp.json()["errors"][0]["field"] in ("page", "per_page")


@pytest.mark.asyncio
async def test_single_signal(monkeypatch):
    async def fake_get_signal(signal_id):
        return {**SIGNAL, "journal": None} if signal_id == 1 else None

    monkeypatch.setattr("src.main.get_signal", fake_get_signal)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/signals/1")
        assert resp.status_code == 200
        assert resp.json()["data"]["signal_candle_ms"] == SIGNAL["signal_candle_ms"]
        missing = await client.get("/api/signals/2")
        assert missing.status_code == 404
        assert missing.json()["message"] == "Signal not found"


@pytest.mark.asyncio
async def test_cron_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        status_resp = await client.get("/api/cron/status")
        assert status_resp.status_code == 200
        status_data = status_resp.json()["data"]
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
    async with AsyncClient(transport=transport, base_url="http://test", cookies=AUTH_COOKIES) as client:
        ok = await client.post("/api/cron/trigger")
        assert ok.status_code == 200
        assert ok.json()["data"]["result"]["new_signals"] == 2
        assert ok.json()["data"]["summary"]["total_signals"] == 3

        failed = await client.post("/api/cron/trigger")
        assert failed.status_code == 503
        assert failed.json()["message"] == "Scan failed, see server logs"
        assert "Database not ready" not in failed.text


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
    async with AsyncClient(transport=transport, base_url="http://test", cookies=AUTH_COOKIES) as client:
        resp = await client.request(method, path)
        assert resp.status_code == 404
        assert resp.json()["status"] == "error"


@pytest.mark.asyncio
async def test_session_cookie_protection():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        fail_resp = await client.post("/api/watchlist", json={"symbol": "KEYTESTUSDT"})
        assert fail_resp.status_code == 401
        assert fail_resp.json()["message"] == "Authentication required"

        ok_resp = await client.post("/api/watchlist", json={"symbol": "KEYTESTUSDT"}, cookies=AUTH_COOKIES)
        assert ok_resp.status_code == 200

        wrong_resp = await client.post(
            "/api/watchlist", json={"symbol": "KEYTESTUSDT"}, cookies={"st_session": "wrong_cookie"}
        )
        assert wrong_resp.status_code == 401
        assert wrong_resp.json()["message"] == "Authentication required"


def test_openapi_documents_envelopes():
    spec = app.openapi()
    assert spec["info"]["title"] == "Simpletrade"
    signals = spec["paths"]["/api/signals"]["get"]
    params = {p["name"] for p in signals["parameters"]}
    assert {"page", "per_page", "status", "date"} <= params and not {"limit", "offset"} & params
    assert signals["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("PageEnvelope_dict_")
    assert {"page", "per_page"} & {p["name"] for p in spec["paths"]["/api/summary"]["get"]["parameters"]} == set()
