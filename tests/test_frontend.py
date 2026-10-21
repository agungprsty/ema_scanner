import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.security import Principal, require_admin
from src.main import app

pytestmark = pytest.mark.unit

APP_JS = Path(__file__).resolve().parent.parent / "src" / "static" / "app.js"
PAGES_DIR = Path(__file__).resolve().parent.parent / "src" / "pages"
DASHBOARD_HTML = PAGES_DIR / "dashboard.html"
INDEX_HTML = DASHBOARD_HTML


@pytest.fixture
def added_symbols(monkeypatch):
    calls = []

    async def mock_add_watchlist_symbol(symbol: str, notes: str = ""):
        calls.append((symbol, notes))
        return {"id": len(calls), "symbol": symbol.strip().upper(), "is_active": True, "notes": notes}

    monkeypatch.setattr("src.main.add_watchlist_symbol", mock_add_watchlist_symbol)
    app.dependency_overrides[require_admin] = lambda: Principal(id=1, username="admin", role="administrator")
    yield calls
    app.dependency_overrides.pop(require_admin, None)


async def _post_watchlist(payload: dict):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/watchlist", json=payload)


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


def test_app_js_runs_the_job_only_on_click():
    source = APP_JS.read_text()
    calls = [line.strip() for line in source.splitlines() if "runNow" in line]
    assert calls == ["async function runNow() {", "btnScan.addEventListener('click', runNow);"]
    assert source.count("/api/cron/trigger") == 1
    assert "fetch('/api/cron/trigger', { method: 'POST' })" in source


def test_frontend_has_no_macd_era_remnants():
    source = APP_JS.read_text()
    html = INDEX_HTML.read_text()
    for needle in ("macd", "/api/klines", "/api/evaluate", "/api/scan", "include_current", "forming"):
        assert needle not in source.lower()
        assert needle not in html.lower()
    for needle in (
        "timeframe: '2h'",
        "CANDLE_MS",
        "cross_time",
        "tp1",
        "win_status",
        "lookback",
        "'/history'",
        "/candles",
        "round_trip_fee_percent",
    ):
        assert needle not in source
    assert "select-lookback" not in html and "btn-eval" not in html


def test_tabs_split_active_and_closed_signals():
    html = INDEX_HTML.read_text()
    source = APP_JS.read_text()
    for tab in ("active", "history", "journal", "watchlist"):
        assert f'data-tab="{tab}"' in html
    assert 'data-tab="scan"' not in html
    assert "active: ['PENDING', 'OPEN']," in source
    assert "history: ['TP_HIT', 'SL_HIT', 'EXPIRED', 'NOT_FILLED']," in source
    assert "status: statuses.join(',')" in source
    assert "params.append('side'" in source and "'direction'" not in source


def test_status_filter_and_labels_cover_every_status():
    html = INDEX_HTML.read_text()
    source = APP_JS.read_text()
    for status in ("PENDING", "OPEN", "TP_HIT", "SL_HIT", "EXPIRED", "NOT_FILLED"):
        assert f'<option value="{status}">' in html
        assert f"    {status}: {{ cls: " in source
    assert "PENDING: { cls: '', label: 'Awaiting fill' }" in source


def test_dashboard_shows_fill_rate_and_brand():
    html = INDEX_HTML.read_text()
    assert 'id="stat-fillrate"' in html
    assert "data.fill_rate" in APP_JS.read_text()
    assert '<span class="brand-name">simple-trade</span>' in html
    assert '<script src="/static/app.js?v=5"></script>' in html


def test_journal_preview_uses_planned_risk():
    source = APP_JS.read_text()
    assert "function tradeReturn(sig, entry, exit) {" in source
    assert "Math.abs(sig.entry_price - sig.stop_loss) / sig.entry_price * 100" in source
    assert "tradeReturn(currentSignal, entry, exit)" in source
    assert "Object.assign(config, info.strategy || {});" in source


def test_chart_draws_rsi_pane_and_skips_levels_when_not_filled():
    source = APP_JS.read_text()
    assert "/chart`" in source
    assert "priceScaleId: 'rsi'" in source
    assert "[config.oversold, config.overbought]" in source
    assert "time: p.time + UTC7_OFFSET_SEC" in source
    assert "intervalMs(sig.timeframe)" in source
    assert "if (sig.status !== 'NOT_FILLED') {" in source
    for marker in ("'Signal'", "'Fill'", "'Exit'"):
        assert f"text: {marker}" in source


def test_app_js_pages_lists_twenty_rows_at_a_time():
    source = APP_JS.read_text()
    assert "const PAGE_SIZE = 20;" in source
    assert "limit: '100'" not in source
    assert "page: String(pageIndex[tab] + 1), per_page: String(PAGE_SIZE)" in source
    assert "offset" not in source
    for loader in ("pageParams(tab)", "pageParams('journal')"):
        assert loader in source


def test_dashboard_has_no_separate_closed_trades_tab():
    html = INDEX_HTML.read_text()
    assert 'data-tab="profitloss"' not in html
    assert "loadProfitLossHistory" not in APP_JS.read_text()


def test_chart_library_loads_on_demand():
    html = INDEX_HTML.read_text()
    assert "lightweight-charts" not in html.split("<body>")[0].replace('href="https://unpkg.com"', "")
    assert "function loadChartLibrary(" in APP_JS.read_text()


def test_app_js_reads_api_envelopes():
    source = APP_JS.read_text()
    assert "X-API-KEY" not in source
    assert "prompt(" not in source
    # The old API key is only ever removed, never read or sent
    assert "localStorage.removeItem('crossline_api_key')" in source
    assert "getItem" not in source and "Bearer" not in source
    assert "page.meta.total_data" in source
    assert "`/api/summary?${filterParams(base)}`" in source
    assert "getJson('/api/journal/summary')" in source
    assert "body.message" in source and "body.errors" in source
    assert "res.headers.get('Retry-After')" in source
    for stale in ("data.signals", "data.entries", "data.watchlist", "data.summary", ".detail"):
        assert stale not in source


def test_pages_satisfy_csp():
    # APP_CSP has no 'unsafe-inline' for scripts: no inline scripts or handlers, no eval
    pages = [
        PAGES_DIR / "landing.html",
        PAGES_DIR / "login.html",
        PAGES_DIR / "register.html",
        PAGES_DIR / "dashboard.html",
    ]
    scripts = [
        APP_JS,
        APP_JS.parent / "auth.js",
    ]
    for page in pages:
        html = page.read_text()
        assert not re.search(r"\son[a-z]+\s*=", html), f"{page.name} contains inline event handler"
        assert "javascript:" not in html, f"{page.name} contains javascript: protocol"
        # Scripts should only be external script tags
        script_tags = re.findall(r"<script[^>]*>", html)
        for tag in script_tags:
            assert 'src="/static/' in tag, f"Unexpected script tag in {page.name}: {tag}"

    for script_file in scripts:
        source = script_file.read_text()
        for needle in ("eval(", "new Function", "innerHTML = `<script"):
            assert needle not in source, f"Forbidden '{needle}' found in {script_file.name}"
