import pytest
from httpx import ASGITransport, AsyncClient

from src.main import app

pytestmark = pytest.mark.unit

TEST_API_KEY = "test_api_secret_key"
AUTH_HEADERS = {"X-API-KEY": TEST_API_KEY}


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
    history_store = []

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

    async def mock_get_macd_signals_summary():
        return {
            "total_signals": 10,
            "long_signals": 6,
            "short_signals": 4,
            "symbols_count": 2,
            "tp1_hit_count": 5,
            "sl_hit_count": 2,
            "pending_count": 3,
            "winrate": 71.4,
            "target_rr": 1.5,
            "watchlist_count": len(watchlist_store),
            "latest_signal_time": "2026-10-11 00:00:00",
            "total_net_pnl": 12.5,
            "closed_trades": 7,
        }

    async def mock_get_macd_signals(*args, **kwargs):
        signals = [
            {
                "id": 1,
                "symbol": "BTCUSDT",
                "side": "LONG",
                "timeframe": "2h",
                "reason": "MACD_BULLISH_CROSS",
                "cross_candle_ms": 1728500000000,
                "cross_time": "2026-10-11 00:00:00",
                "cross_price": 60000.0,
                "close_price": 60050.0,
                "entry_price": 60050.0,
                "stop_loss": 59000.0,
                "tp1": 61500.0,
                "risk_reward_ratio": 1.5,
                "win_status": "PROFIT",
                "macd": 10.5,
                "signal": 8.2,
                "histogram": 2.3,
                "bars_ago": 0,
                "is_confirmed": True,
            }
        ]
        return signals, len(signals), {"open_count": 0, "closed_count": len(signals), "total_r": 1.5}

    async def mock_record_profit_loss(**kwargs):
        item = {
            "id": len(history_store) + 1,
            **kwargs,
        }
        history_store.append(item)
        return item

    async def mock_get_profitloss_history(
        symbol=None,
        direction=None,
        timeframe=None,
        result=None,
        date=None,
        start_date=None,
        end_date=None,
        limit=50,
        offset=0,
    ):
        summary = {
            "total_trades": len(history_store),
            "profit_count": len([x for x in history_store if x.get("result") == "PROFIT"]),
            "loss_count": len([x for x in history_store if x.get("result") == "LOSS"]),
            "winrate": 100.0 if history_store else 0.0,
            "total_pnl_percent": sum(x.get("pnl_percent", 0.0) for x in history_store),
        }
        return history_store, len(history_store), summary

    monkeypatch.setattr("src.main.is_db_connected", mock_is_db_connected)
    monkeypatch.setattr("src.main.is_redis_connected", mock_is_redis_connected)
    monkeypatch.setattr("src.main.get_watchlist", mock_get_watchlist)
    monkeypatch.setattr("src.main.add_watchlist_symbol", mock_add_watchlist_symbol)
    monkeypatch.setattr("src.main.delete_watchlist_symbol", mock_delete_watchlist_symbol)
    monkeypatch.setattr("src.main.toggle_watchlist_symbol", mock_toggle_watchlist_symbol)
    monkeypatch.setattr("src.main.get_macd_signals_summary", mock_get_macd_signals_summary)
    monkeypatch.setattr("src.main.get_macd_signals", mock_get_macd_signals)
    monkeypatch.setattr("src.main.record_profit_loss", mock_record_profit_loss)
    monkeypatch.setattr("src.main.get_profitloss_history", mock_get_profitloss_history)


@pytest.mark.asyncio
async def test_health_endpoint(monkeypatch):
    async def fresh():
        return True

    monkeypatch.setattr("src.main.cron_scheduler.is_fresh", fresh)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        # Minimal on purpose: booleans only, no scheduler internals or error text
        assert resp.json() == {"status": "healthy", "db": True, "redis": True, "cron_fresh": True, "version": "2.2.0"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("db_ok", "redis_ok", "cron_fresh", "status", "http_status"),
    [
        (True, True, None, "healthy", 200),
        (True, True, False, "degraded", 200),
        (True, False, True, "degraded", 200),
        (False, True, True, "down", 503),
        (False, False, False, "down", 503),
    ],
)
async def test_health_reports_stale_cron_and_outages(monkeypatch, db_ok, redis_ok, cron_fresh, status, http_status):
    async def db():
        return db_ok

    async def redis():
        return redis_ok

    async def fresh():
        return cron_fresh

    monkeypatch.setattr("src.main.is_db_connected", db)
    monkeypatch.setattr("src.main.is_redis_connected", redis)
    monkeypatch.setattr("src.main.cron_scheduler.is_fresh", fresh)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
    assert resp.status_code == http_status
    data = resp.json()
    assert data["status"] == status
    assert (data["db"], data["redis"], data["cron_fresh"]) == (db_ok, redis_ok, cron_fresh)
    assert set(data) == {"status", "db", "redis", "cron_fresh", "version"}


