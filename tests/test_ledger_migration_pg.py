"""Boot-time ledger migration against a real PostgreSQL, seeded with the production snapshot.

Opt-in: set TEST_DATABASE_URL to an empty throwaway database, e.g.
postgresql+asyncpg://postgres:postgres@localhost:55432/ledger_test. Every table is dropped first.
"""

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.services import db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]

# The v2.2.0 schema, as production has it before this migration
OLD_SCHEMA = (
    """
    CREATE TABLE macd_signals (
        id SERIAL PRIMARY KEY,
        symbol VARCHAR(20) NOT NULL,
        side VARCHAR(10) NOT NULL,
        timeframe VARCHAR(10) NOT NULL,
        reason VARCHAR(50) NOT NULL,
        cross_candle_ms BIGINT NOT NULL,
        cross_time VARCHAR(40) NOT NULL,
        cross_price DOUBLE PRECISION NOT NULL,
        close_price DOUBLE PRECISION NOT NULL,
        entry_price DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        stop_loss DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        tp1 DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        risk_reward_ratio DOUBLE PRECISION NOT NULL DEFAULT 1.5,
        win_status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
        macd DOUBLE PRECISION NOT NULL,
        signal DOUBLE PRECISION NOT NULL,
        histogram DOUBLE PRECISION NOT NULL,
        bars_ago INTEGER NOT NULL DEFAULT 0,
        is_confirmed BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE profitloss_history (
        id SERIAL PRIMARY KEY,
        signal_id INTEGER,
        symbol VARCHAR(20) NOT NULL,
        timeframe VARCHAR(10) NOT NULL DEFAULT '2h',
        direction VARCHAR(10) NOT NULL,
        entry_price DOUBLE PRECISION NOT NULL,
        stop_loss DOUBLE PRECISION NOT NULL,
        tp1 DOUBLE PRECISION NOT NULL,
        exit_price DOUBLE PRECISION NOT NULL,
        result VARCHAR(20) NOT NULL,
        pnl_percent DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        r_multiple DOUBLE PRECISION,
        fee_percent DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        risk_reward_ratio DOUBLE PRECISION NOT NULL DEFAULT 1.5,
        cross_time VARCHAR(40) NOT NULL,
        exit_time VARCHAR(40) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
    );
    """,
)

# (id, symbol, side, cross_price, entry, stop, tp1, bars_ago, win_status, created_at)
SIGNALS = [
    (1, "ETHUSDT", "SHORT", 2526.240353, 2526.240353, 2584.12794, 2439.408973, 2, "TP1_HIT", "2026-10-12T22:16:41Z"),
    (2, "SOLUSDT", "SHORT", 110.065444, 110.065444, 113.60676, 104.75347, 3, "TP1_HIT", "2026-10-12T22:16:41Z"),
    (3, "ZECUSDT", "SHORT", 1261.156715, 1261.156715, 1339.21308, 1144.072168, 2, "TP1_HIT", "2026-10-12T22:16:41Z"),
    (4, "BTCUSDT", "LONG", 83691.975102, 83691.975102, 80823.03, 87995.392755, 0, "PENDING", "2026-10-13T08:00:15Z"),
    (5, "XRPUSDT", "LONG", 1.369027, 1.3699, 1.345803, 1.406045, 0, "SL_HIT", "2026-10-13T16:00:16Z"),
    (6, "SOLUSDT", "LONG", 107.471533, 107.51, 105.36884, 110.72174, 0, "SL_HIT", "2026-10-13T20:00:16Z"),
    (7, "ZECUSDT", "LONG", 1218.999049, 1217.85, 1171.81168, 1286.90748, 0, "SL_HIT", "2026-10-14T00:00:16Z"),
    (8, "ETHUSDT", "LONG", 2509.265884, 2512.59, 2489.25152, 2547.59772, 0, "SL_HIT", "2026-10-14T04:00:17Z"),
    (9, "ETHUSDT", "SHORT", 2499.172812, 2495.86, 2534.62914, 2437.70629, 0, "TP1_HIT", "2026-10-14T10:00:16Z"),
    (10, "NEARUSDT", "LONG", 5.030624, 5.028, 4.953074, 5.140389, 0, "SL_HIT", "2026-10-14T10:00:16Z"),
    (11, "BTCUSDT", "SHORT", 83276.063867, 82282.3, 84285.8352, 79276.9972, 0, "PENDING", "2026-10-14T14:00:21Z"),
    (12, "SOLUSDT", "SHORT", 105.35391, 104.82, 108.8673, 98.74905, 0, "PENDING", "2026-10-14T14:00:21Z"),
    (13, "NEARUSDT", "SHORT", 4.914539, 4.841, 5.178336, 4.334996, 0, "PENDING", "2026-10-14T16:00:19Z"),
    (14, "XRPUSDT", "SHORT", 1.344051, 1.3266, 1.389073, 1.232891, 0, "PENDING", "2026-10-14T16:00:19Z"),
    (15, "ZECUSDT", "SHORT", 1187.385305, 1143.65, 1272.21936, 950.79596, 0, "PENDING", "2026-10-14T16:00:19Z"),
]

