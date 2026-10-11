import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from src.main import app
from src.services.db import close_db, init_db


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    await init_db()
    yield
    await close_db()


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
