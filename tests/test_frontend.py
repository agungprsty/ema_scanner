import base64
import hashlib
import re
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
    # The page opens on the recorded signals, never on a scan
    assert "selectTab('history');\n});" in source


def test_app_js_pages_lists_twenty_rows_at_a_time():
    source = APP_JS.read_text()
    assert "const PAGE_SIZE = 20;" in source
    assert "limit: '100'" not in source
    for loader in ("pageParams('history')", "pageParams('journal')"):
        assert loader in source


def test_dashboard_has_no_separate_closed_trades_tab():
    html = (APP_JS.parent / "index.html").read_text()
    assert 'data-tab="profitloss"' not in html
    assert "loadProfitLossHistory" not in APP_JS.read_text()


def test_chart_library_loads_on_demand():
    html = (APP_JS.parent / "index.html").read_text()
    assert "lightweight-charts" not in html.split("<body>")[0]
    assert "function loadChartLibrary(" in APP_JS.read_text()


VENDOR_CHART_LIB = APP_JS.parent / "vendor" / "lightweight-charts-4.2.1.standalone.production.js"


def test_chart_library_is_self_hosted_and_pinned_by_hash():
    source = APP_JS.read_text()
    html = (APP_JS.parent / "index.html").read_text()
    assert "unpkg" not in source and "unpkg" not in html
    assert "const CHART_LIB_URL = '/static/vendor/lightweight-charts-4.2.1.standalone.production.js';" in source

    lib = VENDOR_CHART_LIB.read_bytes()
    assert b"TradingView Lightweight Charts\xe2\x84\xa2 v4.2.1" in lib[:200]
    # The bytes published in the npm tarball of lightweight-charts@4.2.1
    assert hashlib.sha256(lib).hexdigest() == "197180bdf2185bb33f3cad878d0d29d2128b4fca96ed847a0de4dc64da1c5e97"
    integrity = re.search(r"const CHART_LIB_INTEGRITY = '(sha384-[^']+)';", source).group(1)
    assert integrity == "sha384-" + base64.b64encode(hashlib.sha384(lib).digest()).decode()
    assert "script.integrity = CHART_LIB_INTEGRITY;" in source


def test_api_key_never_lives_in_page_storage():
    source = APP_JS.read_text()
    assert "prompt(" not in source
    assert "localStorage.setItem" not in source
    assert "localStorage.getItem" not in source
    assert "X-API-KEY" not in source
    # Keys stored by older versions are wiped on load
    assert "STALE_KEY_STORAGE = ['crossline_api_key', 'tokyo_api_key']" in source
    assert "localStorage.removeItem(key)" in source


def test_owner_requests_use_the_session_cookie_with_the_csrf_header():
    source = APP_JS.read_text()
    html = (APP_JS.parent / "index.html").read_text()
    assert "const CSRF_HEADERS = { 'X-Requested-With': 'crossline' };" in source
    auth_fetch = source.split("async function authFetch(")[1].split("\n  }\n")[0]
    assert "credentials: 'same-origin'" in auth_fetch
    assert "requestLogin(" in auth_fetch
    assert "fetch('/api/auth/login'" in source
    assert "fetch('/api/auth/logout'" in source
    assert "getJson('/api/auth/session')" in source
    login = html.split('id="login-modal"')[1].split("</dialog>")[0]
    assert 'type="password"' in login
    assert 'autocomplete="current-password"' in login
    assert 'id="btn-auth"' in html
    # The journal is private, so it goes through the authenticated path
    assert "authFetch(`/api/journal?" in source
    assert "getJson(`/api/journal" not in source


def test_health_badge_reads_the_minimal_health_payload():
    source = APP_JS.read_text()
    health = source.split("async function checkHealth()")[1].split("async function loadConfig()")[0]
    assert "data.cron_fresh === false" in health
    assert "data.database" not in health
    assert "getJson('/api/cron/status')" in health


INDEX_HTML = APP_JS.parent / "index.html"
MINUS = "\u2212"


def test_header_labels_the_app_as_research_with_methodology():
    html = INDEX_HTML.read_text()
    assert "Research · no validated edge" in html
    body = html.split('class="research-body"')[1].split("</details>")[0]
    for fact in ("Oct 2024 to Sep 2026", "12 symbols", "n≈7,576", "+0.025R", f"{MINUS}0.014R", f"{MINUS}0.043R to {MINUS}0.057R"):
        assert fact in body


