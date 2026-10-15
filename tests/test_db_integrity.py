import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from src.services import db
from src.strategy.macd import MacdSignal

pytestmark = pytest.mark.unit


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _make_signal() -> MacdSignal:
    return MacdSignal(
        symbol="BTCUSDT",
        side="LONG",
        reason="MACD_GOLDEN_CROSS",
        cross_candle_ms=1_700_000_000_000,
        cross_time="2026-10-11 00:00:00",
        cross_price=100.0,
        entry_price=100.0,
        stop_loss=95.0,
        tp1=107.5,
        risk_reward_ratio=1.5,
        macd=1.0,
        signal=0.5,
        histogram=0.5,
        close=101.0,
        bars_ago=1,
    )


class FakeResult:
    def __init__(self, value=None, rows=None):
        self._value = value
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(first=lambda: self._rows[0] if self._rows else None, all=lambda: self._rows)


class FakeSession:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.statements.append(stmt)
        return self.results.pop(0) if self.results else FakeResult()

    async def commit(self):
        self.commits += 1


def test_macd_signal_unique_index_declared():
    indexes = {idx.name: idx for idx in db.MacdSignalModel.__table__.indexes}
    idx = indexes["uq_macd_signals_strategy_candle"]
    assert idx.unique
    assert [c.name for c in idx.columns] == list(db.SIGNAL_UNIQUE_COLUMNS)
    # One row per cross per strategy version, so a shadow strategy can record the same candle
    assert db.SIGNAL_UNIQUE_COLUMNS[:2] == ("strategy_key", "strategy_version")
    assert db.LEGACY_SIGNAL_UNIQUE_INDEX not in indexes


def test_profitloss_signal_id_unique_index_declared():
    indexes = {idx.name: idx for idx in db.ProfitLossHistoryModel.__table__.indexes}
    idx = indexes["uq_profitloss_history_signal_id"]
    assert idx.unique
    assert [c.name for c in idx.columns] == ["signal_id"]
    assert db.ProfitLossHistoryModel.__table__.c.signal_id.nullable


def test_signal_insert_uses_on_conflict_do_nothing_returning_id():
    sql = _compile(db.build_signal_insert(_make_signal(), "2h"))
    assert "INSERT INTO macd_signals" in sql
    assert (
        "ON CONFLICT (strategy_key, strategy_version, symbol, side, timeframe, cross_candle_ms) DO NOTHING" in sql
    )
    assert "RETURNING macd_signals.id" in sql


def test_profitloss_insert_uses_on_conflict_signal_id():
    sql = _compile(
        db.build_profitloss_insert(
            signal_id=1,
            symbol="BTCUSDT",
            timeframe="2h",
            direction="LONG",
            entry_price=100.0,
            stop_loss=95.0,
            tp1=107.5,
            exit_price=107.5,
            result="PROFIT",
            pnl_percent=7.5,
            risk_reward_ratio=1.5,
            cross_time="2026-10-11 00:00:00",
            exit_time="2026-10-11 02:00:00",
        )
    )
    assert "INSERT INTO profitloss_history" in sql
    assert "ON CONFLICT (signal_id) DO NOTHING" in sql


def test_migrations_dedupe_before_creating_unique_indexes():
    statements = [" ".join(m.split()) for m in db.UNIQUE_INDEX_MIGRATIONS]
    delete_signals = next(i for i, s in enumerate(statements) if s.startswith("DELETE FROM macd_signals"))
    delete_pl = next(i for i, s in enumerate(statements) if s.startswith("DELETE FROM profitloss_history"))
    create_signals = next(
        i for i, s in enumerate(statements)
        if "CREATE UNIQUE INDEX IF NOT EXISTS uq_macd_signals_strategy_candle" in s
    )
    add_columns = [i for i, s in enumerate(statements) if s.startswith("ALTER TABLE macd_signals ADD COLUMN")]
    drop_old = statements.index(f"DROP INDEX IF EXISTS {db.LEGACY_SIGNAL_UNIQUE_INDEX};")
    # The strategy columns exist before the dedup reads them, and the old index goes only once the
    # new one guards every cross
    assert add_columns and max(add_columns) < delete_signals
    assert create_signals < drop_old
    create_pl = next(
        i for i, s in enumerate(statements)
        if "CREATE UNIQUE INDEX IF NOT EXISTS uq_profitloss_history_signal_id" in s
    )
    repoint = next(i for i, s in enumerate(statements) if s.startswith("UPDATE profitloss_history"))
    assert repoint < delete_signals < create_signals
    assert delete_signals < delete_pl < create_pl
    assert all("r.rn > 1" in statements[i] for i in (repoint, delete_signals, delete_pl))


def test_migration_survivor_prefers_resolved_signal():
    rank = " ".join(db.SIGNAL_SURVIVOR_RANK.split())
    assert (
        "PARTITION BY COALESCE(strategy_key, 'macd_cross'), COALESCE(strategy_version, ''), "
        "symbol, side, timeframe, cross_candle_ms" in rank
    )
    assert "ORDER BY (COALESCE(win_status, 'PENDING') <> 'PENDING') DESC, id ASC" in rank
    assert "ROW_NUMBER() OVER w AS rn" in rank
    assert "FIRST_VALUE(id) OVER w AS keep_id" in rank

    statements = [" ".join(m.split()) for m in db.UNIQUE_INDEX_MIGRATIONS]
    repoint = next(s for s in statements if s.startswith("UPDATE profitloss_history"))
    delete_signals = next(s for s in statements if s.startswith("DELETE FROM macd_signals"))
    assert rank in repoint
    assert rank in delete_signals
    assert "SET signal_id = r.keep_id" in repoint


def test_migration_profitloss_dedupe_prefers_result_matching_survivor():
    statements = [" ".join(m.split()) for m in db.UNIQUE_INDEX_MIGRATIONS]
    delete_pl = next(s for s in statements if s.startswith("DELETE FROM profitloss_history"))
    assert "PARTITION BY pl.signal_id" in delete_pl
    assert "LEFT JOIN macd_signals s ON s.id = pl.signal_id" in delete_pl
    assert (
        "ORDER BY ( (s.win_status = 'TP1_HIT' AND pl.result = 'PROFIT') "
        "OR (s.win_status = 'SL_HIT' AND pl.result = 'LOSS') ) IS TRUE DESC, pl.id ASC"
    ) in delete_pl


