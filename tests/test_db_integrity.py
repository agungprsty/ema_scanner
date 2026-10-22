import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from src.services import db
from src.strategy.outcome import Trade
from src.strategy.rsi import Signal

pytestmark = pytest.mark.unit

HOUR = 3_600_000
S = 1_700_000_000_000 - 1_700_000_000_000 % HOUR


@pytest.fixture(autouse=True)
def windows(monkeypatch):
    monkeypatch.setattr(db, "FILL_WINDOW_HOURS", 12.0)
    monkeypatch.setattr(db, "MAX_HOLD_HOURS", 24.0)


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _signal(**overrides) -> Signal:
    values = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "signal_candle_ms": S,
        "rsi": 27.4,
        "prev_rsi": 30.1,
        "candle_close": 100.5,
        "entry_price": 100.0,
        "take_profit": 102.1,
        "stop_loss": 99.1,
        "bars_ago": 0,
    }
    values.update(overrides)
    return Signal(**values)


def _rec(**overrides) -> db.SignalModel:
    values = {
        "id": 21,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "signal_candle_ms": S,
        "rsi": 27.4,
        "prev_rsi": 30.1,
        "candle_close": 100.5,
        "entry_price": 100.0,
        "take_profit": 102.1,
        "stop_loss": 99.1,
        "status": "PENDING",
    }
    values.update(overrides)
    return db.SignalModel(**values)


class FakeResult:
    def __init__(self, value=None, rows=None):
        self._value = value
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


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


class FakeClient:
    def __init__(self, candles):
        self.candles = candles
        self.calls = []

    def klines(self, symbol, interval, startTime, limit):
        self.calls.append((symbol, interval, startTime, limit))
        return [c for c in self.candles if c[0] >= startTime][:limit]


def test_signal_table_schema():
    table = db.SignalModel.__table__
    unique = next(c for c in table.constraints if c.name == "uq_signals_key")
    assert [c.name for c in unique.columns] == ["symbol", "side", "timeframe", "signal_candle_ms"]
    checks = {c.name: str(c.sqltext) for c in table.constraints if c.name and c.name.startswith("ck_")}
    assert checks["ck_signals_side"] == "side IN ('LONG', 'SHORT')"
    assert "'NOT_FILLED'" in checks["ck_signals_status"]
    active = next(i for i in table.indexes if i.name == "ix_signals_active")
    assert str(active.dialect_options["postgresql"]["where"]) == "status IN ('PENDING', 'OPEN')"


def test_journal_references_signals_with_cascade():
    table = db.JournalModel.__table__
    assert table.name == "signal_journal"
    fk = next(iter(table.c.signal_id.foreign_keys))
    assert fk.target_fullname == "signals.id"
    assert fk.ondelete == "CASCADE"
    assert table.c.signal_id.unique and not table.c.signal_id.nullable


def test_signal_insert_skips_stored_hits_and_crosses_inside_a_live_trade_on_the_same_side():
    sql = _compile(db.build_signal_insert(_signal(), "1h"))
    assert "INSERT INTO signals (symbol, side, timeframe, signal_candle_ms" in sql
    assert (
        "WHERE NOT (EXISTS (SELECT signals.id \nFROM signals \nWHERE signals.symbol = %(symbol_1)s::VARCHAR"
        " AND signals.side = %(side_1)s::VARCHAR AND signals.timeframe = %(timeframe_1)s::VARCHAR"
        " AND (signals.status IN (__[POSTCOMPILE_status_1])"
        " OR signals.signal_candle_ms < %(signal_candle_ms_1)s::BIGINT"
        " AND signals.closed_ms > %(closed_ms_1)s::BIGINT)))"
    ) in sql
    assert "ON CONFLICT ON CONSTRAINT uq_signals_key DO NOTHING" in sql
    assert "RETURNING signals.id" in sql


def test_transition_update_only_wins_from_the_old_status():
    sql = _compile(db.build_transition_update(21, "PENDING", {"status": "OPEN", "fill_price": 100.0}))
    assert "UPDATE signals SET status=" in sql
    assert "WHERE signals.id = %(id_1)s::BIGINT AND signals.status = %(status_1)s::VARCHAR" in sql
    assert "updated_at=now()" in sql
    assert sql.endswith("RETURNING signals.id")


