"""cron_runs migration and queries against a real PostgreSQL.

Opt-in: set TEST_DATABASE_URL to an empty throwaway database, e.g.
postgresql+asyncpg://postgres:postgres@localhost:55432/ledger_test. Every table is dropped first.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.services import db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]

ALL_TABLES = (
    "macd_signals, profitloss_history, watchlist_symbols, trade_journal, cron_runs, "
    "signal_events, ledger_heads, signal_candles, profitloss_adjustments"
)


async def _execute(url: str, *statements: str) -> list:
    engine = create_async_engine(url)
    rows: list = []
    async with engine.begin() as conn:
        for sql in statements:
            res = await conn.execute(text(sql))
            rows = res.all() if res.returns_rows else []
    await engine.dispose()
    return rows


async def _boot(monkeypatch, url: str) -> None:
    monkeypatch.setattr(db, "DATABASE_URL", url)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_maker", None)
    assert await db.init_db(max_retries=1)


async def test_cron_runs_is_created_on_an_existing_database_and_survives_reboots(monkeypatch):
    url = os.environ["TEST_DATABASE_URL"]
    await _execute(url, f"DROP TABLE IF EXISTS {ALL_TABLES}")

    await _boot(monkeypatch, url)
    try:
        assert await db.get_last_cron_success() is None
        base = datetime(2026, 10, 15, 6, 0, 15, tzinfo=UTC)
        slot = int(datetime(2026, 10, 15, 6, tzinfo=UTC).timestamp() * 1000)
        assert await db.record_cron_run(slot, base, base + timedelta(seconds=40), "ok", 2, 5)
        assert await db.record_cron_run(slot, base, base + timedelta(seconds=1), "skipped")
        later = base + timedelta(hours=2)
        assert await db.record_cron_run(slot + 7_200_000, later, later + timedelta(seconds=9), "error", error="x" * 5000)

        # Only successes count, and errors and skips after them do not move the clock
        assert await db.get_last_cron_success() == base + timedelta(seconds=40)
        with pytest.raises(ValueError):
            await db.record_cron_run(slot, base, base, "running")
    finally:
        await db.close_db()

    rows_sql = "SELECT slot_ms, status, signals_count, evaluated_count, length(error) FROM cron_runs ORDER BY id"
    first = await _execute(url, rows_sql)
    assert [(r[1], r[2], r[3], r[4]) for r in first] == [
        ("ok", 2, 5, None),
        ("skipped", 0, 0, None),
        ("error", 0, 0, db.CRON_RUN_ERROR_MAX_LENGTH),
    ]

    # Rebooting keeps every row and re-running the migration statements is a no-op
    await _boot(monkeypatch, url)
    await db.close_db()
    await _execute(url, *db.CRON_RUNS_MIGRATIONS)
    assert await _execute(url, rows_sql) == first
    index = await _execute(url, "SELECT indexname FROM pg_indexes WHERE tablename = 'cron_runs' ORDER BY 1")
    assert ("ix_cron_runs_status_finished",) in index


async def test_migration_statements_create_the_table_on_their_own(monkeypatch):
    url = os.environ["TEST_DATABASE_URL"]
    await _execute(url, "DROP TABLE IF EXISTS cron_runs")
    # As on a database whose app predates the table, before create_all ever saw the model
    await _execute(url, *db.CRON_RUNS_MIGRATIONS)
    cols = await _execute(
        url,
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'cron_runs' ORDER BY ordinal_position",
    )
    assert [c[0] for c in cols] == [
        "id", "slot_ms", "started_at", "finished_at", "status", "signals_count", "evaluated_count", "error",
    ]
    await _boot(monkeypatch, url)
    try:
        now = datetime.now(UTC)
        assert await db.record_cron_run(0, now, now, "ok")
        assert await db.get_last_cron_success() == now
    finally:
        await db.close_db()