async def test_save_macd_signal_returns_inserted_row(monkeypatch):
    row = SimpleNamespace(to_dict=lambda: {"id": 7, "symbol": "BTCUSDT"})
    session = FakeSession([FakeResult(value=7), FakeResult(rows=[row])])
    monkeypatch.setattr(db, "_session_maker", lambda: session)

    saved = await db.save_macd_signal(_make_signal(), timeframe="2h")

    assert saved == {"id": 7, "symbol": "BTCUSDT", "inserted": True}
    assert "ON CONFLICT" in _compile(session.statements[0])
    assert "macd_signals.id = " in _compile(session.statements[1])
    assert session.commits == 1


async def test_save_macd_signal_returns_existing_row_on_conflict(monkeypatch):
    row = SimpleNamespace(to_dict=lambda: {"id": 3, "symbol": "BTCUSDT"})
    session = FakeSession([FakeResult(value=None), FakeResult(rows=[row])])
    monkeypatch.setattr(db, "_session_maker", lambda: session)

    saved = await db.save_macd_signal(_make_signal(), timeframe="2h")

    assert saved == {"id": 3, "symbol": "BTCUSDT", "inserted": False}
    lookup_sql = _compile(session.statements[1])
    assert "macd_signals.cross_candle_ms = " in lookup_sql
    assert "macd_signals.timeframe = " in lookup_sql


async def test_save_macd_signal_returns_none_when_conflict_row_missing(monkeypatch):
    session = FakeSession([FakeResult(value=None), FakeResult(rows=[])])
    monkeypatch.setattr(db, "_session_maker", lambda: session)

    assert await db.save_macd_signal(_make_signal(), timeframe="2h") is None


async def test_save_macd_signal_returns_none_without_db(monkeypatch):
    monkeypatch.setattr(db, "_session_maker", None)

    assert await db.save_macd_signal(_make_signal(), timeframe="2h") is None


async def test_save_macd_signal_returns_none_on_error(monkeypatch):
    class BrokenSession(FakeSession):
        async def execute(self, stmt):
            raise RuntimeError("db down")

    monkeypatch.setattr(db, "_session_maker", lambda: BrokenSession([]))

    assert await db.save_macd_signal(_make_signal(), timeframe="2h") is None


async def test_backfill_skips_signals_with_existing_profitloss(monkeypatch):
    rec = SimpleNamespace(
        id=11,
        symbol="ETHUSDT",
        side="SHORT",
        timeframe="2h",
        win_status="SL_HIT",
        entry_price=100.0,
        cross_price=100.0,
        stop_loss=105.0,
        tp1=92.5,
        risk_reward_ratio=1.5,
        cross_time="2026-10-11 00:00:00",
        is_confirmed=True,
        entry_basis="next_open",
        source="cron",
        strategy_version="2.2.0",
    )
    session = FakeSession([FakeResult(rows=[rec])])

    await db.backfill_resolved_signals_to_profitloss_history(session)

    select_sql = _compile(session.statements[0])
    assert "NOT (EXISTS" in select_sql
    insert_sql = _compile(session.statements[1])
    assert "ON CONFLICT (signal_id) DO NOTHING" in insert_sql
    assert session.commits == 1


async def test_evaluate_pending_signals_is_serialized(monkeypatch):
    state = {"active": 0, "max_active": 0, "calls": 0}

    async def fake_unlocked(client):
        state["active"] += 1
        state["calls"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0.01)
        state["active"] -= 1
        return {"evaluated_count": 1, "profit_count": 1, "loss_count": 0}

    monkeypatch.setattr(db, "_session_maker", object())
    monkeypatch.setattr(db, "_evaluation_lock", asyncio.Lock())
    monkeypatch.setattr(db, "_evaluate_pending_signals_unlocked", fake_unlocked)

    results = await asyncio.gather(*(db.evaluate_pending_signals(object()) for _ in range(4)))

    assert state["calls"] == 4
    assert state["max_active"] == 1
    assert all(r == {"evaluated_count": 1, "profit_count": 1, "loss_count": 0} for r in results)


async def test_evaluate_pending_signals_without_db_skips_lock(monkeypatch):
    lock = asyncio.Lock()
    await lock.acquire()
    monkeypatch.setattr(db, "_session_maker", None)
    monkeypatch.setattr(db, "_evaluation_lock", lock)

    result = await asyncio.wait_for(db.evaluate_pending_signals(object()), timeout=1)

    assert result == {"evaluated_count": 0, "profit_count": 0, "loss_count": 0, "expired_count": 0, "closed": []}
    lock.release()


async def test_evaluate_pending_signals_inserts_profitloss_with_on_conflict(monkeypatch):
    rec = SimpleNamespace(
        id=21,
        symbol="BTCUSDT",
        side="LONG",
        timeframe="2h",
        win_status="PENDING",
        cross_candle_ms=1_700_000_000_000,
        entry_price=100.0,
        cross_price=100.0,
        stop_loss=95.0,
        tp1=107.5,
        risk_reward_ratio=1.5,
        cross_time="2026-10-11 00:00:00",
        is_confirmed=True,
        entry_basis="next_open",
        source="cron",
        strategy_version="2.2.0",
    )
    session = FakeSession([FakeResult(rows=[rec]), FakeResult(value=1)])
    client = SimpleNamespace(
        klines=lambda **kwargs: [
            [1_700_000_000_000, "100", "101", "99", "100"],
            [1_700_007_200_000, "100", "108", "99", "107"],
        ]
    )
    monkeypatch.setattr(db, "_session_maker", lambda: session)
    monkeypatch.setattr(db, "_evaluation_lock", asyncio.Lock())
    monkeypatch.setattr(db, "ROUND_TRIP_FEE_PERCENT", 0.1)
    monkeypatch.setattr(db, "slippage_bps_for", lambda symbol: 2.0)

    result = await db.evaluate_pending_signals(client)

    closed = result.pop("closed")
    assert result == {"evaluated_count": 1, "profit_count": 1, "loss_count": 0, "expired_count": 0}
    assert rec.win_status == "TP1_HIT"
    assert [t["signal_id"] for t in closed] == [21]
    assert closed[0]["exit_time"] == "2023-11-15T00:13:20Z"
    # Target at its limit; entry slips 2 bps to 100.02, so net = 7.48% - 0.1% fee on 5% risk
    trade = closed[0]
    assert trade["gross_r"] == pytest.approx(1.5)
    assert trade["cost_r"] == pytest.approx(0.024)
    assert trade["net_r"] == pytest.approx(1.476)
    assert trade["r_multiple"] == pytest.approx(trade["net_r"])
    assert trade["pnl_percent"] == pytest.approx(7.38)
    assert trade["fee_percent"] == 0.1
    assert trade["slippage_bps"] == 2.0
    assert trade["cost_model_version"] == db.COST_MODEL_VERSION
    assert trade["entry_basis"] == "next_open"
    assert rec.source == "cron"
    insert_sql = _compile(session.statements[1])
    assert "INSERT INTO profitloss_history" in insert_sql
    assert "ON CONFLICT (signal_id) DO NOTHING" in insert_sql
    assert session.commits == 1