def test_alert_claim_only_takes_recent_unalerted_rows():
    sql = _compile(db.build_alert_claim(S))
    assert "UPDATE signals SET alerted_at=now()" in sql
    assert "WHERE signals.alerted_at IS NULL AND signals.signal_candle_ms >= %(signal_candle_ms_1)s" in sql
    assert "RETURNING signals.id" in sql


async def test_claim_unalerted_signals_returns_rows_oldest_first(monkeypatch):
    session = FakeSession([FakeResult(rows=[_rec(id=9), _rec(id=4)])])
    monkeypatch.setattr(db, "_session_maker", lambda: session)

    rows = await db.claim_unalerted_signals(S)

    assert [r["id"] for r in rows] == [4, 9]
    assert session.commits == 1


async def test_insert_signals_returns_inserted_rows(monkeypatch):
    session = FakeSession([FakeResult(rows=[_rec(id=7)])])
    monkeypatch.setattr(db, "_session_maker", lambda: session)

    later, earlier = _signal(signal_candle_ms=S + HOUR), _signal()
    rows = await db.insert_signals([later, earlier], "1h")

    assert [r["id"] for r in rows] == [7]
    assert rows[0]["fill_deadline"] == db._ms_to_iso(S + 13 * HOUR)
    # One statement per hit, oldest first, so the earlier cross opens the trade and blocks the later
    stored = [s.compile().params["param_4"] for s in session.statements]
    assert stored == [S, S + HOUR]
    assert session.commits == 1


async def test_insert_signals_without_db_raises(monkeypatch):
    monkeypatch.setattr(db, "_session_maker", None)
    assert await db.insert_signals([]) == []
    with pytest.raises(RuntimeError):
        await db.insert_signals([_signal()])


def test_signal_row_deadlines_follow_its_timeframe():
    row = _rec(timeframe="15m", fill_ms=S + 900_000).to_dict()
    assert row["fill_deadline"] == db._ms_to_iso(S + 49 * 900_000)
    assert row["hold_deadline"] == db._ms_to_iso(S + 900_000 + 96 * 900_000)
    assert _rec().to_dict()["hold_deadline"] is None


def test_trade_values_use_planned_risk():
    # TP and SL are placed net of fees, so a target is +2% and +2R, a stop -1% and -1R
    target = db.trade_values(_rec(), Trade("TP_HIT", 100.0, S + HOUR, 102.1, S + 2 * HOUR))
    assert target["pnl_percent"] == pytest.approx(2.0) and target["r_multiple"] == pytest.approx(2.0)
    stop = db.trade_values(_rec(), Trade("SL_HIT", 100.0, S + HOUR, 99.1, S + 2 * HOUR))
    assert stop["pnl_percent"] == pytest.approx(-1.0) and stop["r_multiple"] == pytest.approx(-1.0)

    # A fill that gapped near the stop is still measured against the planned 1R
    gap = db.trade_values(_rec(), Trade("SL_HIT", 99.2, S + HOUR, 98.9, S + 2 * HOUR))
    assert gap["r_multiple"] == pytest.approx((98.9 - 99.2) / 99.2 * 100 - db.ROUND_TRIP_FEE_PERCENT)

    not_filled = db.trade_values(_rec(), Trade("NOT_FILLED", closed_ms=S + 13 * HOUR))
    assert not_filled["status"] == "NOT_FILLED"
    assert not_filled["pnl_percent"] is None and not_filled["r_multiple"] is None
    assert not_filled["closed_ms"] == S + 13 * HOUR


def test_fetch_trade_klines_pages_long_windows():
    candles = [[S + i * 60_000, "1", "1", "1", "1"] for i in range(2200)]
    client = FakeClient(candles)

    got = db.fetch_trade_klines(client, "BTCUSDT", "1m", S, 2160, 60_000)

    assert got == candles[:2160]
    assert [(c[2], c[3]) for c in client.calls] == [(S, 1500), (S + 1500 * 60_000, 660)]


