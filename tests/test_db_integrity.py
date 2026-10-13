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
    idx = indexes["uq_macd_signals_symbol_side_tf_candle"]
    assert idx.unique
    assert [c.name for c in idx.columns] == list(db.SIGNAL_UNIQUE_COLUMNS)


def test_profitloss_signal_id_unique_index_declared():
    indexes = {idx.name: idx for idx in db.ProfitLossHistoryModel.__table__.indexes}
    idx = indexes["uq_profitloss_history_signal_id"]
    assert idx.unique
    assert [c.name for c in idx.columns] == ["signal_id"]
    assert db.ProfitLossHistoryModel.__table__.c.signal_id.nullable


def test_signal_insert_uses_on_conflict_do_nothing_returning_id():
    sql = _compile(db.build_signal_insert(_make_signal(), "2h"))
    assert "INSERT INTO macd_signals" in sql
    assert "ON CONFLICT (symbol, side, timeframe, cross_candle_ms) DO NOTHING" in sql
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
        if "CREATE UNIQUE INDEX IF NOT EXISTS uq_macd_signals_symbol_side_tf_candle" in s
    )
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
    assert "PARTITION BY symbol, side, timeframe, cross_candle_ms" in rank
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
    )
    session = FakeSession([FakeResult(rows=[rec])])

    await db.backfill_resolved_signals_to_profitloss_history(session)

    select_sql = _compile(session.statements[0])
    assert "NOT (EXISTS" in select_sql
    insert_sql = _compile(session.statements[1])
    assert "ON CONFLICT (signal_id) DO NOTHING" in insert_sql
    assert session.commits == 1