async def test_evaluate_reports_only_newly_inserted_trades(monkeypatch):
    rec = SimpleNamespace(
        id=22,
        symbol="ETHUSDT",
        side="SHORT",
        timeframe="2h",
        win_status="PENDING",
        cross_candle_ms=1_700_000_000_000,
        entry_price=100.0,
        cross_price=100.0,
        stop_loss=105.0,
        tp1=92.5,
        risk_reward_ratio=1.5,
        cross_time="2026-10-11 00:00:00",
        is_confirmed=True,
        entry_basis="next_open",
        source="cron",
        strategy_version="2.2.0",
    )
    # The P/L row already exists (another worker closed it), so the insert returns no id
    session = FakeSession([FakeResult(rows=[rec]), FakeResult(value=None)])
    client = SimpleNamespace(
        klines=lambda **kwargs: [
            [1_700_000_000_000, "100", "101", "99", "100"],
            [1_700_007_200_000, "100", "106", "99", "105"],
        ]
    )
    monkeypatch.setattr(db, "_session_maker", lambda: session)
    monkeypatch.setattr(db, "_evaluation_lock", asyncio.Lock())

    result = await db.evaluate_pending_signals(client)

    assert result["loss_count"] == 1
    assert result["closed"] == []
    assert rec.win_status == "SL_HIT"


async def test_evaluator_books_interpolated_entry_as_legacy(monkeypatch):
    # Like production BTC #4: still pending, stored with the interpolated cross as its entry
    rec = SimpleNamespace(
        id=4,
        symbol="BTCUSDT",
        side="LONG",
        timeframe="2h",
        win_status="PENDING",
        cross_candle_ms=1_700_000_000_000,
        entry_price=100.0,
        cross_price=100.0,
        stop_loss=95.0,
        tp1=107.5,
        risk_reward_ratio=1.5,
        cross_time="2026-10-13T06:00:00Z",
        is_confirmed=True,
        entry_basis=None,
        source=None,
        strategy_version=None,
    )
    session = FakeSession([FakeResult(rows=[rec]), FakeResult(value=1)])
    client = SimpleNamespace(
        klines=lambda **kwargs: [
            [1_700_000_000_000, "100", "101", "99", "100"],
            [1_700_007_200_000, "100", "101", "94", "95"],
        ]
    )
    monkeypatch.setattr(db, "_session_maker", lambda: session)
    monkeypatch.setattr(db, "_evaluation_lock", asyncio.Lock())

    result = await db.evaluate_pending_signals(client)

    [trade] = result["closed"]
    assert trade["entry_price"] == 100.0
    assert trade["entry_basis"] == "cross_interp"
    assert trade["fee_percent"] == db.ROUND_TRIP_FEE_PERCENT
    assert trade["net_r"] < trade["gross_r"] == pytest.approx(-1.0)
    assert (rec.source, rec.entry_basis, rec.strategy_version) == ("legacy", "cross_interp", "legacy")


async def test_evaluator_without_entry_books_cross_price_as_legacy(monkeypatch):
    rec = SimpleNamespace(
        id=30,
        symbol="ETHUSDT",
        side="SHORT",
        timeframe="2h",
        win_status="PENDING",
        cross_candle_ms=1_700_000_000_000,
        entry_price=0.0,
        cross_price=100.0,
        stop_loss=105.0,
        tp1=92.5,
        risk_reward_ratio=1.5,
        cross_time="2026-10-11T00:00:00Z",
        is_confirmed=True,
        entry_basis="next_open",
        source="cron",
        strategy_version="2.2.0",
    )
    session = FakeSession([FakeResult(rows=[rec]), FakeResult(value=1)])
    client = SimpleNamespace(
        klines=lambda **kwargs: [
            [1_700_000_000_000, "100", "101", "99", "100"],
            [1_700_007_200_000, "100", "106", "99", "105"],
        ]
    )
    monkeypatch.setattr(db, "_session_maker", lambda: session)
    monkeypatch.setattr(db, "_evaluation_lock", asyncio.Lock())

    result = await db.evaluate_pending_signals(client)

    [trade] = result["closed"]
    assert trade["entry_price"] == 100.0
    assert trade["entry_basis"] == "cross_interp"
    assert rec.source == "legacy"


def test_backfill_helper_has_no_fee_free_or_cross_price_fallback():
    import inspect

    for fn in (db.backfill_resolved_signals_to_profitloss_history, db._evaluate_pending_signals_unlocked):
        src = inspect.getsource(fn)
        assert "or rec.cross_price" not in src
        assert ", 0.0)" not in src


