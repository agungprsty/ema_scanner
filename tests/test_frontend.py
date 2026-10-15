"""Static checks of the public pages' front end: ES modules under src/static/js, the stylesheet and the
Jinja templates. The pages themselves are rendered in tests/test_pages.py."""

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
SRC = Path(__file__).resolve().parent.parent / "src"
STATIC = SRC / "static"
JS = STATIC / "js"
TEMPLATES = SRC / "templates"
MINUS = "\u2212"


def js(name: str) -> str:
    return (JS / name).read_text()


def all_js() -> dict[str, str]:
    return {str(p.relative_to(JS)): p.read_text() for p in sorted(JS.rglob("*.js"))}


def templates() -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(TEMPLATES.glob("*.html"))}


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
    ["<img src=x onerror=alert(1)>", "BTC<script>", 'BTC"USDT', "BTC USDT", "BTC/USDT", "../etc", "A", "A" * 21, ""],
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


# Module layout ------------------------------------------------------------------------------------------

def test_the_single_dashboard_script_is_split_into_modules():
    assert not (STATIC / "app.js").exists() and not (STATIC / "index.html").exists()
    for name in ("core.js", "format.js", "cards.js", "chart.js", "kpi.js", "events.js"):
        assert (JS / name).is_file(), name
    assert (STATIC / "css" / "base.css").is_file()


def test_every_page_template_has_an_entry_module():
    entries = {p.stem for p in (JS / "pages").glob("*.js")}
    assert {"overview", "signals", "signal", "track-record", "status", "ops", "static"} <= entries
    for name, html in templates().items():
        if name == "base.html":
            assert '<script type="module" src="/static/js/pages/{{ entry }}.js' in html
        else:
            assert html.startswith('{% extends "base.html" %}'), name


IMPORT = re.compile(r"import\s*\{([^}]*)\}\s*from\s*'([^']+)'")
EXPORT = re.compile(r"export\s+(?:async\s+)?(?:function|const|let|class)\s+([\w$]+)")


def test_every_imported_name_is_exported_by_its_module():
    sources = all_js()
    for name, source in sources.items():
        base = (JS / name).parent
        for names, target in IMPORT.findall(source):
            path = (base / target).resolve()
            assert path.is_file(), f"{name} imports missing {target}"
            exported = set(EXPORT.findall(path.read_text()))
            for imported in (n.strip() for n in names.split(",") if n.strip()):
                assert imported in exported, f"{name} imports {imported} that {target} does not export"


# Security ------------------------------------------------------------------------------------------------

def test_templates_have_no_inline_script_or_event_handlers():
    for name, html in templates().items():
        for tag in re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S):
            assert tag.strip() == "", f"inline script in {name}"
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), f"script without src in {name}"
        assert not re.search(r"\son[a-z]+\s*=", html), f"inline event handler in {name}"
        assert "|safe" not in html.replace(" ", ""), f"unescaped output in {name}"


def test_js_escapes_api_values_before_they_reach_markup():
    assert "export function escapeHtml(" in js("format.js")
    raw = re.compile(
        r"\$\{(?:s|r|ev|item|sig|row|h|run|b|strategy|info|v|p)\."
        r"(?:symbol|side|notes|source|strategy_version|strategy_key|hash|key|reason|error|ref|status|label|file|url)\}"
    )
    for name, source in all_js().items():
        # Lines that build markup; plain-text helpers are escaped where their text is inserted
        for line in (line for line in source.splitlines() if "<" in line):
            assert not raw.search(line), f"{name} interpolates an API value into markup unescaped: {line.strip()}"
        assert "/api/watchlist/${sym}" not in source
        assert "symbol=${sig.symbol}" not in source


def test_api_key_never_lives_in_page_storage():
    for name, source in all_js().items():
        assert "prompt(" not in source, name
        assert "localStorage.setItem" not in source and "localStorage.getItem" not in source, name
        assert "X-API-KEY" not in source, name
    core = js("core.js")
    # Keys stored by older versions are wiped on load
    assert "const STALE_KEY_STORAGE = ['crossline_api_key', 'tokyo_api_key'];" in core
    assert "localStorage.removeItem(key)" in core


def test_owner_requests_use_the_session_cookie_with_the_csrf_header():
    core = js("core.js")
    ops = js("pages/ops.js")
    html = templates()["ops.html"]
    assert "export const CSRF_HEADERS = { 'X-Requested-With': 'crossline' };" in core
    auth_fetch = core.split("export async function authFetch(")[1].split("\n}\n")[0]
    assert "credentials: 'same-origin'" in auth_fetch and "CSRF_HEADERS" in auth_fetch
    assert "fetch('/api/auth/login'" in core and "fetch('/api/auth/logout'" in core
    assert "getJson('/api/auth/session')" in core
    login = html.split('id="login-form"')[1].split("</form>")[0]
    assert 'type="password"' in login and 'autocomplete="current-password"' in login
    # The journal and every write go through the authenticated path
    assert "authFetch(`/api/journal?" in ops
    assert "getJson(`/api/journal" not in ops
    for other, source in all_js().items():
        if other != "pages/ops.js":
            assert "authFetch(" not in source or other == "core.js", other


