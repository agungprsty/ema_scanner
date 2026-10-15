"""Public pages and the API they read, with the database layer faked: titles and Open Graph tags of the
signal page for a win and a loss, 404s, escaping, CSP-safe markup, the ops page behind sign-in, research
documents, and the pure helpers behind the track record, status and signal pages."""

import re
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from src import main
from src.services import db, performance
from src.services.performance import ClosedTrade
from src.web import pages, views

pytestmark = pytest.mark.unit

H2 = 2 * 60 * 60 * 1000
CROSS_MS = 1_760_400_000_000  # 2025-10-14 00:00 UTC
MINUS = "\u2212"
API_KEY = "pages_test_key_0123456789"


def _signal(**overrides) -> dict:
    sig = {
        "id": 42,
        "symbol": "BTCUSDT",
        "side": "SHORT",
        "timeframe": "2h",
        "reason": "macd_cross",
        "cross_candle_ms": CROSS_MS,
        "cross_time": "2025-10-14T00:00:00Z",
        "cross_price": 83900.0,
        "close_price": 83850.0,
        "entry_price": 83829.2,
        "stop_loss": 85086.6,
        "tp1": 81943.1,
        "risk_reward_ratio": 1.5,
        "win_status": "SL_HIT",
        "macd": -12.5,
        "signal": -10.1,
        "histogram": -2.4,
        "bars_ago": 0,
        "is_confirmed": True,
        "strategy_key": "macd_cross",
        "strategy_version": "2.2.0",
        "source": "cron",
        "entry_basis": "next_open",
        "detected_at": "2025-10-14T02:00:21.512+00:00",
        "is_eligible": True,
        "input_candles_sha256": "c" * 64,
        "exit_price": 85086.6,
        "exit_time": "2025-10-14T08:00:00Z",
        "r_multiple": -1.07,
        "pnl_percent": -1.6,
        "gross_r": -0.98,
        "cost_r": 0.09,
        "net_r": -1.07,
        "journal": {"status": "TAKEN", "notes": "private"},
    }
    sig.update(overrides)
    return sig


WIN = {"win_status": "TP1_HIT", "side": "LONG", "symbol": "ETHUSDT", "id": 43, "exit_price": 2600.0,
       "gross_r": 1.5, "cost_r": 0.04, "net_r": 1.46, "r_multiple": 1.46, "entry_price": 2500.0,
       "stop_loss": 2433.33, "tp1": 2600.0}


@pytest.fixture
def fake_db(monkeypatch):
    state = {"signals": {42: _signal(), 43: _signal(**WIN)}, "events": {}, "windows": {}}

    async def fake_signal(signal_id):
        sig = state["signals"].get(signal_id)
        return dict(sig) if sig else None

    async def fake_events(signal_id):
        return state["events"].get(signal_id, [])

    async def fake_window(signal_id):
        return state["windows"].get(signal_id)

    monkeypatch.setattr(pages, "get_signal_by_id", fake_signal)
    monkeypatch.setattr(pages, "get_signal_events", fake_events)
    monkeypatch.setattr(pages, "get_signal_candle_window", fake_window)
    monkeypatch.setattr(main, "get_signal_by_id", fake_signal)
    return state


