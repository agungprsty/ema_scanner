"""The signal ledger against a real PostgreSQL: genesis, append-only triggers, the event chain written
by the app, candle persistence, daily heads, exports and tamper detection.

Opt-in: set TEST_DATABASE_URL to an empty throwaway database, e.g.
postgresql+asyncpg://postgres:postgres@localhost:55432/ledger_test. Every table is dropped first.
"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import test_ledger_migration_pg as snapshot
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.ledger import __main__ as cli
from src.ledger.chain import GENESIS_HASH
from src.ledger.verify import verify_database, verify_jsonl
from src.services import db, ledger
from src.strategy import registry
from src.strategy.base import SignalIntent
from src.strategy.macd import MacdSignal

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]

H2 = 2 * 60 * 60 * 1000
ALL_TABLES = (
    "macd_signals, profitloss_history, watchlist_symbols, trade_journal, cron_runs, strategy_versions, strategies, "
    "research_trials, signal_events, ledger_heads, signal_candles, profitloss_adjustments"
)
EVENTS_SQL = "SELECT id, signal_id, event_type, hash FROM signal_events ORDER BY id"


async def _execute(url: str, *statements: str, replica: bool = False) -> list:
    """Run statements in one transaction; `replica` switches triggers off for it, as only a superuser
    can, to stand in for someone tampering with the tables directly."""
    engine = create_async_engine(url)
    rows: list = []
    try:
        async with engine.begin() as conn:
            if replica:
                await conn.execute(text("SET LOCAL session_replication_role = replica"))
            for sql in statements:
                res = await conn.execute(text(sql))
                rows = res.all() if res.returns_rows else []
    finally:
        await engine.dispose()
    return rows


async def _open(monkeypatch, url: str) -> None:
    monkeypatch.setattr(db, "DATABASE_URL", url)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_maker", None)
    assert await db.init_db(max_retries=1)


async def _reboot(monkeypatch, url: str) -> None:
    await db.close_db()
    await _open(monkeypatch, url)


async def _verify(url: str) -> dict:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await verify_database(conn)
    finally:
        await engine.dispose()


@pytest.fixture
def url():
    return os.environ["TEST_DATABASE_URL"]


@pytest.fixture
async def empty_db(url, monkeypatch):
    await _execute(url, f"DROP TABLE IF EXISTS {ALL_TABLES}")
    await _open(monkeypatch, url)
    ledger.reset_caches()
    yield url
    await db.close_db()


# --- genesis --------------------------------------------------------------------------------------


async def test_genesis_restates_the_production_snapshot_once(url, monkeypatch):
    await _execute(url, f"DROP TABLE IF EXISTS {ALL_TABLES}")
    await snapshot._seed(url)
    await _open(monkeypatch, url)
    try:
        events = await _execute(url, EVENTS_SQL)
        # One restated snapshot per signal, plus excluded for the four legacy rows, in signal order
        assert [(sid, kind) for _, sid, kind, _ in events][:3] == [(1, "restated"), (1, "excluded"), (2, "restated")]
        assert sum(kind == "restated" for *_, kind, _ in events) == 15
        assert sorted(sid for _, sid, kind, _ in events if kind == "excluded") == [1, 2, 3, 4]

        payloads = dict(await _execute(
            url, "SELECT signal_id, payload::text FROM signal_events WHERE event_type = 'restated'"
        ))
        five = json.loads(payloads[5])
        net_r = (await _execute(url, "SELECT net_r FROM profitloss_history WHERE signal_id = 5"))[0][0]
        assert five["genesis"] is True and five["trade"]["net_r"] == net_r
        assert five["trade"]["cost_model_version"] == "cm1" and five["restatement"]["booked_r_multiple"] == -1.06
        assert json.loads(payloads[4])["signal"]["strategy_version"] == "legacy"

        report = await _verify(url)
        assert report["ok"], report
        assert (report["chain"]["events_checked"], report["rows"]["checked"]) == (19, 15)

        # A reboot writes nothing. Genesis is one-time: a row put in the table afterwards, outside the
        # app, is never hashed into the chain by a restart; verification keeps reporting it and the
        # statistics leave it out, even when it looks like an eligible winning trade
        await _reboot(monkeypatch, url)
        assert await _execute(url, EVENTS_SQL) == events
        perf_before = await db.get_performance()
        [(fake_id,)] = await _execute(
            url,
            "INSERT INTO macd_signals (symbol, side, timeframe, reason, cross_candle_ms, cross_time, cross_price, "
            "close_price, entry_price, stop_loss, tp1, macd, signal, histogram, win_status, source, entry_basis, "
            "strategy_key, strategy_version) VALUES ('SUIUSDT', 'LONG', '2h', 'r', 1792000000000, 't', 1, 1, 1.01, "
            "0.99, 1.04, 0, 0, 0, 'TP1_HIT', 'cron', 'next_open', 'macd_cross', '2.2.0') RETURNING id",
        )
        await _execute(
            url,
            "INSERT INTO profitloss_history (signal_id, symbol, timeframe, direction, entry_price, stop_loss, tp1, "
            "exit_price, result, pnl_percent, risk_reward_ratio, cross_time, exit_time, r_multiple, gross_r, cost_r, "
            f"net_r, cost_model_version, booked_by) VALUES ({fake_id}, 'SUIUSDT', '2h', 'LONG', 1.01, 0.99, 1.04, "
            "1.04, 'PROFIT', 3, 1.5, 't', '2026-10-15T00:00:00Z', 1.45, 1.5, 0.05, 1.45, 'cm1', 'evaluator')",
        )
        await _reboot(monkeypatch, url)
        assert await _execute(url, EVENTS_SQL) == events
        report = await _verify(url)
        assert not report["ok"] and report["rows"]["without_events"] == [fake_id]
        perf = await db.get_performance()
        assert (perf["n_closed"], perf["total_net_r"]) == (perf_before["n_closed"], perf_before["total_net_r"])
        assert perf["excluded"]["outside_ledger"] == 1
    finally:
        await db.close_db()


# --- append-only ----------------------------------------------------------------------------------


async def _lifecycle(url: str) -> dict:
    """Store a live signal, close it through the evaluator, alert on it and publish today's head."""
    strategy = registry.get("macd_cross@2.2.0")
    cross_ms = 1_760_140_800_000
    sig = MacdSignal(
        symbol="BTCUSDT", side="LONG", reason="MACD cross up", cross_candle_ms=cross_ms,
        cross_time="2025-10-11T00:00:00Z", cross_price=100.0, entry_price=100.5, stop_loss=99.0, tp1=102.75,
        risk_reward_ratio=1.5, macd=0.30000000000000004, signal=1e-07, histogram=2.0, close=100.4, bars_ago=0,
    )
    intent = SignalIntent.from_signal(sig, strategy)
    intent.input_candles_sha256 = "c" * 64
    intent.input_candles = {"decision_ms": cross_ms + H2, "timeframes": {"2h": {"count": 498}}}
    detected_at = datetime.now(UTC)
    saved = await db.save_macd_signal(intent, source="cron", detected_at=detected_at)
    assert saved["inserted"] is True and saved["input_candles_sha256"] == "c" * 64
    # Saving the same cross again writes neither a row nor an event
    assert (await db.save_macd_signal(intent, source="cron"))["inserted"] is False

    klines = [[cross_ms + i * H2, "100.4", "100.9", "99.8", "100.5", "10"] for i in range(5)]
    klines[2] = [cross_ms + 2 * H2, "100.5", "101.0", "98.5", "99.2", "12"]  # stop on the second trade candle
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        return klines

    result = await db.evaluate_pending_signals(SimpleNamespace(klines=fetch))
    assert result["loss_count"] == 1
    assert await db.record_alert_events([saved["id"]], "outcome", "<b>BTCUSDT Long</b> stopped out")
    head = await db.record_daily_ledger_head()
    assert head is not None and await db.record_daily_ledger_head() is None
    return {"id": saved["id"], "klines": klines, "head": head}