# Cross candle open (ms) per signal and exit time per closed trade, as production stored them: the
# pre-v2.2 evaluator wrote exits without the "T" and zone, later rows are ISO
CROSS_MS = {
    1: 1791820800000, 2: 1791813600000, 3: 1791820800000, 4: 1791871200000, 5: 1791900000000,
    6: 1791914400000, 7: 1791928800000, 8: 1791943200000, 9: 1791964800000, 10: 1791964800000,
    11: 1791979200000, 12: 1791979200000, 13: 1791986400000, 14: 1791986400000, 15: 1791986400000,
}
EXIT_TIMES = {
    1: "2026-10-13 00:00:00", 2: "2026-10-13 00:00:00", 3: "2026-10-13 00:00:00",
    5: "2026-10-14T14:00:00Z", 6: "2026-10-14T12:00:00Z", 7: "2026-10-14T14:00:00Z",
    8: "2026-10-14T10:00:00Z", 9: "2026-10-14T14:00:00Z", 10: "2026-10-14T12:00:00Z",
}


# cm1 one-way slippage per symbol, in bps
CM1_SLIPPAGE = {"BTCUSDT": 2.0, "ETHUSDT": 2.0, "SOLUSDT": 8.0, "XRPUSDT": 10.0, "ZECUSDT": 20.0, "NEARUSDT": 25.0}

# The restated v2.2.0 headline under cm1 (ids 5-10, net R): pinned so nobody tunes the cost model
# toward an expected number without this test saying so
V22_TOTAL_NET_R_CM1 = -4.724


