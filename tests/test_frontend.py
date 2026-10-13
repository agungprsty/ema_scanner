from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src import main
from src.main import app

pytestmark = pytest.mark.unit

API_KEY = "frontend_test_key"
APP_JS = Path(__file__).resolve().parent.parent / "src" / "static" / "app.js"


@pytest.fixture
def added_symbols(monkeypatch):
    calls = []

    async def mock_add_watchlist_symbol(symbol: str, notes: str = ""):
        calls.append((symbol, notes))
        return {"id": len(calls), "symbol": symbol.strip().upper(), "is_active": True, "notes": notes}

    monkeypatch.setattr("src.main.add_watchlist_symbol", mock_add_watchlist_symbol)
    monkeypatch.setattr(main, "API_SECRET_KEY", API_KEY)
    return calls


async def _post_watchlist(payload: dict):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/watchlist", json=payload, headers={"X-API-KEY": API_KEY})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbol",
    [
        "<img src=x onerror=alert(1)>",
        "BTC<script>",
        'BTC"USDT',
        "BTC USDT",
        "BTC/USDT",
        "../etc",
        "A",
        "A" * 21,
        "",
    ],
)
async def test_watchlist_add_rejects_malicious_symbol(added_symbols, symbol):
    resp = await _post_watchlist({"symbol": symbol, "notes": "x"})
    assert resp.status_code == 422
    assert added_symbols == []


@pytest.mark.asyncio
async def test_watchlist_add_rejects_oversized_notes(added_symbols):
    resp = await _post_watchlist({"symbol": "BTCUSDT", "notes": "n" * 101})
    assert resp.status_code == 422
    assert added_symbols == []


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["BTCUSDT", "1000PEPEUSDT", "ethusdt"])
async def test_watchlist_add_accepts_valid_symbol(added_symbols, symbol):
    resp = await _post_watchlist({"symbol": symbol, "notes": "<b>note</b>"})
    assert resp.status_code == 200
    assert added_symbols == [(symbol, "<b>note</b>")]


def test_app_js_defines_escape_helper():
    source = APP_JS.read_text()
    assert "function escapeHtml(" in source
    for needle in (
        "${item.symbol}",
        "${item.notes",
        "${s.symbol}",
        "${s.side}",
        "${item.direction}",
        "${item.timeframe",
        "/api/watchlist/${sym}",
        "symbol=${sig.symbol}",
    ):
        assert needle not in source


def test_app_js_does_not_scan_on_page_load():
    source = APP_JS.read_text()
    calls = [line.strip() for line in source.splitlines() if "runScan" in line]
    assert calls == ["async function runScan() {", "btnScan.addEventListener('click', runScan);"]
    assert "renderScanView();\n});" in source


def test_app_js_pages_lists_twenty_rows_at_a_time():
    source = APP_JS.read_text()
    assert "const PAGE_SIZE = 20;" in source
    assert "limit: '100'" not in source
    for loader in ("pageParams('history')", "pageParams('profitloss')", "pageParams('journal')"):
        assert loader in source