async def test_app_writes_a_verifiable_chain_for_a_signal_lifecycle(empty_db):
    url = empty_db
    life = await _lifecycle(url)
    sid = life["id"]

    events = await ledger.get_signal_events(sid)
    assert [e["event_type"] for e in events] == ["detected", "resolved", "alert_sent"]
    assert events[0]["prev_hash"] == GENESIS_HASH
    detected, resolved, alert = (e["payload"] for e in events)
    assert detected["params_hash"] == registry.get("macd_cross@2.2.0").params_hash
    assert detected["input_candles_sha256"] == "c" * 64
    # JSONB gives the numbers back so that the hash still checks out
    assert (detected["macd"], detected["signal_line"], detected["histogram"]) == (0.30000000000000004, 1e-07, 2)
    trade = (await _execute(url, f"SELECT result, net_r, gross_r, cost_r FROM profitloss_history WHERE signal_id = {sid}"))
    assert resolved["result"] == "LOSS" and resolved["net_r"] == trade[0][1] and resolved["cost_model_version"] == "cm1"
    assert alert["kind"] == "outcome" and alert["channel"] == "telegram"

    # The window the trade was settled on is persisted, hashed into the resolved event, and served
    window = await ledger.get_signal_candle_window(sid)
    assert window["persisted"] and window["sha256"] == resolved["candles"]["sha256"]
    assert [c["time"] for c in window["candles"]] == [k[0] // 1000 for k in life["klines"]]
    assert window["candles"][2]["low"] == 98.5

    head = await ledger.get_ledger_head()
    assert head["head"]["event_id"] == events[-1]["id"] and head["head"]["hash"] == events[-1]["hash"]
    assert head["daily"][0]["head_hash"] == life["head"]["head_hash"]

    report = await ledger.verify_ledger()
    assert report["ok"], report
    assert report["candles"]["checked"] == 1 and report["heads"]["checked"] == 1

    # The export verifies offline, and so does the database through the CLI
    body = await ledger.export_jsonl()
    exported = verify_jsonl(body.splitlines(), expected_heads=[life["head"]["head_hash"]])
    assert exported["ok"], exported
    assert await asyncio.to_thread(cli.main, ["verify", "--database-url", url]) == 0
    rows = await ledger.export_rows()
    [row] = rows
    assert (row["counted"], row["exclusion_reason"], row["event_count"]) == (True, None, 3)
    assert row["last_event_hash"] == events[-1]["hash"] and row["candles_sha256"] == window["sha256"]
    assert row["net_r"] == trade[0][1]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE signal_events SET payload = '{}'::jsonb WHERE id = 1",
        "DELETE FROM signal_events WHERE id = 1",
        "TRUNCATE signal_events",
        "UPDATE signal_candles SET candles = '[]'::jsonb",
        "DELETE FROM signal_candles",
        "UPDATE ledger_heads SET head_hash = 'x'",
        "DELETE FROM ledger_heads",
        "TRUNCATE ledger_heads",
    ],
)
async def test_ledger_tables_refuse_update_delete_and_truncate(empty_db, statement):
    await _lifecycle(empty_db)
    before = await _execute(empty_db, EVENTS_SQL)
    with pytest.raises(Exception, match="append-only"):
        await _execute(empty_db, statement)
    assert await _execute(empty_db, EVENTS_SQL) == before
    assert (await _verify(empty_db))["ok"]