def test_fetch_trade_klines_stops_at_the_latest_candle():
    client = FakeClient([[S + i * HOUR, "1", "1", "1", "1"] for i in range(5)])
    assert len(db.fetch_trade_klines(client, "BTCUSDT", "1h", S, 36, HOUR)) == 5
    assert len(client.calls) == 1


async def test_evaluate_active_records_the_transition(monkeypatch):
    rec = _rec()
    client = FakeClient(
        [
            # Fills at the limit; the open was above it, so this candle's high cannot count as TP
            [S + HOUR, "100.5", "102.5", "99.8", "100.2"],
            [S + 2 * HOUR, "100.2", "102.5", "100.0", "102.0"],
        ]
    )
    session = FakeSession([FakeResult(rows=[rec]), FakeResult(value=21)])
    monkeypatch.setattr(db, "_session_maker", lambda: session)
    monkeypatch.setattr(db, "_evaluation_lock", asyncio.Lock())

    result = await db.evaluate_active(client)

    transitions = result.pop("transitions")
    assert result == {
        "evaluated": 1, "filled": 1, "tp": 1, "sl": 0, "expired": 0, "not_filled": 0, "failed": 0
    }
    assert client.calls == [("BTCUSDT", "1h", S + HOUR, 36)]
    assert len(transitions) == 1
    moved = transitions[0]
    assert moved["previous_status"] == "PENDING"
    assert moved["status"] == "TP_HIT"
    assert moved["fill_price"] == 100.0 and moved["exit_price"] == 102.1
    assert moved["pnl_percent"] == pytest.approx(2.0, abs=0.01)
    assert moved["r_multiple"] == pytest.approx(2.0, abs=0.01)
    assert "signals.status = " in _compile(session.statements[1])
    assert session.commits == 1


async def test_evaluate_active_lost_update_sends_nothing(monkeypatch):
    client = FakeClient([[S + HOUR, "100.0", "100.5", "98.5", "99.0"]])
    session = FakeSession([FakeResult(rows=[_rec()]), FakeResult(value=None)])
    monkeypatch.setattr(db, "_session_maker", lambda: session)
    monkeypatch.setattr(db, "_evaluation_lock", asyncio.Lock())

    result = await db.evaluate_active(client)

    assert result["transitions"] == [] and result["sl"] == 0
    assert len(session.statements) == 2


async def test_evaluate_active_never_moves_a_trade_backwards(monkeypatch):
    # An empty klines reply replays as PENDING; the open trade must stay open
    rec = _rec(status="OPEN", fill_price=100.0, fill_ms=S + HOUR)
    session = FakeSession([FakeResult(rows=[rec])])
    monkeypatch.setattr(db, "_session_maker", lambda: session)
    monkeypatch.setattr(db, "_evaluation_lock", asyncio.Lock())

    result = await db.evaluate_active(FakeClient([]))

    assert result["transitions"] == []
    assert len(session.statements) == 1


async def test_evaluate_active_marks_not_filled_without_pnl(monkeypatch):
    candles = [[S + i * HOUR, "101", "101.5", "100.5", "101"] for i in range(1, 13)]
    session = FakeSession([FakeResult(rows=[_rec()]), FakeResult(value=21)])
    monkeypatch.setattr(db, "_session_maker", lambda: session)
    monkeypatch.setattr(db, "_evaluation_lock", asyncio.Lock())

    result = await db.evaluate_active(FakeClient(candles))

    assert result["not_filled"] == 1 and result["filled"] == 0
    moved = result["transitions"][0]
    assert moved["status"] == "NOT_FILLED"
    assert moved["closed_ms"] == S + 13 * HOUR
    assert moved["pnl_percent"] is None and moved["r_multiple"] is None


async def test_evaluate_active_skips_unknown_timeframe(monkeypatch):
    client = FakeClient([])
    session = FakeSession([FakeResult(rows=[_rec(timeframe="1w")])])
    monkeypatch.setattr(db, "_session_maker", lambda: session)
    monkeypatch.setattr(db, "_evaluation_lock", asyncio.Lock())

    result = await db.evaluate_active(client)

    assert result["failed"] == 1
    assert client.calls == []