async def test_backfill_books_resolved_signal_with_fees(monkeypatch):
    rec = SimpleNamespace(
        id=12,
        symbol="SOLUSDT",
        side="LONG",
        timeframe="2h",
        win_status="TP1_HIT",
        entry_price=100.0,
        cross_price=99.0,
        stop_loss=95.0,
        tp1=107.5,
        risk_reward_ratio=1.5,
        cross_time="2026-10-11T00:00:00Z",
        is_confirmed=True,
        entry_basis="next_open",
        source="cron",
        strategy_version="2.2.0",
    )
    session = FakeSession([FakeResult(rows=[rec])])
    monkeypatch.setattr(db, "ROUND_TRIP_FEE_PERCENT", 0.1)
    monkeypatch.setattr(db, "slippage_bps_for", lambda symbol: 10.0)

    await db.backfill_resolved_signals_to_profitloss_history(session)

    params = session.statements[1].compile(dialect=postgresql.dialect()).params
    assert params["fee_percent"] == 0.1
    assert params["entry_price"] == 100.0
    assert params["gross_r"] == pytest.approx(1.5)
    assert params["net_r"] == pytest.approx(1.46)
    assert params["r_multiple"] == pytest.approx(1.46)
    assert params["cost_model_version"] == db.COST_MODEL_VERSION


def _migration(prefix: str) -> str:
    statements = [" ".join(m.split()) for m in db.PROVENANCE_MIGRATIONS]
    return next(s for s in statements if s.startswith(prefix))


def test_provenance_migrations_only_add_columns_and_fill_nulls():
    statements = [" ".join(m.split()) for m in db.PROVENANCE_MIGRATIONS]
    for stmt in statements:
        if stmt.startswith("ALTER TABLE"):
            assert "ADD COLUMN IF NOT EXISTS" in stmt
            assert "DROP" not in stmt
        else:
            assert stmt.startswith(("UPDATE macd_signals", "UPDATE profitloss_history SET booked_by"))
            assert " IS NULL;" in stmt
    # The historical outcome columns are never rewritten
    assert not any("pnl_percent" in s or "r_multiple" in s or "win_status =" in s for s in statements)


def test_provenance_migrations_add_every_column():
    statements = " ".join(" ".join(m.split()) for m in db.PROVENANCE_MIGRATIONS)
    for column in ("strategy_key", "strategy_version", "source", "entry_basis", "detected_at", "is_eligible"):
        assert f"ALTER TABLE macd_signals ADD COLUMN IF NOT EXISTS {column} " in statements
    for column in ("gross_r", "cost_r", "net_r", "cost_model_version", "slippage_bps", "entry_basis", "booked_by"):
        assert f"ALTER TABLE profitloss_history ADD COLUMN IF NOT EXISTS {column} " in statements
    assert (
        "is_eligible BOOLEAN GENERATED ALWAYS AS "
        "(COALESCE(source = 'cron' AND entry_basis = 'next_open', FALSE)) STORED"
    ) in statements


def test_provenance_classification_order():
    statements = [" ".join(m.split()) for m in db.PROVENANCE_MIGRATIONS]
    first_update = next(i for i, s in enumerate(statements) if s.startswith("UPDATE"))
    assert all(s.startswith("ALTER TABLE") for s in statements[:first_update])
    order = [
        next(i for i, s in enumerate(statements) if s.startswith(f"UPDATE macd_signals{suffix}"))
        for suffix in (" SET entry_basis", " AS m SET source", " SET strategy_version", " SET detected_at")
    ]
    assert order == sorted(order)


def test_entry_basis_backfill_marks_interpolated_entries():
    sql = _migration("UPDATE macd_signals SET entry_basis")
    assert (
        "WHEN entry_price IS NULL OR entry_price <= 0 "
        "OR ABS(entry_price - cross_price) <= 1e-09 * ABS(cross_price) THEN 'cross_interp'"
    ) in sql
    assert "WHEN is_confirmed IS FALSE THEN 'forming_close' ELSE 'next_open' END" in sql
    assert sql.endswith("WHERE entry_basis IS NULL;")


def test_source_backfill_marks_legacy_then_catchup_then_cron():
    sql = _migration("UPDATE macd_signals AS m SET source")
    legacy = sql.index("THEN 'legacy'")
    catchup = sql.index("WHEN m.bars_ago > 0 THEN 'catchup'")
    assert legacy < catchup < sql.index("ELSE 'cron'")
    assert "m.entry_basis = 'cross_interp' OR EXISTS" in sql
    assert "WHERE p.signal_id = m.id AND COALESCE(p.fee_percent, 0) = 0" in sql
    assert sql.endswith("WHERE m.source IS NULL;")


def test_source_backfill_applies_the_runtime_late_rule_and_keeps_forming_entries_out():
    sql = _migration("UPDATE macd_signals AS m SET source")
    # Stored more than the grace period after the entry candle opened: late, like is_late_signal
    late = (
        "WHEN m.created_at > to_timestamp( (m.cross_candle_ms + CASE m.timeframe WHEN '2h' THEN 7200000 "
        f"ELSE 7200000 END) / 1000.0 ) + interval '{db.LATE_SIGNAL_GRACE_SECONDS} seconds' THEN 'catchup'"
    )
    assert late in sql
    # Only a manual scan of the forming candle stores a forming-close entry
    forming = "WHEN m.entry_basis = 'forming_close' THEN 'manual'"
    assert sql.index("WHEN m.bars_ago > 0") < sql.index(late) < sql.index(forming) < sql.index("ELSE 'cron'")


def test_booked_by_backfill_tells_the_evaluator_from_legacy_rows():
    sql = _migration("UPDATE profitloss_history SET booked_by")
    assert sql == (
        "UPDATE profitloss_history SET booked_by = CASE WHEN fee_percent > 0 THEN 'evaluator' ELSE 'legacy' END "
        "WHERE booked_by IS NULL;"
    )
    statements = [" ".join(m.split()) for m in db.PROVENANCE_MIGRATIONS]
    added = statements.index("ALTER TABLE profitloss_history ADD COLUMN IF NOT EXISTS booked_by VARCHAR(10);")
    assert added < statements.index(sql)


def test_version_and_detection_backfill():
    version = _migration("UPDATE macd_signals SET strategy_version")
    assert "WHEN source = 'legacy' THEN 'legacy' ELSE '2.2.0' END" in version
    assert version.endswith("WHERE strategy_version IS NULL;")
    assert _migration("UPDATE macd_signals SET strategy_key") == (
        "UPDATE macd_signals SET strategy_key = 'macd_cross' WHERE strategy_key IS NULL;"
    )
    assert _migration("UPDATE macd_signals SET detected_at") == (
        "UPDATE macd_signals SET detected_at = created_at WHERE detected_at IS NULL;"
    )