async def test_tables_from_create_all_match_the_migration_ddl(empty_db):
    """create_all makes the tables on a fresh database, so the models carry the DDL's defaults, and
    signal_candles.signal_id (the signal's id) has no sequence of its own."""
    defaults = dict(await _execute(
        empty_db,
        "SELECT table_name || '.' || column_name, column_default FROM information_schema.columns WHERE "
        "(table_name, column_name) IN (('profitloss_adjustments', 'timeframe'), ('research_trials', 'purpose'), "
        "('strategies', 'description'), ('strategy_versions', 'notes'), ('strategy_versions', 'frozen'), "
        "('signal_candles', 'signal_id'))",
    ))
    assert defaults["profitloss_adjustments.timeframe"].startswith("'2h'")
    assert defaults["research_trials.purpose"].startswith("'research'")
    assert defaults["strategies.description"].startswith("''")
    assert defaults["strategy_versions.notes"].startswith("''")
    assert defaults["strategy_versions.frozen"] == "false"
    assert defaults["signal_candles.signal_id"] is None
    assert not await _execute(empty_db, "SELECT 1 FROM pg_class WHERE relname = 'signal_candles_signal_id_seq'")


async def test_boot_repairs_tables_an_earlier_build_created(empty_db, monkeypatch):
    """A database an earlier Phase 1 build set up got a sequence on signal_candles.signal_id and no
    server defaults; the boot migrations bring it in line."""
    await _execute(
        empty_db,
        "DROP TABLE signal_candles",
        "CREATE TABLE signal_candles (signal_id SERIAL PRIMARY KEY, symbol VARCHAR(20) NOT NULL, "
        "timeframe VARCHAR(10) NOT NULL, interval_ms BIGINT NOT NULL, start_ms BIGINT NOT NULL, end_ms BIGINT NOT NULL, "
        "candle_count INTEGER NOT NULL, resolved_at_ms BIGINT NOT NULL, candles JSONB NOT NULL, "
        "sha256 VARCHAR(64) NOT NULL, created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW())",
        "ALTER TABLE research_trials ALTER COLUMN purpose DROP DEFAULT",
    )
    await _reboot(monkeypatch, empty_db)
    assert not await _execute(empty_db, "SELECT 1 FROM pg_class WHERE relname = 'signal_candles_signal_id_seq'")
    [(purpose,)] = await _execute(
        empty_db,
        "SELECT column_default FROM information_schema.columns WHERE table_name = 'research_trials' "
        "AND column_name = 'purpose'",
    )
    assert purpose.startswith("'research'")