def test_chart_library_is_self_hosted_pinned_by_hash_and_loaded_on_demand():
    chart = js("chart.js")
    for name, html in templates().items():
        assert "lightweight-charts" not in html and "unpkg" not in html, name
    assert "unpkg" not in chart
    assert "export function loadChartLibrary(" in chart
    assert "const CHART_LIB_URL = '/static/vendor/lightweight-charts-4.2.1.standalone.production.js';" in chart
    lib = (STATIC / "vendor" / "lightweight-charts-4.2.1.standalone.production.js").read_bytes()
    assert b"TradingView Lightweight Charts\xe2\x84\xa2 v4.2.1" in lib[:200]
    # The bytes published in the npm tarball of lightweight-charts@4.2.1
    assert hashlib.sha256(lib).hexdigest() == "197180bdf2185bb33f3cad878d0d29d2128b4fca96ed847a0de4dc64da1c5e97"
    integrity = re.search(r"const CHART_LIB_INTEGRITY = '(sha384-[^']+)';", chart).group(1)
    assert integrity == "sha384-" + base64.b64encode(hashlib.sha384(lib).digest()).decode()
    assert "script.integrity = CHART_LIB_INTEGRITY;" in chart


def test_nginx_csp_allows_only_same_origin_scripts():
    conf = (SRC.parent / "nginx.conf.example").read_text()
    csp = re.search(r'add_header Content-Security-Policy "([^"]+)"', conf).group(1)
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";") if d.strip())
    assert directives["script-src"] == "'self'"
    assert directives["object-src"] == "'none'" and directives["frame-ancestors"] == "'none'"


# Behaviour kept from the Phase 0 dashboard ----------------------------------------------------------------

def test_ops_does_not_scan_on_page_load_and_scans_preview_by_default():
    ops = js("pages/ops.js")
    calls = [line.strip() for line in ops.splitlines() if "runScan" in line]
    assert calls == ["async function runScan() {", "$('btn-scan').addEventListener('click', runScan);"]
    select = templates()["ops.html"].split('id="select-save"')[1].split("</select>")[0]
    assert '<option value="false" selected>Preview only</option>' in select
    assert "save_db: String(saveDb)" in ops
    assert "send_telegram: 'false'" in ops
    assert "authFetch(`/api/scan?" in ops


def test_lists_page_twenty_rows_at_a_time():
    assert "const PAGE_SIZE = 20;" in js("pages/signals.js")
    assert "const PAGE_SIZE = 20;" in js("pages/ops.js")


def test_health_badge_reads_the_minimal_health_payload():
    health = js("core.js").split("export async function checkHealth()")[1].split("const STALE_KEY_STORAGE")[0]
    assert "data.cron_fresh === false" in health
    assert "data.database" not in health
    assert "getJson('/api/cron/status')" in health


def test_kpi_tiles_show_honest_figures_without_summed_percent():
    kpi = js("kpi.js")
    labels = re.findall(r"tile\('([^']+)'", kpi)
    assert labels == ["Mean net R", "Win rate", "Net R", "Max drawdown"]
    for name, source in all_js().items():
        if name != "pages/ops.js":
            assert "% after fees" not in source, name
        assert "total_net_pnl" not in source and "/api/summary" not in source, name
    for needle in ("mean_net_r_ci95", "break_even_win_rate", "sample_size_warning", "max_drawdown_r", "win_rule"):
        assert needle in kpi
    # Rates always carry their sample: "44.4% (4/9)"
    assert "rateText(Number(p.wins) || 0, n)" in kpi
    assert "export function rateText(wins, n, digits = 1)" in js("format.js")


def test_net_r_total_waits_for_the_minimum_sample():
    kpi = js("kpi.js")
    assert "return (p.n_closed || 0) >= (p.min_sample_for_totals || 100);" in kpi
    assert "const showTotal = n > 0 && totalsShown(p);" in kpi
    assert "Total shown from n=" in kpi


def test_signal_cards_mark_excluded_and_shadow_signals():
    cards = js("cards.js")
    for chip in ("'LEGACY'", "'LATE'", "'MANUAL'", ">SHADOW<"):
        assert chip in cards
    assert cards.count("excluded from stats") >= 2
    assert "return s.entry_basis !== 'cross_interp';" in cards
    assert "if (!hasAchievableEntry(s)) return" in cards
    # Statuses of both target and exit-rule strategies
    for status in ("TP1_HIT", "SL_HIT", "EXPIRED", "STOPPED", "OPPOSITE_CROSS", "TIME_EXIT"):
        assert status in cards


def test_signed_figures_use_the_typographic_minus():
    fmt = js("format.js")
    assert f"export const MINUS = '{MINUS}';" in fmt
    assert "export function formatR(val, digits = 2)" in fmt
    assert f".replace('-', '{MINUS}')" in fmt.split("export function fixedText(")[1].split("}")[0]
    assert f"'{MINUS}'" in fmt.split("export function signedNum(")[1].split("\n}\n")[0]


def test_dates_are_wib():
    fmt = js("format.js")
    assert "export const TZ = 'Asia/Jakarta';" in fmt
    assert "export const TZ_OFFSET_MINUTES = 420;" in fmt


def test_signal_page_toggles_between_publish_and_after_close():
    page = js("pages/signal.js")
    html = templates()["signal.html"]
    assert 'value="publish"' in html and 'value="after" checked' in html
    assert "?view=publish" in page
    assert "asOfMs" in page
    # At publish draws only candles closed by the detection time
    assert "c.time * 1000 + CANDLE_MS <= asOfMs" in js("chart.js")


def test_breakdowns_grey_small_groups():
    page = js("pages/track-record.js")
    assert "r.meaningful ? '' : 'not-meaningful'" in page
    assert "not meaningful" in page
    assert "tr.not-meaningful" in (STATIC / "css" / "base.css").read_text()


def test_stylesheet_keeps_the_visual_language_and_mobile_guards():
    css = (STATIC / "css" / "base.css").read_text()
    for token in ("--bg: #0f1012", "--long: #3fb68b", "--short: #e5605a", "IBM Plex Sans", "IBM Plex Mono"):
        assert token in css
    assert "overflow-wrap: anywhere" in css
    assert ".table-scroll { overflow-x: auto; max-width: 100%; }" in css
