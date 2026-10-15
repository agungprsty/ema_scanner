"""The queries behind the public pages against a real PostgreSQL, on the production snapshot: track record,
signal filters, signal counts per version, recent ledger events, cron runs, and the signal page itself.

Opt-in: set TEST_DATABASE_URL to an empty throwaway database. Every table is dropped first.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
import test_ledger_migration_pg as snapshot
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src import main
from src.services import db, ledger

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]

ALL_TABLES = (
    "macd_signals, profitloss_history, watchlist_symbols, trade_journal, cron_runs, strategy_versions, strategies, "
    "research_trials, signal_events, ledger_heads, signal_candles, profitloss_adjustments"
)
H2 = 2 * 60 * 60 * 1000
MINUS = "\u2212"


async def _execute(url: str, *statements: str) -> list:
    engine = create_async_engine(url)
    rows: list = []
    try:
        async with engine.begin() as conn:
            for sql in statements:
                res = await conn.execute(text(sql))
                rows = res.all() if res.returns_rows else []
    finally:
        await engine.dispose()
    return rows


@pytest.fixture
async def snapshot_db(monkeypatch):
    url = os.environ["TEST_DATABASE_URL"]
    await _execute(url, f"DROP TABLE IF EXISTS {ALL_TABLES}")
    await snapshot._seed(url)
    monkeypatch.setattr(db, "DATABASE_URL", url)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_maker", None)
    assert await db.init_db(max_retries=1)
    ledger.reset_caches()
    yield url
    await db.close_db()


async def test_track_record_matches_performance(snapshot_db):
    perf = await db.get_performance()
    record = await db.get_track_record()
    assert record["performance"] == perf
    # v2.2 only: ids 5-10, every one in the curve, the histogram and each breakdown
    assert sum(p["n"] for p in record["equity"]) == perf["n_closed"] == 6
    assert record["equity"][-1]["equity_r"] == pytest.approx(perf["total_net_r"], abs=1e-3)
    assert min(p["drawdown_r"] for p in record["equity"]) <= 0
    assert sum(b["count"] for b in record["histogram"]) == 6
    for name in ("side", "symbol", "month"):
        groups = record["breakdowns"][name]
        assert sum(g["n"] for g in groups) == 6
        assert all(not g["meaningful"] for g in groups)
    assert [g["key"] for g in record["breakdowns"]["month"]] == ["2026-10"]

    shadow = await db.get_track_record("macd_cross_ema50d_oppexit", "1.0.0")
    assert shadow["performance"]["n_closed"] == 0 and shadow["equity"] == []
    # Every macd_cross row is another strategy to it, its legacy rows included (they are not the shadow's)
    assert shadow["performance"]["excluded"]["other_version"] == 15
    assert shadow["performance"]["excluded"]["legacy"] == 0


async def test_signal_filters_and_counts(snapshot_db):
    _, legacy, _ = await db.get_macd_signals(source="legacy", limit=100)
    assert legacy == 4
    rows, total, _ = await db.get_macd_signals(strategy_key="macd_cross", strategy_version="2.2.0", limit=100)
    assert total == 11 and {r["strategy_version"] for r in rows} == {"2.2.0"}
    _, none, _ = await db.get_macd_signals(strategy_key="macd_cross_ema50d_oppexit")
    assert none == 0
    _, open_total, _ = await db.get_macd_signals(win_status="PENDING", source="cron")
    assert open_total == 5

    counts = await db.get_signal_counts()
    by = {(c["strategy_version"], c["source"]): (c["count"], c["open"]) for c in counts}
    assert by[("legacy", "legacy")][0] == 4
    assert sum(n for n, _ in by.values()) == 15


async def test_recent_events_and_filtered_ledger_rows(snapshot_db):
    events = await ledger.get_recent_events(5)
    assert len(events) == 5
    assert [e["id"] for e in events] == sorted((e["id"] for e in events), reverse=True)
    assert all(e["symbol"] and e["hash"] and len(e["hash"]) == 64 for e in events)
    rows = await ledger.export_rows("macd_cross", "legacy")
    assert len(rows) == 4 and {r["exclusion_reason"] for r in rows} == {"legacy"}


async def test_cron_runs_and_schedule(snapshot_db):
    now = datetime.now(UTC)
    slot = now - timedelta(milliseconds=int(now.timestamp() * 1000) % H2)
    for i, status in enumerate(("ok", "error", "ok")):
        started = slot - timedelta(hours=2 * i) + timedelta(seconds=20)
        await db.record_cron_run(
            int((slot - timedelta(hours=2 * i)).timestamp() * 1000), started, started + timedelta(seconds=9), status,
            signals_count=i, error="boom" if status == "error" else None,
        )
    public = await db.get_cron_runs(limit=2)
    assert [r["status"] for r in public["runs"]] == ["ok", "error"]
    assert "error" not in public["runs"][1] and public["runs"][1]["has_error"]
    assert public["total_runs"] == 3 and public["last_success"]
    owner = await db.get_cron_runs(include_error=True)
    assert owner["runs"][1]["error"] == "boom"
    stats = owner["schedule"]
    assert stats["on_schedule_slots"] <= 2 and stats["expected_slots"] >= stats["on_schedule_slots"]


async def test_signal_page_and_api_on_the_snapshot(snapshot_db):
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        rows = (await client.get("/api/signals?strategy_key=macd_cross&strategy_version=2.2.0&limit=100")).json()
        closed = next(r for r in rows["signals"] if r["win_status"] == "SL_HIT")
        page = await client.get(f"/s/{closed['id']}")
        assert page.status_code == 200
        net = f"{MINUS}{abs(closed['net_r']):.2f}R"
        assert f"<title>{closed['symbol']} {closed['side']} · {net} · Crossline macd_cross@2.2.0</title>" in page.text
        open_row = next(r for r in rows["signals"] if r["win_status"] == "PENDING")
        assert open_row["expires_at"] and open_row["strategy_status"] == "live"
        assert "· OPEN ·" in (await client.get(f"/s/{open_row['id']}")).text
        record = (await client.get("/api/track-record")).json()
        assert record["performance"]["n_closed"] == 6
        assert (await client.get("/api/ledger/events?limit=3")).json()["events"]