async def test_reboot_keeps_the_triggers_and_the_chain(empty_db, monkeypatch):
    await _lifecycle(empty_db)
    before = await _execute(empty_db, EVENTS_SQL)
    await _reboot(monkeypatch, empty_db)
    await _execute(empty_db, *db.LEDGER_MIGRATIONS)
    assert await _execute(empty_db, EVENTS_SQL) == before
    with pytest.raises(Exception, match="append-only"):
        await _execute(empty_db, "DELETE FROM signal_events")


# --- tamper detection -----------------------------------------------------------------------------


async def test_a_payload_changed_behind_the_trigger_fails_verification_at_that_event(empty_db, capsys):
    await _lifecycle(empty_db)
    await _execute(
        empty_db,
        "UPDATE signal_events SET payload = jsonb_set(payload, '{net_r}', '1.5') WHERE event_type = 'resolved'",
        replica=True,
    )
    resolved_id = (await _execute(empty_db, "SELECT id FROM signal_events WHERE event_type = 'resolved'"))[0][0]
    report = await _verify(empty_db)
    assert not report["ok"] and report["first_bad_event"] == resolved_id
    assert report["chain"]["reason"] == "hash does not match the event's contents"
    # The published head names an event past the break: it cannot be checked, which is not a mismatch,
    # and the rows are not compared with unverified snapshots
    assert report["heads"]["unverifiable"] >= 1 and not report["heads"]["mismatched"]
    assert report["rows"] == {"ok": None, "skipped": f"chain broken at event {resolved_id}"}
    assert await asyncio.to_thread(cli.main, ["verify", "--database-url", empty_db]) == 1
    assert json.loads(capsys.readouterr().out)["first_bad_event"] == resolved_id


async def test_rewritten_candles_and_drifted_rows_are_reported(empty_db):
    life = await _lifecycle(empty_db)
    sid = life["id"]
    await _execute(
        empty_db,
        f"UPDATE signal_candles SET candles = jsonb_set(candles, '{{2,3}}', '\"97\"') WHERE signal_id = {sid}",
        replica=True,
    )
    await _execute(empty_db, f"UPDATE macd_signals SET win_status = 'TP1_HIT' WHERE id = {sid}")
    await _execute(empty_db, f"UPDATE profitloss_history SET net_r = 1.4 WHERE signal_id = {sid}")
    report = await _verify(empty_db)
    assert report["chain"]["ok"] and not report["ok"]
    assert report["candles"]["mismatched"] == [{"signal_id": sid, "reason": "candles do not hash to the stored sha256"}]
    [drift] = report["rows"]["drifted"]
    assert drift["signal_id"] == sid and drift["fields"] == ["win_status", "trade.net_r"]