async def _get(path, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        return await client.get(path, **kwargs)


def _meta(html: str, prop: str) -> str:
    m = re.search(rf'<meta (?:property|name)="{re.escape(prop)}" content="([^"]*)">', html)
    assert m, prop
    return m.group(1)


def _title(html: str) -> str:
    return re.search(r"<title>(.*?)</title>", html, flags=re.S).group(1)


# --- /s/{id} ------------------------------------------------------------------------------------------


async def test_loss_page_title_and_og_tags(fake_db):
    resp = await _get("/s/42")
    assert resp.status_code == 200
    html = resp.text
    expected = f"BTCUSDT SHORT · {MINUS}1.07R · Crossline macd_cross@2.2.0"
    assert _title(html) == expected
    assert _meta(html, "og:title") == expected
    assert _meta(html, "twitter:title") == expected
    assert _meta(html, "og:type") == "article"
    assert _meta(html, "og:url") == "http://test/s/42"
    assert f"Stopped out {MINUS}1.07R (gross {MINUS}0.98R, cost {MINUS}0.09R)" in _meta(html, "og:description")
    assert "not investment advice" in _meta(html, "og:description")
    assert f"Stopped out {MINUS}1.07R (gross {MINUS}0.98R, cost {MINUS}0.09R)" in html
    # The owner's journal never reaches the public page
    assert "private" not in html


async def test_win_page_gets_the_same_treatment(fake_db):
    html = (await _get("/s/43")).text
    expected = "ETHUSDT LONG · +1.46R · Crossline macd_cross@2.2.0"
    assert _title(html) == expected and _meta(html, "og:title") == expected
    assert "Target hit +1.46R (gross +1.50R, cost " + MINUS + "0.04R)" in html
    # Same markup for a win and a loss: only the values and the neutral up/down class differ
    loss = (await _get("/s/42")).text
    assert 'class="banner banner-up"' in html and 'class="banner banner-down"' in loss
    strip = lambda h: re.sub(r">[^<]*<", "><", re.sub(r'"[^"]*"', '""', h))  # noqa: E731
    assert strip(html) == strip(loss)


async def test_open_signal_title_says_open(fake_db):
    fake_db["signals"][44] = _signal(id=44, win_status="PENDING", exit_price=None, exit_time=None, net_r=None,
                                     gross_r=None, cost_r=None, r_multiple=None, pnl_percent=None)
    html = (await _get("/s/44")).text
    assert _title(html) == "BTCUSDT SHORT · OPEN · Crossline macd_cross@2.2.0"
    # Expiry of macd_cross: close of the 48th candle after the cross
    assert "expires 2025-10-18 09:00 WIB at the latest" in html


async def test_interpolated_legacy_entry_has_no_result_in_its_title(fake_db):
    fake_db["signals"][45] = _signal(id=45, source="legacy", strategy_version="legacy", entry_basis="cross_interp",
                                     is_eligible=False)
    html = (await _get("/s/45")).text
    assert _title(html) == "BTCUSDT SHORT · CLOSED · Crossline macd_cross@legacy"
    assert "no achievable net result" in html
    assert "Legacy: booked under pre-v2.2 rules, excluded from stats" in html


@pytest.mark.parametrize("path", ["/s/999", "/s/abc", "/s/-1", "/s/12345678901"])
async def test_unknown_signal_is_a_404_page(fake_db, path):
    resp = await _get(path)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "There is no signal with this id." in resp.text
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"


async def test_signal_page_escapes_stored_values(fake_db):
    evil = '<script>alert(1)</script>"'
    fake_db["signals"][46] = _signal(id=46, symbol=evil, strategy_version='2.2.0"><img src=x onerror=alert(1)>')
    html = (await _get("/s/46")).text
    assert "<script>alert(1)" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&#34;" in html
    assert 'content="&lt;script&gt;alert(1)&lt;/script&gt;&#34; SHORT' in html


async def test_signal_facts_show_provenance_alert_and_excursions(fake_db):
    fake_db["events"][42] = [
        {"id": 1, "event_type": "detected", "occurred_at": "2025-10-14T02:00:21.512Z",
         "payload": {"params_hash": "a87bf51a" + "0" * 56, "params_source": "frozen", "git_sha": "abc"}},
        {"id": 2, "event_type": "alert_sent", "occurred_at": "2025-10-14T02:00:23.000Z", "payload": {"kind": "signal"}},
        {"id": 3, "event_type": "resolved", "occurred_at": "2025-10-14T10:00:05.000Z",
         "payload": {"cost_model_version": "cm1"}},
    ]
    # Entry candle 02:00 and the stop candle 08:00 (SHORT: adverse is the high, favourable the low)
    fake_db["windows"][42] = {"candles": [
        {"time": (CROSS_MS + i * H2) // 1000, "high": 84000 + i * 300, "low": 83500 - (100 if i == 2 else 0)}
        for i in range(0, 6)
    ]}
    html = (await _get("/s/42")).text
    assert "a87bf51a" + "0" * 56 in html
    assert "2025-10-14 09:00 WIB" in html  # alert sent
    assert "2025-10-14 07:00 WIB" in html  # cross candle open
    assert "cm1" in html
    # Held from the 02:00 entry candle through the 08:00 stop candle: worst high 85,200, best low 83,400
    assert re.search(r"MAE / MFE</dt>\s*<dd[^>]*>" + MINUS + r"1\.09R / \+0\.34R", html)


# --- every page ---------------------------------------------------------------------------------------

PAGES = ["/", "/signals", "/track-record", "/status", "/methodology", "/changelog", "/ops"]


@pytest.mark.parametrize("path", PAGES)
async def test_pages_render_with_nav_and_one_module_script(path):
    resp = await _get(path)
    assert resp.status_code == 200
    html = resp.text
    assert "Research · no validated edge" in html
    for href in ('href="/"', 'href="/signals"', 'href="/track-record"', 'href="/status"', 'href="/methodology"',
                 'href="/changelog"'):
        assert href in html
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.S)
    assert len(scripts) == 1
    attrs, body = scripts[0]
    assert 'type="module"' in attrs and 'src="/static/js/pages/' in attrs and body == ""
    assert not re.search(r"\son[a-z]+=", html)
    entry = re.search(r'src="/static/js/pages/([\w-]+)\.js', attrs).group(1)
    assert (pages.TEMPLATES_DIR.parent / "static" / "js" / "pages" / f"{entry}.js").is_file()
    assert _meta(html, "og:title") == _title(html)


async def test_head_request_on_overview_still_works():
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        assert (await client.head("/")).status_code == 200


async def test_old_history_url_redirects_to_signals():
    resp = await _get("/history")
    assert resp.status_code == 301 and resp.headers["location"] == "/signals"


async def test_track_record_states_the_baseline_and_links_the_holdout():
    html = (await _get("/track-record")).text
    # The headline backtest figure is under cm1, the cost model of the live KPIs beside it
    for fact in ("Nov 2024 to Sep 2026, 12 symbols", "7,450", f"{MINUS}0.085R", f"{MINUS}6.15",
                 f"fee only {MINUS}0.016R", "gross +0.023R", "17,489", f"{MINUS}0.050R", "Mean net R (cm1)"):
        assert fact in html
    # One card per version, the shadow candidate's with its selection data and its holdout
    assert 'data-ref="macd_cross@2.2.0" aria-labelledby' in html
    assert 'data-ref="macd_cross_ema50d_oppexit@1.0.0" aria-labelledby' in html
    assert "+0.004R" in html and "7,354" in html and f"{MINUS}0.024R" in html
    assert "/research/holdout-result-macd_cross_ema50d_oppexit-1.0.0.md" in html
    assert "FAIL" in html
    assert "0.014R" not in html  # the old fee-only headline is gone


async def test_overview_and_methodology_quote_the_cm1_backtest():
    for path in ("/", "/methodology"):
        html = (await _get(path)).text
        assert f"{MINUS}0.085R" in html and "7,450" in html


async def test_methodology_lists_cost_model_and_exits():
    html = (await _get("/methodology")).text
    assert "Cost model cm1" in html and "0.1%" in html
    assert ">BTCUSDT</td><td class=\"r num\">2.0</td><td class=\"r num\">3.0</td>" in html.replace("\n", "")
    assert "Stop first." in html
    assert "48th 2H candle" in html and "96th candle" in html
    assert "Wilson" in html and "Student t" in html
    assert "/research/preregistration-macd_cross_ema50d_oppexit-1.0.0.md" in html


async def test_changelog_links_research_paths_without_unescaped_markup():
    html = (await _get("/changelog")).text
    assert '<a href="/research/holdout-result-macd_cross_ema50d_oppexit-1.0.0.md">' in html
    parts = views.link_parts('See /research/preregistration-macd_cross_ema50d_oppexit-1.0.0.md <b>, /research/nope.md')
    assert [p["href"] for p in parts] == [None, "/research/preregistration-macd_cross_ema50d_oppexit-1.0.0.md", None]
    assert "".join(p["text"] for p in parts).endswith("<b>, /research/nope.md")


def test_held_time_is_an_upper_bound_for_level_hits():
    sig = {"cross_candle_ms": CROSS_MS, "timeframe": "2h", "win_status": "SL_HIT",
           "exit_time": datetime.fromtimestamp((CROSS_MS + 3 * H2) / 1000, UTC).isoformat()}
    assert views.held_text(sig) == "\u2264 6h"  # entry candle, the next, and the stop candle
    assert views.held_text({**sig, "exit_time": datetime.fromtimestamp((CROSS_MS + H2) / 1000, UTC).isoformat()}) \
        == "within the entry candle"
    assert views.held_text({**sig, "win_status": "EXPIRED"}) == "4h"


async def test_changelog_page_and_json_list_strategy_versions():
    data = (await _get("/api/changelog")).json()
    assert data["format"] == 1 and data["entries"]
    refs = {ref for e in data["entries"] for ref in e["strategy_refs"]}
    assert {"macd_cross@2.2.0", "macd_cross_ema50d_oppexit@1.0.0"} <= refs
    phases = {e["phase"] for e in data["entries"]}
    assert {"Phase 0", "Phase 1"} <= phases
    dates = [e["date"] for e in data["entries"]]
    assert dates == sorted(dates, reverse=True)
    html = (await _get("/changelog")).text
    assert all(e["title"].replace("'", "&#39;") in html for e in data["entries"])


async def test_ops_page_is_behind_the_cookie_login(monkeypatch):
    monkeypatch.setattr(main, "API_SECRET_KEY", API_KEY)
    resp = await _get("/ops")
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"
    html = resp.text
    assert 'data-signed-in="false"' in html
    assert re.search(r'id="ops-tools"\s+hidden', html)
    assert re.search(r'id="login-panel"(?!\s+hidden)', html)

    signed = (await _get("/ops", headers={"X-API-KEY": API_KEY})).text
    assert 'data-signed-in="true"' in signed
    assert re.search(r'id="ops-tools">', signed)


@pytest.mark.parametrize("name", ["holdout-result-macd_cross_ema50d_oppexit-1.0.0.md",
                                  "preregistration-macd_cross_ema50d_oppexit-1.0.0.md"])
async def test_research_documents_are_served_as_text(name):
    resp = await _get(f"/research/{name}")
    assert resp.status_code == 200 and resp.headers["content-type"] == "text/plain; charset=utf-8"


@pytest.mark.parametrize("name", ["..%2F..%2Fsrc%2Fmain.py", ".hidden.md", "nope.md", "x.py", "%2Fetc%2Fpasswd"])
async def test_research_route_refuses_anything_else(name):
    assert (await _get(f"/research/{name}")).status_code == 404


def test_research_file_rejects_traversal():
    assert views.research_file("../reviews/2026-10-14-institutional-review.md") is None
    assert views.research_file("holdout-result-macd_cross_ema50d_oppexit-1.0.0.md") is not None


# --- API the pages read -------------------------------------------------------------------------------


async def test_signals_api_passes_filters_and_adds_page_fields(monkeypatch):
    seen = {}

    async def fake_signals(**kwargs):
        seen.update(kwargs)
        return [_signal(win_status="PENDING", strategy_key="macd_cross_ema50d_oppexit", strategy_version="1.0.0",
                        tp1=0.0)], 1, {}

    monkeypatch.setattr(main, "get_macd_signals", fake_signals)
    resp = await _get("/api/signals?strategy_key=macd_cross_ema50d_oppexit&strategy_version=1.0.0&source=cron"
                      "&win_status=PENDING")
    assert resp.status_code == 200
    assert (seen["strategy_key"], seen["strategy_version"], seen["source"]) == (
        "macd_cross_ema50d_oppexit", "1.0.0", "cron")
    row = resp.json()["signals"][0]
    assert row["strategy_status"] == "shadow" and row["permalink"] == "/s/42"
    # Shadow time exit: close of the 96th candle from the entry candle
    assert row["expires_at"] == views.iso_utc(CROSS_MS + 97 * H2)
    assert (await _get("/api/signals?source=bogus")).status_code == 422
    assert (await _get("/api/signals?strategy_key=DROP%20TABLE")).status_code == 422


async def test_publish_view_charts_only_candles_closed_at_detection(fake_db, monkeypatch):
    fetches = []

    async def no_cache(*args, **kwargs):
        return None

    def fake_fetch(client, symbol, timeframe, start_ms, end_ms):
        fetches.append((start_ms, end_ms))
        return [{"time": start_ms // 1000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]

    monkeypatch.setattr(main, "get_cached_json", no_cache)
    monkeypatch.setattr(main, "set_cached_json", no_cache)
    monkeypatch.setattr(main, "get_signal_candle_window", no_cache)
    monkeypatch.setattr(main, "fetch_candles_range", fake_fetch)
    monkeypatch.setattr(main, "_get_client", lambda: object())
    monkeypatch.setattr(main, "_klines_budget", {"window": 0, "count": 0})
    monkeypatch.setattr(main, "_klines_inflight", {})
    resp = await _get("/api/signals/42/candles?view=publish")
    body = resp.json()
    assert (body["view"], body["final"]) == ("publish", True)
    # Detected 02:00:21: the last closed candle is the cross candle (00:00-02:00)
    assert fetches == [(CROSS_MS - main.SIGNAL_CANDLES_BEFORE * H2, CROSS_MS)]
    # A late detection shows the candles that had closed by then
    fake_db["signals"][42]["detected_at"] = "2025-10-14T07:30:00Z"
    main._klines_inflight.clear()
    await _get("/api/signals/42/candles?view=publish")
    assert fetches[-1][1] == CROSS_MS + 2 * H2


async def test_performance_api_takes_a_strategy_key(monkeypatch):
    monkeypatch.setattr(db, "_session_maker", None)
    body = (await _get("/api/performance?strategy_key=macd_cross_ema50d_oppexit")).json()
    assert (body["strategy_key"], body["strategy_version"]) == ("macd_cross_ema50d_oppexit", "1.0.0")
    assert body["win_rule"] == "net_r_positive" and body["target_rr"] is None
    live = (await _get("/api/performance")).json()
    assert (live["strategy_key"], live["win_rule"], live["target_rr"]) == ("macd_cross", "target_hit", 1.5)


async def test_versions_api_lists_live_and_shadow_with_holdout(monkeypatch):
    async def fake_counts():
        return [{"strategy_key": "macd_cross", "strategy_version": "2.2.0", "source": "cron", "count": 5, "open": 2},
                {"strategy_key": "macd_cross", "strategy_version": "legacy", "source": "legacy", "count": 4,
                 "open": 0}]

    monkeypatch.setattr(main, "get_signal_counts", fake_counts)
    data = (await _get("/api/versions")).json()
    by_ref = {s["ref"]: s for s in data["strategies"]}
    assert by_ref["macd_cross@2.2.0"]["status"] == "live" and by_ref["macd_cross@2.2.0"]["frozen"]
    assert by_ref["macd_cross@2.2.0"]["signals"] == {"total": 5, "open": 2, "by_source": {"cron": 5}}
    shadow = by_ref["macd_cross_ema50d_oppexit@1.0.0"]
    assert shadow["status"] == "shadow" and shadow["holdout"]["verdict"] == "FAIL"
    assert shadow["has_target"] is False and shadow["hold_limit_candles"] == 96
    assert by_ref["macd_cross@legacy"]["status"] == "retired"
    assert [s["status"] for s in data["strategies"]][:2] == ["live", "shadow"]
    # Without a database the lock stands in, as it does for the scan: both versions may emit
    assert by_ref["macd_cross@2.2.0"]["emitting"] and by_ref["macd_cross@2.2.0"]["refusal"] is None


async def test_versions_api_reports_a_stored_record_that_refuses_emission(monkeypatch):
    """`frozen` and the frozen hashes come from strategy_versions, the record that gates emission, so a
    drifted row shows on the page instead of only in /api/cron/status after a run."""
    live = main.registry.get("macd_cross@2.2.0")

    async def fake_counts():
        return []

    async def fake_stored():
        return {"macd_cross@2.2.0": {"frozen": True, "params_hash": live.params_hash, "code_hash": "ab" * 32,
                                     "frozen_at": "2026-10-15T02:04:41+00:00"}}

    monkeypatch.setattr(main, "get_signal_counts", fake_counts)
    monkeypatch.setattr(main, "get_strategy_versions", fake_stored)
    by_ref = {s["ref"]: s for s in (await _get("/api/versions")).json()["strategies"]}
    row = by_ref["macd_cross@2.2.0"]
    assert row["frozen"] and row["frozen_code_hash"] == "ab" * 32 and row["code_hash"] == live.code_hash
    assert row["emitting"] is False and row["refusal"].startswith("code_hash")
    shadow = by_ref["macd_cross_ema50d_oppexit@1.0.0"]
    assert shadow["emitting"] is False and shadow["refusal"] == "no strategy_versions row"


async def test_cron_runs_api_hides_error_text_from_visitors(monkeypatch):
    seen = []

    async def fake_runs(limit=50, include_error=False):
        seen.append(include_error)
        return {"runs": [], "total_runs": 0, "last_success": None, "schedule": {}}

    monkeypatch.setattr(main, "get_cron_runs", fake_runs)
    monkeypatch.setattr(main, "API_SECRET_KEY", API_KEY)
    assert (await _get("/api/cron/runs")).status_code == 200
    await _get("/api/cron/runs", headers={"X-API-KEY": API_KEY})
    assert seen == [False, True]


async def test_ledger_rows_and_csv_filter_by_strategy(monkeypatch):
    rows = [{"strategy_key": "macd_cross", "strategy_version": "2.2.0", "signal_id": 1},
            {"strategy_key": "macd_cross", "strategy_version": "legacy", "signal_id": 2},
            {"strategy_key": "macd_cross_ema50d_oppexit", "strategy_version": "1.0.0", "signal_id": 3}]

    async def fake_rows():
        return rows

    monkeypatch.setattr(main, "export_rows", fake_rows)
    data = (await _get("/api/ledger?strategy_key=macd_cross")).json()
    assert [r["signal_id"] for r in data["rows"]] == [1, 2]
    data = (await _get("/api/ledger?strategy_key=macd_cross&strategy_version=2.2.0")).json()
    assert [r["signal_id"] for r in data["rows"]] == [1]
    csv = await _get("/api/ledger.csv?strategy_key=macd_cross_ema50d_oppexit")
    assert csv.text.count("\n") == 2
    assert 'filename="crossline-ledger-macd_cross_ema50d_oppexit.csv"' in csv.headers["content-disposition"]


async def test_static_modules_are_revalidated_but_the_vendor_library_is_not():
    assert (await _get("/static/js/core.js")).headers["cache-control"] == "no-cache"
    assert (await _get("/static/css/base.css")).headers["cache-control"] == "no-cache"
    vendor = await _get("/static/vendor/lightweight-charts-4.2.1.standalone.production.js")
    assert "cache-control" not in vendor.headers


# --- pure helpers -------------------------------------------------------------------------------------


def _trade(i, net, exit_time, result="LOSS"):
    return ClosedTrade(i, result, net, None, None, exit_time)


def test_win_rate_of_a_strategy_without_target_counts_positive_net_r():
    trades = [_trade(1, 0.8, "2026-10-01T00:00:00Z", "OPPOSITE_CROSS"),
              _trade(2, -1.1, "2026-10-02T00:00:00Z", "STOPPED"),
              _trade(3, 0.0, "2026-10-03T00:00:00Z", "TIME_EXIT")]
    out = performance.summarize_trades(trades, None)
    assert (out["wins"], out["losses"], out["expired"], out["win_rule"]) == (1, 2, 0, "net_r_positive")
    assert out["break_even_win_rate"] is None
    target = performance.summarize_trades([_trade(1, 1.4, "2026-10-01T00:00:00Z", "PROFIT")], 1.5)
    assert (target["wins"], target["win_rule"]) == (1, "target_hit")


def test_equity_curve_merges_exits_at_the_same_instant():
    points = performance.equity_curve([
        _trade(2, -1.0, "2026-10-01T02:00:00Z"),
        _trade(1, 1.5, "2026-10-01T02:00:00Z"),
        _trade(3, -1.0, "2026-10-01 06:00:00"),
        _trade(4, 0.5, None),
    ])
    # Within an instant trades go by id, as max_drawdown orders them: +1.5 then -1.0 dips 1R below the peak
    assert [(p["equity_r"], p["drawdown_r"], p["n"]) for p in points] == [(0.5, -1.0, 2), (-0.5, -2.0, 1)]
    trades = [_trade(2, -1.0, "2026-10-01T02:00:00Z"), _trade(1, 1.5, "2026-10-01T02:00:00Z"),
              _trade(3, -1.0, "2026-10-01 06:00:00")]
    assert -min(p["drawdown_r"] for p in performance.equity_curve(trades)) == performance.max_drawdown(
        t.net_r for t in performance.chronological(trades))
    assert points[0]["time"] < points[1]["time"]


def test_histogram_bins_are_aligned_and_keep_empty_bins():
    bins = performance.histogram([-1.05, -1.0, 0.1, 1.46])
    assert bins[0] == {"lo": -1.25, "hi": -1.0, "count": 1}
    assert bins[1] == {"lo": -1.0, "hi": -0.75, "count": 1}
    assert sum(b["count"] for b in bins) == 4
    assert [b["lo"] for b in bins] == [round(-1.25 + 0.25 * i, 4) for i in range(len(bins))]
    assert performance.histogram([]) == []


def test_breakdown_flags_groups_below_twenty_trades():
    records = [("LONG", _trade(i, 0.1, "2026-10-01T00:00:00Z")) for i in range(20)]
    records += [("SHORT", _trade(100 + i, -0.2, "2026-10-01T00:00:00Z")) for i in range(3)]
    rows = performance.breakdown(records, 1.5)
    assert [(r["key"], r["n"], r["meaningful"]) for r in rows] == [("LONG", 20, True), ("SHORT", 3, False)]
    assert rows[1]["mean_net_r"] == pytest.approx(-0.2) and rows[1]["mean_net_r_ci95"] is not None


def test_track_record_payload_groups_by_side_symbol_and_month():
    perf = db._performance_payload("2.2.0", None, None, [])
    records = [
        {"signal_id": 1, "symbol": "BTCUSDT", "side": "LONG", "result": "PROFIT", "net_r": 1.4, "gross_r": 1.5,
         "cost_r": 0.1, "exit_time": "2026-09-30T22:00:00Z"},
        {"signal_id": 2, "symbol": "ETHUSDT", "side": "LONG", "result": "LOSS", "net_r": -1.1, "gross_r": -1.0,
         "cost_r": 0.1, "exit_time": "2026-10-02 04:00:00"},
    ]
    out = db.track_record_payload(perf, records)
    assert [g["key"] for g in out["breakdowns"]["month"]] == ["2026-09", "2026-10"]
    assert out["breakdowns"]["side"][0] == {**out["breakdowns"]["side"][0], "key": "LONG", "n": 2, "wins": 1}
    assert [p["equity_r"] for p in out["equity"]] == [1.4, pytest.approx(0.3)]
    assert out["min_breakdown_sample"] == 20


def test_cron_schedule_ratio_counts_on_time_slots():
    now = datetime(2026, 10, 15, 10, 30, tzinfo=UTC)
    slot = lambda h: int(datetime(2026, 10, 15, h, tzinfo=UTC).timestamp() * 1000)  # noqa: E731
    runs = [
        {"status": "ok", "slot_ms": slot(10), "started_at_ms": slot(10) + 20_000},
        {"status": "ok", "slot_ms": slot(8), "started_at_ms": slot(8) + 20 * 60_000},  # late: not on schedule
        {"status": "error", "slot_ms": slot(6), "started_at_ms": slot(6) + 15_000},
        {"status": "ok", "slot_ms": slot(4), "started_at_ms": slot(4) + 15_000},
    ]
    stats = db.cron_schedule_stats(runs, now, first_slot_ms=slot(4))
    # Slots 04, 06, 08 and 10 are due (10:00 + 15 min grace has passed)
    assert (stats["expected_slots"], stats["on_schedule_slots"]) == (4, 2)
    assert stats["on_schedule_ratio"] == 0.5
    early = db.cron_schedule_stats(runs, datetime(2026, 10, 15, 10, 5, tzinfo=UTC), first_slot_ms=slot(4))
    assert early["expected_slots"] == 3


def test_views_number_grammar():
    assert views.signed_r(-1.0734) == f"{MINUS}1.07R"
    assert views.signed_r(0.004) == "0.00R"
    assert views.signed_r(1.5) == "+1.50R"
    assert views.format_wib("2025-10-14 00:00:00") == "2025-10-14 07:00 WIB"
    assert views.duration_text(6 * 3600) == "6h" and views.duration_text(27 * 3600) == "1d 3h"


def test_excursions_use_only_the_candles_held():
    sig = _signal(side="LONG", entry_price=100.0, stop_loss=98.0, win_status="TP1_HIT",
                  exit_time=views.iso_utc(CROSS_MS + 3 * H2))
    candles = [
        {"time": CROSS_MS // 1000, "high": 200.0, "low": 1.0},  # the cross candle: before the entry
        {"time": (CROSS_MS + H2) // 1000, "high": 101.0, "low": 99.0},
        {"time": (CROSS_MS + 2 * H2) // 1000, "high": 102.0, "low": 99.5},
        {"time": (CROSS_MS + 3 * H2) // 1000, "high": 103.0, "low": 100.5},  # the target candle
        {"time": (CROSS_MS + 4 * H2) // 1000, "high": 300.0, "low": 0.5},  # after the exit
    ]
    assert views.excursions(sig, candles) == {"mae_r": -0.5, "mfe_r": 1.5, "candles": 3}
    assert views.excursions(_signal(win_status="PENDING"), candles) is None
    assert views.excursions(sig, None) is None