@pytest.mark.asyncio
async def test_info_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enforced_timeframe"] == "2h"
        assert "cron" in data
        assert data["mode"] == "SIGNALS_ONLY"
        assert data["edge_validated"] is False
        assert data["strategy_key"] == "macd_cross"
        assert data["strategy_version"]
        assert data["cost_model"]["version"] == "cm1"
        assert data["cost_model"]["stop_slippage_multiplier"] > 1


@pytest.mark.asyncio
async def test_info_reports_signals_only_even_with_dry_run_off(monkeypatch):
    monkeypatch.setattr("src.config.settings.DRY_RUN", False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        data = (await client.get("/api/info")).json()
    assert data["mode"] == "SIGNALS_ONLY"
    assert data["edge_validated"] is False


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
        assert "winrate" in data
        assert "tp1_hit_count" in data
        assert "sl_hit_count" in data
        assert "watchlist_count" in data
        assert "total_net_pnl" in data
        assert "closed_trades" in data
        assert data["target_rr"] == 1.5
        assert "avg_rr" not in data


def test_summary_marks_summed_percent_deprecated():
    schema = app.openapi()["components"]["schemas"]["SummaryResponse"]["properties"]
    assert schema["total_net_pnl"]["deprecated"] is True
    assert "avg_rr" not in schema


@pytest.mark.asyncio
async def test_performance_endpoint_is_public_and_passes_filters(monkeypatch):
    from src.services import db

    calls = []

    async def mock_get_performance(strategy_version=None, symbol=None, side=None):
        calls.append((strategy_version, symbol, side))
        return db._performance_payload(strategy_version or "2.2.0", symbol, side, [], 2, {"legacy": 4})

    monkeypatch.setattr("src.main.get_performance", mock_get_performance)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/performance?strategy_version=2.2.0&symbol=ETHUSDT&side=LONG")
        assert resp.status_code == 200
        data = resp.json()
        assert calls == [("2.2.0", "ETHUSDT", "LONG")]
        assert data["n_closed"] == 0
        assert data["open_count"] == 2
        assert data["excluded"] == {"legacy": 4, "catchup": 0, "manual": 0, "other_version": 0, "outside_ledger": 0, "other": 0}
        assert data["min_sample_for_totals"] == 100
        assert data["sample_size_warning"] == "insufficient sample"
        assert data["mean_net_r_ci95"] is None
        assert "win_rate" in data["definitions"]

        resp = await client.get("/api/performance")
        assert resp.status_code == 200
        assert calls[-1] == (None, None, None)

        assert (await client.get("/api/performance?side=UP")).status_code == 422


@pytest.mark.asyncio
async def test_bad_date_filter_is_a_validation_error(monkeypatch):
    from src.services import db

    async def strict(**kwargs):
        db.utc_day_bounds(kwargs.get("start_date"), kwargs.get("end_date"))
        return [], 0, {}

    monkeypatch.setattr("src.main.get_macd_signals", strict)
    monkeypatch.setattr("src.main.get_profitloss_history", strict)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/api/signals", "/api/profitloss-history"):
            resp = await client.get(f"{path}?end_date=2026-10-32")
            assert resp.status_code == 422
            assert "YYYY-MM-DD" in resp.json()["detail"]
            assert (await client.get(f"{path}?start_date=2026-10-12&end_date=2026-10-13")).status_code == 200


@pytest.mark.asyncio
async def test_signals_list_and_filters():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/signals?limit=10&direction=LONG&result=PROFIT&timeframe=2h")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "signals" in data
        assert isinstance(data["signals"], list)
        assert data["summary"]["total_r"] == 1.5


@pytest.mark.asyncio
async def test_profitloss_history_lifecycle():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        # 1. Add record
        record_payload = {
            "symbol": "ETHUSDT",
            "direction": "LONG",
            "entry_price": 2500.0,
            "stop_loss": 2450.0,
            "tp1": 2575.0,
            "exit_price": 2575.0,
            "result": "PROFIT",
            "timeframe": "2h",
            "pnl_percent": 3.0,
            "risk_reward_ratio": 1.5,
            "cross_time": "2026-10-11 00:00:00",
            "exit_time": "2026-10-11 02:00:00",
        }
        add_resp = await client.post("/api/profitloss-history", json=record_payload)
        assert add_resp.status_code == 200
        assert add_resp.json()["status"] == "success"

        # 2. Get with filters
        get_resp = await client.get("/api/profitloss-history?direction=LONG&result=PROFIT&symbol=ETHUSDT")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["total"] >= 1
        assert "summary" in data
        assert data["summary"]["profit_count"] >= 1
        assert len(data["history"]) >= 1
        assert data["history"][0]["symbol"] == "ETHUSDT"
        assert data["history"][0]["result"] == "PROFIT"


@pytest.mark.asyncio
async def test_cron_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Check status
        status_resp = await client.get("/api/cron/status")
        assert status_resp.status_code == 200
        status_data = status_resp.json()
        assert "enabled" in status_data
        assert "interval_hours" in status_data
        assert status_data["timeframe"] == "2h"


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


@pytest.mark.asyncio
async def test_manual_profitloss_rejects_unknown_results_and_stored_signals(monkeypatch):
    from src.services.db import SignalTradeConflict

    async def conflict(**kwargs):
        if kwargs.get("signal_id") is not None:
            raise SignalTradeConflict("signal 11 is a stored signal")
        return {"id": 1, **kwargs}

    monkeypatch.setattr("src.main.record_profit_loss", conflict)
    payload = {
        "symbol": "BTCUSDT", "direction": "short", "entry_price": 82282.3, "stop_loss": 84285.8352,
        "tp1": 79276.9972, "exit_price": 79276.9972, "result": "profit",
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=AUTH_HEADERS) as client:
        ok = await client.post("/api/profitloss-history", json=payload)
        assert ok.status_code == 200
        assert (ok.json()["item"]["direction"], ok.json()["item"]["result"]) == ("SHORT", "PROFIT")

        assert (await client.post("/api/profitloss-history", json={**payload, "result": "WIN"})).status_code == 422
        assert (await client.post("/api/profitloss-history", json={**payload, "direction": "UP"})).status_code == 422

        clash = await client.post("/api/profitloss-history", json={**payload, "signal_id": 11})
        assert clash.status_code == 409
        assert "stored signal" in clash.json()["detail"]


@pytest.mark.asyncio
async def test_health_answers_in_time_when_a_dependency_hangs(monkeypatch):
    import asyncio
    import time

    async def hang():
        # A hung connection whose cleanup is slow too, like an asyncpg session on a paused database
        try:
            await asyncio.sleep(30)
        finally:
            await asyncio.shield(asyncio.sleep(1))
        return True

    async def ok():
        return True

    monkeypatch.setattr("src.main.HEALTH_PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("src.main.cron_scheduler.is_fresh", ok)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr("src.main.is_redis_connected", hang)
        started = time.monotonic()
        resp = await client.get("/api/health")
        assert (resp.status_code, resp.json()["status"], resp.json()["redis"]) == (200, "degraded", False)

        # A database that does not answer is down, and the endpoint still answers at once
        monkeypatch.setattr("src.main.is_db_connected", hang)
        resp = await client.get("/api/health")
        assert (resp.status_code, resp.json()["status"], resp.json()["db"]) == (503, "down", False)
        # Neither answer waited for the hung check's cleanup
        assert time.monotonic() - started < 0.9


@pytest.mark.asyncio
@pytest.mark.parametrize(("require_cron", "status"), [(False, "healthy"), (True, "degraded")])
async def test_health_with_the_scheduler_off(monkeypatch, require_cron, status):
    async def disabled():
        return None

    monkeypatch.setattr("src.main.REQUIRE_CRON", require_cron)
    monkeypatch.setattr("src.main.cron_scheduler.is_fresh", disabled)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert (resp.json()["status"], resp.json()["cron_fresh"]) == (status, None)


@pytest.mark.asyncio
async def test_signals_take_a_local_day_offset(monkeypatch):
    calls = []

    async def capture(**kwargs):
        calls.append(kwargs)
        return [], 0, {}

    monkeypatch.setattr("src.main.get_macd_signals", capture)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/signals?date=2026-10-15&tz_offset_minutes=420")).status_code == 200
        assert calls[-1]["tz_offset_minutes"] == 420
        assert (await client.get("/api/signals")).status_code == 200
        assert calls[-1]["tz_offset_minutes"] == 0
        assert (await client.get("/api/signals?tz_offset_minutes=900")).status_code == 422


def test_as_booked_summary_fields_are_marked_deprecated():
    schema = app.openapi()["components"]["schemas"]
    for model, fields in (
        ("ProfitLossSummary", ("winrate", "total_r", "avg_r", "total_pnl_percent")),
        ("SignalsSummary", ("total_r", "total_pnl_percent")),
    ):
        props = schema[model]["properties"]
        for field in fields:
            assert props[field]["deprecated"] is True
        assert props["eligible_net_r"].get("deprecated") is None