def _cross_time(sid: int) -> str:
    return datetime.fromtimestamp(CROSS_MS[sid] / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# (signal_id, exit_price, result, pnl_percent, r_multiple, fee_percent)
TRADES = [
    (1, 2439.408973, "PROFIT", 3.44, 1.5, 0.0),
    (2, 104.75347, "PROFIT", 4.83, 1.5, 0.0),
    (3, 1144.072168, "PROFIT", 9.28, 1.5, 0.0),
    (5, 1.345803, "LOSS", -1.86, -1.06, 0.1),
    (6, 105.36884, "LOSS", -2.09, -1.05, 0.1),
    (7, 1171.81168, "LOSS", -3.88, -1.03, 0.1),
    (8, 2489.25152, "LOSS", -1.03, -1.11, 0.1),
    (9, 2437.70629, "PROFIT", 2.23, 1.44, 0.1),
    (10, 4.953074, "LOSS", -1.59, -1.07, 0.1),
]


async def _seed(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text(
            "DROP TABLE IF EXISTS macd_signals, profitloss_history, watchlist_symbols, trade_journal, "
            "signal_events, ledger_heads, signal_candles, profitloss_adjustments"
        ))
        for ddl in OLD_SCHEMA:
            await conn.execute(text(ddl))
        signals = {s[0]: s for s in SIGNALS}
        for sid, sym, side, cross, entry, stop, tp1, bars_ago, status, created in SIGNALS:
            await conn.execute(
                text(
                    "INSERT INTO macd_signals (id, symbol, side, timeframe, reason, cross_candle_ms, cross_time, "
                    "cross_price, close_price, entry_price, stop_loss, tp1, win_status, macd, signal, histogram, "
                    "bars_ago, created_at) VALUES (:id, :sym, :side, '2h', 'r', :ms, :cross_time, :cross, :cross, :entry, "
                    ":stop, :tp1, :status, 0, 0, 0, :bars_ago, :created)"
                ),
                {
                    "id": sid, "sym": sym, "side": side, "ms": CROSS_MS[sid], "cross_time": _cross_time(sid),
                    "cross": cross,
                    "entry": entry, "stop": stop, "tp1": tp1, "status": status, "bars_ago": bars_ago,
                    "created": datetime.fromisoformat(created),
                },
            )
        for sid, exit_price, result, pnl, r, fee in TRADES:
            _, sym, side, _, entry, stop, tp1, *_ = signals[sid]
            await conn.execute(
                text(
                    "INSERT INTO profitloss_history (signal_id, symbol, direction, entry_price, stop_loss, tp1, "
                    "exit_price, result, pnl_percent, r_multiple, fee_percent, cross_time, exit_time) VALUES "
                    "(:sid, :sym, :side, :entry, :stop, :tp1, :exit, :result, :pnl, :r, :fee, :cross_time, :exit_time)"
                ),
                {
                    "sid": sid, "sym": sym, "side": side, "entry": entry, "stop": stop, "tp1": tp1,
                    "exit": exit_price, "result": result, "pnl": pnl, "r": r, "fee": fee,
                    "cross_time": _cross_time(sid), "exit_time": EXIT_TIMES[sid],
                },
            )
        await conn.execute(text("SELECT setval('macd_signals_id_seq', 15)"))
    await engine.dispose()


async def _boot(monkeypatch, url: str) -> None:
    monkeypatch.setattr(db, "DATABASE_URL", url)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_maker", None)
    assert await db.init_db(max_retries=1)
    await db.close_db()


async def _fetch(url: str, sql: str) -> list:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        rows = (await conn.execute(text(sql))).all()
    await engine.dispose()
    return rows


async def test_production_snapshot_migrates_and_reboots_idempotently(monkeypatch):
    url = os.environ["TEST_DATABASE_URL"]
    await _seed(url)

    await _boot(monkeypatch, url)
    signals_sql = (
        "SELECT id, source, entry_basis, strategy_key, strategy_version, is_eligible, "
        "detected_at = created_at FROM macd_signals ORDER BY id"
    )
    trades_sql = (
        "SELECT signal_id, pnl_percent, r_multiple, fee_percent, gross_r, cost_r, net_r, cost_model_version, "
        "slippage_bps, entry_basis, booked_by FROM profitloss_history ORDER BY signal_id"
    )
    first_signals = await _fetch(url, signals_sql)
    first_trades = await _fetch(url, trades_sql)

    by_id = {row[0]: row[1:] for row in first_signals}
    for sid in (1, 2, 3, 4):
        assert by_id[sid] == ("legacy", "cross_interp", "macd_cross", "legacy", False, True)
    for sid in range(5, 16):
        assert by_id[sid] == ("cron", "next_open", "macd_cross", "2.2.0", True, True)

    trades = {row[0]: row[1:] for row in first_trades}
    originals = {sid: (pnl, r, fee) for sid, _, _, pnl, r, fee in TRADES}
    symbols = {sig[0]: sig[1] for sig in SIGNALS}
    for sid, (pnl, r, fee, gross_r, cost_r, net_r, version, slippage, basis, booked_by) in trades.items():
        # Historical columns untouched, restatement added alongside
        assert (pnl, r, fee) == originals[sid]
        assert version == "cm1"
        assert cost_r > 0
        assert net_r == pytest.approx(gross_r - cost_r)
        assert basis == ("cross_interp" if sid <= 3 else "next_open")
        assert slippage == CM1_SLIPPAGE[symbols[sid]]
        # Fee-free rows were the pre-v2.2 evaluator's; rows with fees are the current evaluator's
        assert booked_by == ("legacy" if sid <= 3 else "evaluator")
    for sid in (1, 2, 3):
        assert trades[sid][3] == pytest.approx(1.5)
    v22_net = sum(trades[sid][5] for sid in range(5, 11))
    v22_gross = sum(trades[sid][3] for sid in range(5, 11))
    assert v22_net < v22_gross < 0
    assert v22_net == pytest.approx(V22_TOTAL_NET_R_CM1, abs=5e-4)

    # Phase 0 acceptance: no fee-free closed row outside the legacy source
    fee_free_outside_legacy = await _fetch(
        url,
        "SELECT p.id FROM profitloss_history p JOIN macd_signals s ON s.id = p.signal_id "
        "WHERE COALESCE(p.fee_percent, 0) = 0 AND s.source <> 'legacy'",
    )
    assert fee_free_outside_legacy == []

    # A second boot changes nothing
    await _boot(monkeypatch, url)
    assert await _fetch(url, signals_sql) == first_signals
    assert await _fetch(url, trades_sql) == first_trades


async def test_performance_and_date_filters_on_the_production_snapshot(monkeypatch):
    url = os.environ["TEST_DATABASE_URL"]
    await _seed(url)
    monkeypatch.setattr(db, "DATABASE_URL", url)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_maker", None)
    assert await db.init_db(max_retries=1)
    try:
        perf = await db.get_performance()
        net = dict(await _fetch(url, "SELECT signal_id, net_r FROM profitloss_history"))

        # v2.2 only: ids 5-10, one target and five stops; ids 1-4 are legacy and stay out
        assert (perf["n_closed"], perf["wins"], perf["losses"], perf["expired"]) == (6, 1, 5, 0)
        assert perf["open_count"] == 5
        assert perf["excluded"] == {"legacy": 4, "catchup": 0, "manual": 0, "other_version": 0, "outside_ledger": 0, "other": 0}
        assert perf["win_rate"] == 16.7
        assert perf["total_net_r"] == pytest.approx(sum(net[sid] for sid in range(5, 11)), abs=1e-3)
        assert perf["total_net_r"] < -3.88
        assert perf["mean_net_r_ci95"][0] < perf["mean_net_r"] < perf["mean_net_r_ci95"][1] < 1
        assert perf["sample_size_warning"] == "insufficient sample"
        assert (perf["first_exit_time"], perf["last_exit_time"]) == ("2026-10-14T10:00:00Z", "2026-10-14T14:00:00Z")
        # Exit order: #8 at 10:00, #6 then #10 at 12:00, then the 14:00 exits by row id (#5, #7, #9)
        losses = [net[sid] for sid in (8, 6, 10, 5, 7)]
        assert perf["max_drawdown_r"] == pytest.approx(-sum(losses), abs=1e-3)

        eth = await db.get_performance(symbol="ethusdt")
        assert (eth["n_closed"], eth["wins"], eth["excluded"]["legacy"]) == (2, 1, 1)
        assert (await db.get_performance(strategy_version="9.9.9"))["n_closed"] == 0

        summary = await db.get_macd_signals_summary()
        assert (summary["closed_trades"], summary["winrate"]) == (6, 16.7)
        assert summary["total_r"] == pytest.approx(perf["total_net_r"], abs=0.01)
        assert summary["total_signals"] == 15

        # end_date keeps its whole last day: 2026-10-13 holds the crosses of #4-#7
        _, total, _ = await db.get_macd_signals(start_date="2026-10-13", end_date="2026-10-13", timeframe=None)
        assert total == 4
        _, total, _ = await db.get_macd_signals(end_date="2026-10-12", timeframe=None)
        assert total == 3
        _, total, _ = await db.get_profitloss_history(start_date="2026-10-12", end_date="2026-10-12")
        assert total == 3
        _, total, _ = await db.get_profitloss_history(start_date="2026-10-13", end_date="2026-10-14")
        assert total == 6
    finally:
        await db.close_db()


async def test_hand_entered_trades_never_reach_the_track_record(monkeypatch):
    url = os.environ["TEST_DATABASE_URL"]
    await _seed(url)
    monkeypatch.setattr(db, "DATABASE_URL", url)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_maker", None)
    assert await db.init_db(max_retries=1)
    try:
        before = await db.get_performance()
        signals_before = (await db.get_macd_signals(limit=100))[2]
        assert (before["n_closed"], before["wins"], before["open_count"]) == (6, 1, 5)

        # A hand-entered win for the pending, counted BTC #11 cannot take the evaluator's slot
        with pytest.raises(db.SignalTradeConflict):
            await db.record_profit_loss(
                "BTCUSDT", "SHORT", 82282.3, 84285.8352, 79276.9972, 79276.9972, "PROFIT", signal_id=11,
                cross_time="2026-10-14T12:00:00Z", exit_time="2026-10-14T18:00:00Z",
            )
        # Without a signal it is stored, labelled manual, and left out of every counted figure
        item = await db.record_profit_loss(
            "BTCUSDT", "SHORT", 82282.3, 84285.8352, 79276.9972, 79276.9972, "PROFIT",
            cross_time="2026-10-14T12:00:00Z", exit_time="2026-10-14T18:00:00Z",
        )
        assert item["booked_by"] == "manual" and item["net_r"] is None
        # It went to the adjustments table, not to profitloss_history
        assert item["adjustment"] is True
        assert (await db.get_profitloss_adjustments())[1] == 1
        # A manual row written straight into the table against a counted signal (as the old endpoint
        # allowed) does not count either
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO profitloss_history (signal_id, symbol, direction, entry_price, stop_loss, tp1, "
                "exit_price, result, pnl_percent, cross_time, exit_time, booked_by) VALUES (12, 'SOLUSDT', "
                "'SHORT', 104.82, 108.8673, 98.74905, 98.74905, 'WIN', 0, 't', 't', 'manual')"
            ))
        await engine.dispose()

        after = await db.get_performance()
        assert {k: after[k] for k in ("n_closed", "wins", "total_net_r", "open_count", "excluded")} == {
            k: before[k] for k in ("n_closed", "wins", "total_net_r", "open_count", "excluded")
        }
        summary = await db.get_macd_signals_summary()
        assert (summary["closed_trades"], summary["winrate"]) == (6, 16.7)
        signals_after = (await db.get_macd_signals(limit=100))[2]
        assert signals_after["eligible_closed_count"] == signals_before["eligible_closed_count"] == 6
        assert signals_after["eligible_net_r"] == signals_before["eligible_net_r"]
        pl_summary = (await db.get_profitloss_history(limit=100))[2]
        assert (pl_summary["eligible_closed_count"], pl_summary["eligible_win_count"]) == (6, 1)
        assert pl_summary["eligible_net_r"] == pytest.approx(before["total_net_r"], abs=0.01)
    finally:
        await db.close_db()

    # A reboot does not restate the hand-entered row written straight into profitloss_history, so it
    # never gains a net R; the one entered through the API is only in profitloss_adjustments
    await _boot(monkeypatch, url)
    manual = await _fetch(url, "SELECT net_r, cost_model_version FROM profitloss_history WHERE booked_by = 'manual'")
    assert manual == [(None, None)]
    assert await _fetch(url, "SELECT symbol, result FROM profitloss_adjustments") == [("BTCUSDT", "PROFIT")]
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_maker", None)
    assert await db.init_db(max_retries=1)
    try:
        assert (await db.get_performance())["total_net_r"] == before["total_net_r"]
    finally:
        await db.close_db()


async def test_backfill_marks_late_and_forming_rows(monkeypatch):
    url = os.environ["TEST_DATABASE_URL"]
    await _seed(url)
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        # #13 stored 15 minutes after its entry candle opened; #14 entered at the forming candle's close
        await conn.execute(text(
            "UPDATE macd_signals SET created_at = to_timestamp((cross_candle_ms + 7200000) / 1000.0) "
            "+ interval '15 minutes' WHERE id = 13"
        ))
        await conn.execute(text("UPDATE macd_signals SET is_confirmed = FALSE WHERE id = 14"))
    await engine.dispose()

    await _boot(monkeypatch, url)
    rows = dict(
        (sid, (source, eligible))
        for sid, source, eligible in await _fetch(url, "SELECT id, source, is_eligible FROM macd_signals WHERE id IN (12, 13, 14)")
    )
    assert rows == {12: ("cron", True), 13: ("catchup", False), 14: ("manual", False)}

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_maker", None)
    assert await db.init_db(max_retries=1)
    try:
        perf = await db.get_performance()
        assert perf["open_count"] == 3
        assert perf["excluded"] == {"legacy": 4, "catchup": 1, "manual": 1, "other_version": 0, "outside_ledger": 0, "other": 0}
    finally:
        await db.close_db()
