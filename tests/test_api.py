import pytest
from httpx import ASGITransport, AsyncClient

from src.main import app

pytestmark = pytest.mark.unit


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
            "avg_rr": 1.5,
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
        return signals, len(signals)

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
async def test_health_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "database" in data
        assert "redis" in data
        assert "cron" in data
        assert data["timeframe"] == "2h"


@pytest.mark.asyncio
async def test_info_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enforced_timeframe"] == "2h"
        assert "cron" in data


@pytest.mark.asyncio
async def test_watchlist_lifecycle():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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


@pytest.mark.asyncio
async def test_profitloss_history_lifecycle():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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
async def test_api_key_protection(monkeypatch):
    from src import main

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Without key set, should succeed
        resp = await client.post("/api/watchlist", json={"symbol": "KEYTESTUSDT"})
        assert resp.status_code == 200

        # Enable API_SECRET_KEY
        monkeypatch.setattr(main, "API_SECRET_KEY", "super_secret_vps_key")

        # Request without key should fail with 401
        fail_resp = await client.post("/api/watchlist", json={"symbol": "KEYTEST2USDT"})
        assert fail_resp.status_code == 401

        # Request with correct X-API-KEY should succeed
        ok_resp = await client.post(
            "/api/watchlist",
            json={"symbol": "KEYTEST2USDT"},
            headers={"X-API-KEY": "super_secret_vps_key"},
        )
        assert ok_resp.status_code == 200

        # Cleanup
        await client.delete(
            "/api/watchlist/KEYTESTUSDT",
            headers={"X-API-KEY": "super_secret_vps_key"},
        )
        await client.delete(
            "/api/watchlist/KEYTEST2USDT",
            headers={"X-API-KEY": "super_secret_vps_key"},
        )