# Production snapshot (2026-10-15): (id, entry, cross, bars_ago, linked fee_percent or None)
PRODUCTION_SIGNALS = [
    (1, 2526.240353, 2526.240353, 2, 0.0),
    (2, 110.065444, 110.065444, 3, 0.0),
    (3, 1261.156715, 1261.156715, 2, 0.0),
    (4, 83691.975102, 83691.975102, 0, None),
    (5, 1.3699, 1.369027, 0, 0.1),
    (6, 107.51, 107.471533, 0, 0.1),
    (7, 1217.85, 1218.999049, 0, 0.1),
    (8, 2512.59, 2509.265884, 0, 0.1),
    (9, 2495.86, 2499.172812, 0, 0.1),
    (10, 5.028, 5.030624, 0, 0.1),
    (11, 82282.3, 83276.063867, 0, None),
    (12, 104.82, 105.35391, 0, None),
    (13, 4.841, 4.914539, 0, None),
    (14, 1.3266, 1.344051, 0, None),
    (15, 1143.65, 1187.385305, 0, None),
]


def _classify_like_backfill(entry, cross, bars_ago, fee):
    """The backfill CASE expressions, evaluated in Python for the production rows."""
    basis = db.classify_entry_basis(entry, cross)
    if basis == "cross_interp" or fee == 0:
        source = "legacy"
    elif bars_ago > 0:
        source = "catchup"
    else:
        source = "cron"
    return basis, source, source == "cron" and basis == "next_open"


def test_production_rows_classify_as_expected():
    classified = {
        sid: _classify_like_backfill(entry, cross, bars_ago, fee)
        for sid, entry, cross, bars_ago, fee in PRODUCTION_SIGNALS
    }
    for sid in (1, 2, 3, 4):
        assert classified[sid] == ("cross_interp", "legacy", False)
    for sid in range(5, 16):
        assert classified[sid] == ("next_open", "cron", True)


def test_classify_entry_basis():
    assert db.classify_entry_basis(None, 100.0) == "cross_interp"
    assert db.classify_entry_basis(0.0, 100.0) == "cross_interp"
    assert db.classify_entry_basis(100.0, 100.0) == "cross_interp"
    assert db.classify_entry_basis(83691.975102, 83691.975102) == "cross_interp"
    assert db.classify_entry_basis(100.01, 100.0) == "next_open"
    assert db.classify_entry_basis(100.01, 100.0, is_confirmed=False) == "forming_close"


def test_signal_insert_stamps_provenance():
    detected = datetime(2026, 10, 15, 8, 0, 17, tzinfo=UTC)
    params = db.build_signal_insert(_make_signal(), "2h", source="cron", detected_at=detected).compile(
        dialect=postgresql.dialect()
    ).params
    assert params["source"] == "cron"
    assert params["strategy_key"] == db.STRATEGY_KEY
    assert params["strategy_version"] == db.STRATEGY_VERSION
    assert params["entry_basis"] == "next_open"
    assert params["detected_at"] == detected
    assert "is_eligible" not in params


def test_signal_insert_defaults_to_manual_with_app_clock():
    before = datetime.now(UTC)
    params = db.build_signal_insert(_make_signal(), "2h").compile(dialect=postgresql.dialect()).params
    assert params["source"] == "manual"
    assert params["detected_at"] >= before


def test_signal_insert_rejects_unknown_source():
    with pytest.raises(ValueError):
        db.build_signal_insert(_make_signal(), "2h", source="cron_live")


def test_is_eligible_is_a_generated_column():
    column = db.MacdSignalModel.__table__.c.is_eligible
    assert column.computed is not None
    assert str(column.computed.sqltext) == db.IS_ELIGIBLE_SQL


class RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


async def test_profitloss_cost_backfill_restates_legacy_and_v22_rows(monkeypatch):
    monkeypatch.setattr(db, "ROUND_TRIP_FEE_PERCENT", 0.1)
    monkeypatch.setattr(db, "slippage_bps_for", lambda symbol: 2.0 if symbol == "ETHUSDT" else 10.0)
    # Legacy ETH #1: interpolated entry, target, booked with no fee
    legacy_signal = SimpleNamespace(
        side="SHORT", entry_price=2526.240353, stop_loss=2584.12794, entry_basis="cross_interp"
    )
    legacy_trade = SimpleNamespace(
        symbol="ETHUSDT", direction="SHORT", entry_price=2526.240353, stop_loss=2584.12794,
        exit_price=2439.408973, result="PROFIT", pnl_percent=3.44, r_multiple=1.5,
    )
    # v2.2 NEAR #10: stopped out
    v22_signal = SimpleNamespace(side="LONG", entry_price=5.028, stop_loss=4.953074, entry_basis="next_open")
    v22_trade = SimpleNamespace(
        symbol="NEARUSDT", direction="LONG", entry_price=5.028, stop_loss=4.953074,
        exit_price=4.953074, result="LOSS", pnl_percent=-1.59, r_multiple=-1.07,
    )
    # A legacy row with no signal is restated from its own levels
    orphan = SimpleNamespace(
        symbol="XRPUSDT", direction="LONG", entry_price=100.0, stop_loss=95.0,
        exit_price=107.5, result="PROFIT", pnl_percent=7.5, r_multiple=None,
    )
    session = FakeSession(
        [RowsResult([(legacy_trade, legacy_signal), (v22_trade, v22_signal), (orphan, None)])]
    )

    await db.backfill_profitloss_costs(session)

    backfill_sql = _compile(session.statements[0])
    assert "profitloss_history.cost_model_version IS NULL" in backfill_sql
    # Hand-entered rows keep what was typed in: never restated, so never given a net R
    assert "profitloss_history.booked_by IS DISTINCT FROM " in backfill_sql
    assert "manual" in session.statements[0].compile(dialect=postgresql.dialect()).params.values()
    assert session.commits == 1
    assert legacy_trade.gross_r == pytest.approx(1.5)
    assert legacy_trade.net_r < legacy_trade.gross_r
    assert legacy_trade.net_r == pytest.approx(legacy_trade.gross_r - legacy_trade.cost_r)
    assert legacy_trade.entry_basis == "cross_interp"
    # The originally booked numbers stay as the historical record
    assert (legacy_trade.pnl_percent, legacy_trade.r_multiple) == (3.44, 1.5)
    assert v22_trade.gross_r == pytest.approx(-1.0)
    assert v22_trade.cost_r > 0.0
    assert (v22_trade.pnl_percent, v22_trade.r_multiple) == (-1.59, -1.07)
    assert v22_trade.slippage_bps == 10.0
    assert orphan.gross_r == pytest.approx(1.5)
    assert orphan.net_r == pytest.approx(1.46)
    assert orphan.entry_basis is None
    for trade in (legacy_trade, v22_trade, orphan):
        assert trade.cost_model_version == db.COST_MODEL_VERSION