def test_stat_tiles_show_honest_kpis_without_summed_percent():
    html = INDEX_HTML.read_text()
    source = APP_JS.read_text()
    tiles = html.split('aria-label="Track record"')[1].split("</section>")[0]
    labels = [part.split("</div>")[0] for part in tiles.split('<div class="stat-label">')[1:]]
    assert labels == ["Mean net R", "Win rate", "Net R", "Max drawdown"]
    assert "% after fees" not in source.split("// Journal")[0]
    assert "total_net_pnl" not in source
    assert "stat-open" not in html
    assert "getJson('/api/performance')" in source
    assert "getJson('/api/summary')" not in source
    for needle in ("mean_net_r_ci95", "break_even_win_rate", "sample_size_warning", "max_drawdown_r"):
        assert needle in source


def test_signal_rows_mark_excluded_sources_and_show_net_r():
    html = INDEX_HTML.read_text()
    source = APP_JS.read_text()
    for chip in ("'LEGACY'", "'LATE'", "'MANUAL'"):
        assert chip in source
    assert source.count("excluded from stats") >= 2
    assert '<th class="r">Net R</th>' in html
    # Signed results use the minus sign U+2212, with 2 decimals by default
    assert "function formatR(val, digits = 2)" in source
    assert f"'{MINUS}'" in source


def test_signal_dialog_shows_cost_split_and_provenance():
    html = INDEX_HTML.read_text()
    for field in ("detail-gross", "detail-cost", "detail-net", "detail-version", "detail-source", "detail-basis",
                  "detail-detected"):
        assert f'id="{field}"' in html
        assert f"'{field}'" in APP_JS.read_text()


def test_manual_scan_is_a_preview_unless_saving_is_chosen():
    html = INDEX_HTML.read_text()
    source = APP_JS.read_text()
    select = html.split('id="select-save"')[1].split("</select>")[0]
    assert '<option value="false" selected>Preview only</option>' in select
    assert "save_db: String(saveDb)" in source
    assert "send_telegram: 'false'" in source
    assert "authFetch(`/api/scan?" in source


def _function(source: str, name: str) -> str:
    return source.split(f"function {name}(")[1].split("\n  }\n")[0]


def test_net_r_total_waits_for_the_minimum_sample():
    source = APP_JS.read_text()
    assert "min_sample_for_totals: 100" in source
    assert "return n >= config.min_sample_for_totals;" in _function(source, "totalsShown")
    summary = source.split("async function loadSummary()")[1].split("// Signals (latest scan")[0]
    assert "const showTotal = n > 0 && totalsShown(n);" in summary
    assert "formatR(p.total_net_r)" in summary.split("showTotal ?")[1]
    assert "Total shown from n=${config.min_sample_for_totals}" in summary
    # The Signals caption drops the summed net R below the same threshold
    assert "totalsShown(sum.eligible_closed_count)" in _function(source, "signalsCaption")


def test_interpolated_entries_show_no_net_result():
    source = APP_JS.read_text()
    assert "return s.entry_basis !== 'cross_interp';" in _function(source, "hasAchievableEntry")
    assert "if (!hasAchievableEntry(s))" in _function(source, "resultHtml")
    provenance = _function(source, "fillProvenance")
    assert "rCell('detail-net', achievable ? sig.net_r : null);" in provenance
    assert "rCell('detail-gross', achievable ? sig.gross_r : null);" in provenance


def test_counted_rows_follow_the_running_strategy_version():
    source = APP_JS.read_text()
    assert "config.strategy_version = info.strategy_version" in source
    excluded = _function(source, "isExcluded")
    assert "s.is_eligible === false || isOtherVersion(s)" in excluded
    assert "s.strategy_version !== config.strategy_version" in _function(source, "isOtherVersion")
    note = source.split("const EXCLUSION_LABELS")[1].split("function totalsShown")[0]
    for key in ("'legacy'", "'catchup'", "'manual'", "'other_version'", "'other'"):
        assert key in note


def test_date_filter_uses_wib_days():
    source = APP_JS.read_text()
    assert "const TZ_OFFSET_MINUTES = 420;" in source
    assert "params.append('tz_offset_minutes', String(TZ_OFFSET_MINUTES));" in _function(source, "filterParams")
    assert "formatWIB(s.cross_time).startsWith(dateVal)" in _function(source, "filterSignals")


def test_journal_tab_does_not_prompt_visitors_and_landing_tab_is_signals():
    source = APP_JS.read_text()
    journal = source.split("async function loadJournal()")[1].split("authFetch(`/api/journal?")[0]
    assert "if (!signedIn) {" in journal
    assert "Your journal is private." in journal
    assert "selectTab('history');" in source.split("// Initialization")[1]
    caption = _function(source, "journalCaption")
    assert "sum.signal_closed_taken" in caption and "none closed yet" in caption


def test_indicator_values_use_the_typographic_minus():
    source = APP_JS.read_text()
    assert f".replace('-', '{MINUS}')" in _function(source, "fixedText")
    assert "const fmt4 = (v) => (v != null ? fixedText(v, 4) : " in source