async def test_a_cached_verification_is_never_served_for_a_changed_ledger(empty_db):
    """The public endpoint caches its result, keyed on the state of every ledger table, so an edit
    behind the trigger (which does not move the chain head) or a deleted row shows up at once."""
    await _lifecycle(empty_db)
    first = await ledger.verify_ledger()
    assert first["ok"] and first["cached_for_seconds"] == 0
    assert (await ledger.verify_ledger())["cached_for_seconds"] >= 0
    assert (await ledger.verify_ledger())["verified_at"] == first["verified_at"]
    await _execute(
        empty_db,
        "UPDATE signal_events SET payload = jsonb_set(payload, '{net_r}', '1.5') WHERE event_type = 'resolved'",
        replica=True,
    )
    again = await ledger.verify_ledger()
    assert not again["ok"] and again["cached_for_seconds"] == 0
    rows_before = await ledger.export_rows()
    await _execute(empty_db, "DELETE FROM profitloss_history")
    assert await ledger.export_rows() != rows_before


async def test_deleted_trade_and_signal_rows_are_reported(empty_db):
    """macd_signals and profitloss_history are mutable: deleting a booked trade, or the signal itself,
    must not leave verification saying ok."""
    sid = (await _lifecycle(empty_db))["id"]
    await _execute(empty_db, f"DELETE FROM profitloss_history WHERE signal_id = {sid}")
    report = await _verify(empty_db)
    assert report["chain"]["ok"] and not report["ok"]
    assert report["rows"]["drifted"][0]["fields"] == ["trade.missing"]
    await _execute(empty_db, f"DELETE FROM macd_signals WHERE id = {sid}")
    report = await _verify(empty_db)
    assert not report["ok"] and report["rows"]["missing_count"] == 1
    assert report["rows"]["missing"][0]["signal_id"] == sid


async def test_a_new_day_publishes_a_new_head(empty_db):
    await _lifecycle(empty_db)
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    assert await db.record_alert_events([1], "signal", "again")
    head = await db.record_daily_ledger_head(tomorrow)
    assert head["date"] == tomorrow.date().isoformat() and head["event_count"] == 4
    daily = (await ledger.get_ledger_head())["daily"]
    assert [d["event_count"] for d in daily] == [4, 3]
    assert (await _verify(empty_db))["heads"] == {"checked": 2, "ok": True, "mismatched": []}


async def test_hand_entered_trades_stay_out_of_the_ledger(empty_db):
    await _lifecycle(empty_db)
    item = await db.record_profit_loss("ETHUSDT", "long", 2500.0, 2450.0, 2575.0, 2575.0, "profit")
    assert item["adjustment"] is True
    assert len(await ledger.export_rows()) == 1
    assert (await db.get_profitloss_history())[1] == 1
    assert (await _verify(empty_db))["ok"]


async def test_a_booking_backfill_on_a_signal_with_events_is_restated(empty_db, monkeypatch):
    strategy = registry.get("macd_cross@2.2.0")
    sig = MacdSignal(
        symbol="ETHUSDT", side="SHORT", reason="MACD cross down", cross_candle_ms=1_760_140_800_000,
        cross_time="2025-10-11T00:00:00Z", cross_price=2500.0, entry_price=2499.0, stop_loss=2530.0, tp1=2452.5,
        risk_reward_ratio=1.5, macd=-1.0, signal=-0.5, histogram=-0.5, close=2499.5, bars_ago=0,
    )
    saved = await db.save_macd_signal(SignalIntent.from_signal(sig, strategy), source="cron")
    sid = saved["id"]
    # Closed outside the evaluator: the row drifts from its detected event
    await _execute(empty_db, f"UPDATE macd_signals SET win_status = 'TP1_HIT' WHERE id = {sid}")
    assert (await _verify(empty_db))["rows"]["drifted"][0]["fields"] == ["win_status"]

    # The boot backfill books it and records that as a restatement, so the ledger says what happened
    await _reboot(monkeypatch, empty_db)
    events = await ledger.get_signal_events(sid)
    assert [e["event_type"] for e in events] == ["detected", "restated"]
    assert events[1]["payload"]["reason"] == "resolved_signal_backfill"
    assert events[1]["payload"]["trade"]["result"] == "PROFIT"
    assert (await _verify(empty_db))["ok"]