def test_profitloss_payload_keeps_old_fields_and_adds_cost_fields():
    row = db.ProfitLossHistoryModel(
        id=1, signal_id=9, symbol="ETHUSDT", timeframe="2h", direction="SHORT", entry_price=2495.86,
        stop_loss=2534.62914, tp1=2437.70629, exit_price=2437.70629, result="PROFIT", pnl_percent=2.23,
        r_multiple=1.44, fee_percent=0.1, risk_reward_ratio=1.5, cross_time="t", exit_time="t",
        gross_r=1.5, cost_r=0.0412, net_r=1.4588, cost_model_version="cm1", slippage_bps=2.0,
        entry_basis="next_open",
    )
    data = row.to_dict()
    assert data["r_multiple"] == 1.44 and data["pnl_percent"] == 2.23
    assert (data["gross_r"], data["cost_r"], data["net_r"]) == (1.5, 0.041, 1.459)
    assert data["cost_model_version"] == "cm1"
    assert data["entry_basis"] == "next_open"


def test_signal_payload_adds_provenance():
    row = db.MacdSignalModel(
        id=5, symbol="XRPUSDT", side="LONG", timeframe="2h", reason="r", cross_candle_ms=1, cross_time="t",
        cross_price=1.369027, close_price=1.37, entry_price=1.3699, stop_loss=1.345803, tp1=1.406045,
        risk_reward_ratio=1.5, win_status="SL_HIT", macd=0.0, signal=0.0, histogram=0.0, bars_ago=0,
        is_confirmed=True, strategy_key="macd_cross", strategy_version="2.2.0", source="cron",
        entry_basis="next_open", detected_at=datetime(2026, 10, 13, 16, 0, 16, tzinfo=UTC), is_eligible=True,
    )
    data = row.to_dict()
    assert data["entry_price"] == 1.3699
    assert data["source"] == "cron"
    assert data["strategy_version"] == "2.2.0"
    assert data["entry_basis"] == "next_open"
    assert data["detected_at"] == "2026-10-13T16:00:16+00:00"
    assert data["is_eligible"] is True


def test_utc_day_bounds_are_half_open_and_keep_the_last_day():
    start, end = db.utc_day_bounds("2026-10-12", "2026-10-13")
    assert start == datetime(2026, 10, 12, tzinfo=UTC)
    assert end == datetime(2026, 10, 14, tzinfo=UTC)
    assert db.utc_day_bounds(None, None) == (None, None)
    assert db.utc_day_bounds(None, " 2026-10-13 ") == (None, datetime(2026, 10, 14, tzinfo=UTC))


@pytest.mark.parametrize("bad", ["2026-13-01", "13/10/2026", "2026-10-13 23:59:59", "x"])
def test_utc_day_bounds_reject_other_formats(bad):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        db.utc_day_bounds(bad, None)


def test_text_times_compare_as_timestamps():
    sql = _compile(db._utc_text_as_timestamp(db.ProfitLossHistoryModel.cross_time) >= datetime(2026, 10, 13))
    assert "CASE WHEN" in sql
    assert "profitloss_history.cross_time ~" in sql
    assert "CAST(replace(replace(profitloss_history.cross_time" in sql
    assert "AS TIMESTAMP WITHOUT TIME ZONE)" in sql


class AnyResult:
    """Fake result answering whichever accessor the code under test uses."""

    def __init__(self, rows=None, scalar=None, one=None):
        self._rows = rows or []
        self._scalar = scalar
        self._one = one

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar

    def one(self):
        return self._one

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


def _performance_session():
    trades = [
        (4, "LOSS", -1.15, -1.0, 0.15, "2026-10-14T10:00:00Z"),
        (9, "PROFIT", 1.46, 1.5, 0.04, "2026-10-14T14:00:00Z"),
        (7, "LOSS", -1.1, -1.0, 0.1, "2026-10-14T14:00:00Z"),
    ]
    return FakeSession(
        [AnyResult(rows=trades), AnyResult(scalar=5), AnyResult(rows=[("legacy", 4), ("catchup", 1), (None, 2)])]
    )


async def test_performance_counts_only_eligible_trades_of_the_version(monkeypatch):
    session = _performance_session()
    monkeypatch.setattr(db, "_session_maker", lambda: session)

    out = await db.get_performance(symbol=" ethusdt ", side="long")

    trades_sql, open_sql, excluded_sql = (_compile(s) for s in session.statements)
    for sql in (trades_sql, open_sql):
        assert "macd_signals.is_eligible IS true" in sql
        assert "macd_signals.strategy_version = " in sql
        assert "macd_signals.symbol = " in sql and "macd_signals.side = " in sql
    assert "JOIN macd_signals ON macd_signals.id = profitloss_history.signal_id" in trades_sql
    # Only the evaluator's own bookings count: never a hand-entered row, an unknown result or no net R
    assert "profitloss_history.booked_by = " in trades_sql
    assert "profitloss_history.result IN " in trades_sql
    assert "profitloss_history.net_r IS NOT NULL" in trades_sql
    assert "macd_signals.win_status != " in trades_sql
    assert "macd_signals.win_status = " in open_sql
    # Excluded covers every signal not counted, other versions included, each under one reason
    assert "macd_signals.is_eligible IS NOT true" in excluded_sql
    assert "macd_signals.strategy_version IS DISTINCT FROM " in excluded_sql
    # Another strategy is checked before the source, so its legacy rows are not this version's
    assert "GROUP BY CASE WHEN (macd_signals.strategy_key IS DISTINCT FROM " in excluded_sql
    # Signals without ledger events never count
    assert "EXISTS (SELECT * \nFROM signal_events \nWHERE signal_events.signal_id = macd_signals.id)" in trades_sql
    assert "macd_signals.strategy_version = " not in excluded_sql
    params = session.statements[0].compile(dialect=postgresql.dialect()).params
    assert db.STRATEGY_VERSION in params.values()
    assert "ETHUSDT" in params.values() and "LONG" in params.values()

    assert out["strategy_version"] == db.STRATEGY_VERSION
    assert out["filters"] == {"symbol": "ETHUSDT", "side": "LONG"}
    assert (out["n_closed"], out["wins"], out["losses"]) == (3, 1, 2)
    assert out["open_count"] == 5
    assert out["excluded"] == {"legacy": 4, "catchup": 1, "manual": 0, "other_version": 0, "outside_ledger": 0, "other": 0}
    assert out["total_net_r"] == pytest.approx(-0.79)
    # Exit order 10:00, then the two 14:00 exits by id: -1.15, -1.1, +1.46
    assert out["max_drawdown_r"] == pytest.approx(2.25)
    assert out["cost_model_version"] == db.COST_MODEL_VERSION
    assert out["sample_size_warning"] == "insufficient sample"