async def test_evaluate_active_row_errors_do_not_stop_the_run(monkeypatch):
    class FlakyClient(FakeClient):
        def klines(self, symbol, **kwargs):
            if symbol == "ETHUSDT":
                raise RuntimeError("binance down")
            return super().klines(symbol, **kwargs)

    client = FlakyClient([[S + HOUR, "100.0", "100.5", "98.5", "99.0"]])
    records = [_rec(id=1, symbol="ETHUSDT"), _rec(id=2)]
    session = FakeSession([FakeResult(rows=records), FakeResult(value=2)])
    monkeypatch.setattr(db, "_session_maker", lambda: session)
    monkeypatch.setattr(db, "_evaluation_lock", asyncio.Lock())

    result = await db.evaluate_active(client)

    assert result["failed"] == 1 and result["sl"] == 1
    assert [t["id"] for t in result["transitions"]] == [2]


async def test_evaluate_active_is_serialized(monkeypatch):
    state = {"active": 0, "max_active": 0}

    async def fake_unlocked(client):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await asyncio.sleep(0.01)
        state["active"] -= 1
        return db._empty_evaluation()

    monkeypatch.setattr(db, "_session_maker", object())
    monkeypatch.setattr(db, "_evaluation_lock", asyncio.Lock())
    monkeypatch.setattr(db, "_evaluate_active_unlocked", fake_unlocked)

    await asyncio.gather(*(db.evaluate_active(object()) for _ in range(4)))

    assert state["max_active"] == 1


async def test_evaluate_active_without_db_raises(monkeypatch):
    monkeypatch.setattr(db, "_session_maker", None)
    with pytest.raises(RuntimeError):
        await db.evaluate_active(object())


def test_stats_leave_not_filled_out_of_the_win_rate():
    counts = {"total": 10, "PENDING": 1, "OPEN": 1, "TP_HIT": 2, "SL_HIT": 1, "EXPIRED": 1, "NOT_FILLED": 4}
    stats = db.build_stats(counts, 2.5, [1.9, -1.1, 1.9, 0.3], watchlist_count=3, latest_signal_ms=S)

    assert stats["filled_count"] == 5
    assert stats["fill_rate"] == pytest.approx(5 / 9 * 100, abs=0.1)
    assert stats["closed_trades"] == 4
    assert stats["winrate"] == 50.0
    assert stats["total_r"] == pytest.approx(3.0)
    assert stats["avg_r"] == pytest.approx(0.75)
    assert stats["max_drawdown_r"] == pytest.approx(1.1)
    assert stats["pending_count"] == 1 and stats["open_count"] == 1 and stats["not_filled_count"] == 4
    assert stats["latest_signal_time"] == db._ms_to_iso(S)


def test_stats_when_empty():
    stats = db.build_stats({})
    assert stats["fill_rate"] == stats["winrate"] == stats["avg_r"] == 0.0
    assert stats["latest_signal_time"] is None


def test_signal_conditions_use_wib_days_and_status_lists():
    day = int(datetime(2026, 10, 20, 17, tzinfo=UTC).timestamp() * 1000)  # 00:00 WIB 21 Oct
    sql = _compile(db.signal_conditions(status="pending, OPEN", date="2026-10-21")[0])
    assert "signals.status IN" in sql
    conds = db.signal_conditions(date="2026-10-21")
    assert [c.right.value for c in conds] == [day, day + db.DAY_MS]
    end = db.signal_conditions(end_date="2026-10-21")[0]
    assert end.right.value == day + db.DAY_MS
    with pytest.raises(ValueError):
        db.signal_conditions(date="21-10-2026")


def test_max_drawdown_measures_peak_to_trough():
    assert db._max_drawdown([]) == 0.0
    assert db._max_drawdown([1.5, -1.0, -1.0, 1.5, -1.0]) == pytest.approx(2.0)
    assert db._max_drawdown([-1.0, -1.0]) == pytest.approx(2.0)
