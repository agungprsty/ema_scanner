import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.security import Principal, require_admin
from src.main import app

pytestmark = pytest.mark.unit

STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "static"
APP_JS = STATIC_DIR / "app.js"
COMMON_JS = STATIC_DIR / "common.js"
SIGNAL_JS = STATIC_DIR / "signal.js"
PAGES_DIR = Path(__file__).resolve().parent.parent / "src" / "pages"
DASHBOARD_HTML = PAGES_DIR / "dashboard.html"
SIGNAL_HTML = PAGES_DIR / "signal.html"
INDEX_HTML = DASHBOARD_HTML


def all_js() -> str:
    """Every script the signed-in pages load; helpers live in common.js, the detail page in signal.js."""
    return "\n".join(f.read_text() for f in (COMMON_JS, APP_JS, SIGNAL_JS))


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
    source = all_js()
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
    source = all_js()
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
    source = COMMON_JS.read_text()
    for status in ("PENDING", "OPEN", "TP_HIT", "SL_HIT", "EXPIRED", "NOT_FILLED"):
        assert f'<option value="{status}">' in html
        assert f"  {status}: {{ cls: " in source
    assert "PENDING: { cls: '', label: 'Awaiting fill' }" in source


def test_dashboard_shows_fill_rate_and_brand():
    html = INDEX_HTML.read_text()
    assert 'id="stat-fillrate"' in html
    assert "data.fill_rate" in APP_JS.read_text()
    assert '<span class="brand-name">simple-trade</span>' in html
    assert '<script src="/static/common.js?v=2"></script>\n  <script src="/static/app.js?v=6"></script>' in html


def test_journal_preview_uses_planned_risk():
    source = all_js()
    assert "function tradeReturn(sig, entry, exit) {" in source
    assert "Math.abs(sig.entry_price - sig.stop_loss) / sig.entry_price * 100" in source
    assert "tradeReturn(currentSignal, entry, exit)" in source
    assert "Object.assign(config, info.strategy || {});" in source


def test_chart_draws_rsi_pane_and_skips_levels_when_not_filled():
    source = SIGNAL_JS.read_text()
    assert "/chart?v=2`" in source
    assert "autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } })," in source
    assert "[config.oversold, config.overbought]" in source
    assert "time: p.time + UTC7_OFFSET_SEC" in source
    assert "intervalMs(sig.timeframe)" in source
    assert "if (sig.status !== 'NOT_FILLED') {" in source
    for marker in ("'Signal'", "'Fill'", "'Exit'"):
        assert f"text: {marker}" in source


def test_chart_draws_ema_and_volume_delta_below_rsi():
    source = SIGNAL_JS.read_text()
    assert "lightweight-charts@5.0.8/" in source
    assert "emaSeries.setData(shift(payload.ema));" in source
    assert "shift(payload.volume_delta)" in source
    # Real panes, top to bottom: price with EMA (0), RSI (1), then volume delta (2)
    assert "chart.addSeries(LightweightCharts.LineSeries, {" in source
    assert "}, 1);\n      const deltaSeries = chart.addSeries(LightweightCharts.HistogramSeries, {" in source
    assert "}, 2);\n      const [pricePane, rsiPane, deltaPane] = chart.panes();" in source
    # Entry/TP/SL widen the price scale so the lines never fall outside the price pane
    assert "Math.min(range.priceRange.minValue, ...levels)" in source
    html = SIGNAL_HTML.read_text()
    assert "EMA 50" in html and "Volume delta" in html


def test_signal_detail_is_a_page_not_a_dialog():
    dashboard = DASHBOARD_HTML.read_text()
    source = APP_JS.read_text()
    assert "<dialog" not in dashboard and "signal-modal" not in source
    assert "`/signals/${encodeURIComponent(item.id)}`" in source
    html = SIGNAL_HTML.read_text()
    assert 'id="journal-form"' in html and 'class="journal admin-only"' in html
    assert html.index("/static/common.js") < html.index("/static/signal.js")


def test_hidden_attribute_beats_component_display_rules():
    # app.css loads before the dashboard's inline .pager / .filters rules
    assert "[hidden] { display: none !important; }" in (STATIC_DIR / "app.css").read_text()
    assert "body:not([data-role]) .admin-only," in (STATIC_DIR / "base.css").read_text()


def test_dashboard_view_survives_a_visit_to_a_signal():
    source = APP_JS.read_text()
    assert "history.replaceState(null, '', query ? `?${query}` : window.location.pathname);" in source
    assert "selectTab(restoreViewFromUrl());" in source
    assert "from.pathname === '/dashboard'" in SIGNAL_JS.read_text()


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
    for page in (DASHBOARD_HTML, SIGNAL_HTML):
        assert "lightweight-charts" not in page.read_text()
    assert "function loadChartLibrary(" in SIGNAL_JS.read_text()
    assert "LightweightCharts" not in APP_JS.read_text()


def test_app_js_reads_api_envelopes():
    source = all_js()
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
        SIGNAL_HTML,
    ]
    scripts = [APP_JS, COMMON_JS, SIGNAL_JS, STATIC_DIR / "auth.js"]
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