async def test_performance_takes_the_requested_version(monkeypatch):
    session = _performance_session()
    monkeypatch.setattr(db, "_session_maker", lambda: session)
    out = await db.get_performance(strategy_version="2.1.0")
    assert out["strategy_version"] == "2.1.0"
    assert "2.1.0" in session.statements[0].compile(dialect=postgresql.dialect()).params.values()


async def test_performance_without_db_is_empty(monkeypatch):
    monkeypatch.setattr(db, "_session_maker", None)
    out = await db.get_performance()
    assert out["n_closed"] == 0 and out["win_rate"] is None
    assert out["excluded"] == {"legacy": 0, "catchup": 0, "manual": 0, "other_version": 0, "outside_ledger": 0, "other": 0}


async def test_summary_reports_eligible_outcomes_with_target_rr(monkeypatch):
    counts = SimpleNamespace(
        _mapping={"total": 15, "long_cnt": 7, "short_cnt": 8, "symbols_cnt": 6, "pending": 6, "eligible_pnl": -7.4}
    )
    session = _performance_session()
    session.results += [AnyResult(one=counts), AnyResult(scalar=6), AnyResult(scalar="2026-10-14T14:00:00Z")]
    monkeypatch.setattr(db, "_session_maker", lambda: session)

    out = await db.get_macd_signals_summary()

    assert "avg_rr" not in out
    assert out["target_rr"] == db.RISK_REWARD_RATIO
    assert out["eligible_only"] is True and out["note"]
    assert (out["total_signals"], out["pending_count"]) == (15, 6)
    assert (out["tp1_hit_count"], out["sl_hit_count"], out["closed_trades"]) == (1, 2, 3)
    # The same win rate as /api/performance: target hits over every closed trade
    assert out["winrate"] == 33.3
    assert out["total_r"] == pytest.approx(-0.79)
    assert out["max_drawdown_r"] == pytest.approx(2.25)
    counts_sql = _compile(session.statements[3])
    assert "sum(profitloss_history.pnl_percent) FILTER (WHERE macd_signals.is_eligible IS true" in counts_sql
    assert "profitloss_history.booked_by = " in counts_sql
    latest_sql = _compile(session.statements[5])
    assert "ORDER BY macd_signals.cross_candle_ms DESC, macd_signals.id DESC" in latest_sql


async def test_signal_list_dates_filter_on_the_cross_candle_half_open(monkeypatch):
    agg = SimpleNamespace(
        total=0, open=0, profit=0, loss=0, expired=0, closed=0, total_r=0.0, pnl=0.0,
        eligible_closed=0, eligible_net_r=0.0, excluded=0,
    )
    session = FakeSession([AnyResult(one=agg), AnyResult(rows=[])])
    monkeypatch.setattr(db, "_session_maker", lambda: session)

    _, _, summary = await db.get_macd_signals(start_date="2026-10-12", end_date="2026-10-13")

    stmt = session.statements[0]
    sql = _compile(stmt)
    assert "macd_signals.cross_candle_ms >= " in sql
    assert "macd_signals.cross_candle_ms < " in sql
    params = stmt.compile(dialect=postgresql.dialect()).params
    day_ms = 86_400_000
    start_ms = int(datetime(2026, 10, 12, tzinfo=UTC).timestamp() * 1000)
    assert start_ms in params.values()
    assert start_ms + 2 * day_ms in params.values()
    assert summary["eligible_closed_count"] == 0 and summary["excluded_count"] == 0
    # Counted figures need an evaluator booking; excluded is every signal outside the running version
    assert "profitloss_history.booked_by = " in sql
    assert (
        "FILTER (WHERE macd_signals.is_eligible IS NOT true OR macd_signals.strategy_key IS DISTINCT FROM "
        in sql
    )
    assert "OR macd_signals.strategy_version IS DISTINCT FROM " in sql
    # Newest cross first, whatever order the rows were stored in
    assert "ORDER BY macd_signals.cross_candle_ms DESC, macd_signals.id DESC" in _compile(session.statements[1])


async def test_signal_list_dates_can_be_local_days(monkeypatch):
    agg = SimpleNamespace(
        total=0, open=0, profit=0, loss=0, expired=0, closed=0, total_r=0.0, pnl=0.0,
        eligible_closed=0, eligible_net_r=0.0, excluded=0,
    )
    session = FakeSession([AnyResult(one=agg), AnyResult(rows=[])])
    monkeypatch.setattr(db, "_session_maker", lambda: session)

    await db.get_macd_signals(date="2026-10-15", tz_offset_minutes=420)

    params = session.statements[0].compile(dialect=postgresql.dialect()).params
    # The WIB day 2026-10-15 runs from 2026-10-14 17:00 UTC to 2026-10-15 17:00 UTC
    start_ms = int(datetime(2026, 10, 14, 17, tzinfo=UTC).timestamp() * 1000)
    assert start_ms in params.values()
    assert start_ms + 86_400_000 in params.values()


def test_utc_day_bounds_with_an_offset():
    start, end = db.utc_day_bounds("2026-10-15", "2026-10-15", tz_offset_minutes=420)
    assert (start, end) == (datetime(2026, 10, 14, 17, tzinfo=UTC), datetime(2026, 10, 15, 17, tzinfo=UTC))
    assert db.utc_day_bounds("2026-10-15", None, -300)[0] == datetime(2026, 10, 15, 5, tzinfo=UTC)


async def test_signal_list_rejects_bad_dates(monkeypatch):
    monkeypatch.setattr(db, "_session_maker", lambda: FakeSession([]))
    with pytest.raises(ValueError, match="end_date"):
        await db.get_macd_signals(end_date="2026-10-32")


async def test_profitloss_summary_labels_as_booked_figures_and_adds_eligible_ones(monkeypatch):
    agg = SimpleNamespace(
        total=10, family_total=9, profit=4, loss=5, expired=0, pnl=9.33, total_r=0.63, avg_r=0.07,
        eligible_closed=6, eligible_wins=1, eligible_net_r=-4.4351,
    )
    session = FakeSession([AnyResult(rows=[]), AnyResult(one=agg)])
    monkeypatch.setattr(db, "_session_maker", lambda: session)

    _, total, summary = await db.get_profitloss_history()

    # The listing total covers every row; the as-booked figures only the headline strategy's
    assert total == 10 and summary["total_trades"] == 9
    # The as-booked figures stay for old clients, next to the counted ones and a note saying which is which
    assert (summary["winrate"], summary["total_r"]) == (44.4, 0.63)
    assert summary["eligible_closed_count"] == 6 and summary["eligible_win_count"] == 1
    assert summary["eligible_win_rate"] == 16.7
    assert summary["eligible_net_r"] == -4.44
    assert summary["strategy_version"] == db.STRATEGY_VERSION
    assert "deprecated" in summary["note"]
    agg_sql = _compile(session.statements[1])
    assert "LEFT OUTER JOIN macd_signals ON macd_signals.id = profitloss_history.signal_id" in agg_sql
    assert "sum(profitloss_history.net_r) FILTER (WHERE macd_signals.is_eligible IS true" in agg_sql
    assert "profitloss_history.booked_by = " in agg_sql
    # A shadow strategy's paper trades never reach the deprecated as-booked figures
    assert "sum(profitloss_history.r_multiple) FILTER (WHERE macd_signals.id IS NULL OR macd_signals.strategy_key = " in agg_sql


async def test_profitloss_dates_compare_as_timestamps(monkeypatch):
    agg = SimpleNamespace(
        total=0, family_total=0, profit=0, loss=0, expired=0, pnl=0.0, total_r=0.0, avg_r=None,
        eligible_closed=0, eligible_wins=0, eligible_net_r=0.0,
    )
    session = FakeSession([AnyResult(rows=[]), AnyResult(one=agg)])
    monkeypatch.setattr(db, "_session_maker", lambda: session)

    await db.get_profitloss_history(start_date="2026-10-12", end_date="2026-10-12")

    stmt = session.statements[0]
    sql = _compile(stmt)
    assert "<= " not in sql
    assert "AS TIMESTAMP WITHOUT TIME ZONE) END >= " in sql
    assert "AS TIMESTAMP WITHOUT TIME ZONE) END < " in sql
    params = stmt.compile(dialect=postgresql.dialect()).params
    assert datetime(2026, 10, 13) in params.values()


async def test_manual_trade_cannot_take_a_stored_signals_slot(monkeypatch):
    session = FakeSession([AnyResult(scalar=11)])
    monkeypatch.setattr(db, "_session_maker", lambda: session)
    with pytest.raises(db.SignalTradeConflict):
        await db.record_profit_loss("BTCUSDT", "SHORT", 82282.3, 84285.8352, 79276.9972, 79276.9972, "PROFIT", signal_id=11)
    # Only the lookup ran; nothing was written
    assert len(session.statements) == 1 and session.commits == 0


class AddingSession(FakeSession):
    def add(self, record):
        self.added = record

    async def refresh(self, record):
        record.id = 1


async def test_manual_trade_is_labelled_manual_without_cost_columns(monkeypatch):
    session = AddingSession([])
    monkeypatch.setattr(db, "_session_maker", lambda: session)
    item = await db.record_profit_loss("ethusdt", "long", 2500.0, 2450.0, 2575.0, 2575.0, "profit")
    assert item["booked_by"] == "manual"
    assert (item["net_r"], item["cost_model_version"]) == (None, None)
    assert (item["symbol"], item["direction"], item["result"]) == ("ETHUSDT", "LONG", "PROFIT")


def test_evaluator_bookings_are_labelled():
    rec = SimpleNamespace(
        id=5, symbol="XRPUSDT", side="LONG", timeframe="2h", stop_loss=1.345803, tp1=1.406045,
        risk_reward_ratio=1.5, cross_time="t",
    )
    values = db.booked_trade_values(rec, 1.3699, "next_open", 1.345803, "LOSS", "t")
    assert values["booked_by"] == "evaluator"
    assert db.ProfitLossHistoryModel.__table__.c.booked_by.type.length == 10


async def test_journal_summary_compares_net_r_of_closed_signals_only(monkeypatch):
    def entry(status):
        return db.TradeJournalModel(status=status, entry_fill=None, exit_fill=None, notes="")

    sig = {sid: SimpleNamespace(id=sid, side="LONG", stop_loss=1.0) for sid in (1, 2, 3)}
    all_rows = [(sig[1], entry("TAKEN")), (sig[2], entry("TAKEN")), (sig[3], entry("SKIPPED"))]
    session = FakeSession([
        AnyResult(scalar=3), AnyResult(rows=[]), AnyResult(rows=all_rows), AnyResult(rows=[(1, -1.1), (3, 1.4)]),
    ])
    monkeypatch.setattr(db, "_session_maker", lambda: session)

    _, total, summary = await db.get_journal()

    assert total == 3
    assert (summary["taken"], summary["skipped"]) == (2, 1)
    # #2 is still open, so it adds nothing and is not counted as closed
    assert (summary["signal_closed_taken"], summary["signal_r_taken"]) == (1, -1.1)
    assert (summary["signal_closed_skipped"], summary["signal_r_skipped"]) == (1, 1.4)
    assert "coalesce(profitloss_history.net_r, profitloss_history.r_multiple)" in _compile(session.statements[3])
