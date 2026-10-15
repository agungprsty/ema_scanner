"""Strategy registry tables, the widened signal uniqueness and research_trials against a real PostgreSQL.

Opt-in: set TEST_DATABASE_URL to an empty throwaway database, e.g.
postgresql+asyncpg://postgres:postgres@localhost:55432/ledger_test. Every table is dropped first.
"""

import asyncio
import logging
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.backtest.__main__ import main as backtest_main
from src.services import db
from src.strategy import registry
from src.strategy.base import SignalIntent
from src.strategy.macd import MacdSignal

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]

ALL_TABLES = (
    "macd_signals, profitloss_history, watchlist_symbols, trade_journal, cron_runs, "
    "strategy_versions, strategies, research_trials, signal_events, ledger_heads, signal_candles, profitloss_adjustments"
)
MACD = registry.get("macd_cross@2.2.0")
SHADOW = registry.get("macd_cross_ema50d_oppexit@1.0.0")
KLINES_DIR = Path(__file__).parent / "fixtures" / "klines"


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


@pytest.fixture
async def fresh(monkeypatch):
    url = os.environ["TEST_DATABASE_URL"]
    await _execute(url, f"DROP TABLE IF EXISTS {ALL_TABLES}")
    await _boot(monkeypatch, url)
    yield url
    await db.close_db()


VERSION_ROW = "SELECT key, version, params_hash, code_hash, status, frozen, frozen_at, notes FROM strategy_versions"
LIVE_ONLY = " WHERE key = 'macd_cross'"


async def test_boot_registers_the_live_strategy_frozen_from_the_lock(fresh, monkeypatch):
    lock = registry.load_lock()["macd_cross@2.2.0"]
    [row] = await _execute(fresh, VERSION_ROW + LIVE_ONLY)
    assert row[:6] == ("macd_cross", "2.2.0", lock["params_hash"], lock["code_hash"], "live", True)
    assert row[6].isoformat().startswith(lock["frozen_at"].rstrip("Z"))
    assert await _execute(fresh, "SELECT key, name FROM strategies ORDER BY key") == [
        ("macd_cross", MACD.name), (SHADOW.key, SHADOW.name)
    ]
    [params] = await _execute(fresh, "SELECT params_json FROM strategy_versions" + LIVE_ONLY)
    assert params[0] == MACD.params_dict

    # The pre-registered shadow version is frozen from the lock too
    shadow_lock = registry.load_lock()[SHADOW.ref]
    [shadow_row] = await _execute(fresh, VERSION_ROW + f" WHERE key = '{SHADOW.key}'")
    assert shadow_row[:6] == (SHADOW.key, "1.0.0", shadow_lock["params_hash"], shadow_lock["code_hash"], "shadow", True)

    stored = await db.get_strategy_versions()
    assert registry.emitting_strategies(stored) == ([MACD, SHADOW], {})

    # Rebooting changes nothing
    await db.close_db()
    await _boot(monkeypatch, fresh)
    assert await _execute(fresh, VERSION_ROW + LIVE_ONLY) == [row]
    assert await _execute(fresh, VERSION_ROW + f" WHERE key = '{SHADOW.key}'") == [shadow_row]


async def test_a_frozen_row_is_never_rewritten_and_drift_is_refused(fresh, monkeypatch, caplog):
    await _execute(fresh, f"UPDATE strategy_versions SET code_hash = '{'0' * 64}'" + LIVE_ONLY)
    await db.close_db()
    with caplog.at_level(logging.ERROR):
        await _boot(monkeypatch, fresh)
    [row] = await _execute(fresh, "SELECT code_hash FROM strategy_versions" + LIVE_ONLY)
    assert row[0] == "0" * 64
    assert "not a recorded rehash" in caplog.text

    allowed, refused = registry.emitting_strategies(await db.get_strategy_versions())
    assert allowed == [SHADOW] and refused["macd_cross@2.2.0"].startswith("code_hash")


async def test_a_recorded_rehash_moves_the_frozen_code_hash(fresh):
    await _execute(fresh, f"UPDATE strategy_versions SET code_hash = '{'a' * 64}'" + LIVE_ONLY)
    lock = registry.load_lock()
    lock["macd_cross@2.2.0"]["code_hash_history"] = [{"code_hash": "a" * 64, "reason": "rename a helper"}]
    async with db._session_maker() as session:
        await db.sync_strategy_registry(session, lock=lock)
    [(code_hash, notes)] = await _execute(fresh, "SELECT code_hash, notes FROM strategy_versions" + LIVE_ONLY)
    assert code_hash == MACD.code_hash and "rename a helper" in notes


async def test_an_unfrozen_row_freezes_once_the_lock_has_it(fresh):
    await _execute(fresh, "UPDATE strategy_versions SET frozen = FALSE, frozen_at = NULL, code_hash = 'draft'" + LIVE_ONLY)
    assert "not frozen" in registry.emitting_strategies(await db.get_strategy_versions())[1]["macd_cross@2.2.0"]
    async with db._session_maker() as session:
        await db.sync_strategy_registry(session)
    [(frozen, code_hash, notes)] = await _execute(
        fresh, "SELECT frozen, code_hash, notes FROM strategy_versions" + LIVE_ONLY
    )
    assert frozen and code_hash == MACD.code_hash and "frozen" in notes


def _signal(**overrides) -> MacdSignal:
    values = dict(
        symbol="BTCUSDT", side="LONG", reason="macd_golden_cross", cross_candle_ms=1_760_000_000_000,
        cross_time="2025-10-09T08:53:20Z", cross_price=100.0, entry_price=100.5, stop_loss=95.0, tp1=108.75,
        risk_reward_ratio=1.5, macd=1.0, signal=0.5, histogram=0.5, close=101.0, bars_ago=0,
    )
    values.update(overrides)
    return MacdSignal(**values)


async def test_live_and_shadow_strategies_each_record_the_same_cross(fresh):
    live = await db.save_macd_signal(_signal(), source="cron")
    shadow = SignalIntent(**_signal().to_dict(), strategy_key="shadow_probe", strategy_version="1.0.0")
    first = await db.save_macd_signal(shadow, source="cron")
    again = await db.save_macd_signal(shadow, source="cron")
    assert live["inserted"] and first["inserted"] and again["inserted"] is False
    assert again["id"] == first["id"] != live["id"]
    assert (first["strategy_key"], first["strategy_version"]) == ("shadow_probe", "1.0.0")
    assert (await db.save_macd_signal(_signal(), source="cron"))["id"] == live["id"]

    indexes = {r[0] for r in await _execute(fresh, "SELECT indexname FROM pg_indexes WHERE tablename = 'macd_signals'")}
    assert "uq_macd_signals_strategy_candle" in indexes and db.LEGACY_SIGNAL_UNIQUE_INDEX not in indexes

    # The shadow row stays out of the headline track record and its counts
    perf = await db.get_performance()
    assert perf["open_count"] == 1 and perf["excluded"]["other_version"] == 1

    # Rebooting (dedup migrations included) keeps both rows
    await db.close_db()
    await _boot_again(fresh)
    assert len(await _execute(fresh, "SELECT id FROM macd_signals")) == 2


async def _boot_again(url):
    db.DATABASE_URL = url
    db._engine = None
    db._session_maker = None
    assert await db.init_db(max_retries=1)


async def test_the_old_one_row_per_cross_index_is_replaced(fresh, monkeypatch):
    await db.save_macd_signal(_signal(), source="cron")
    # As production has it before this migration
    await _execute(
        fresh,
        "DROP INDEX uq_macd_signals_strategy_candle",
        f"CREATE UNIQUE INDEX {db.LEGACY_SIGNAL_UNIQUE_INDEX} ON macd_signals (symbol, side, timeframe, cross_candle_ms)",
    )
    await db.close_db()
    await _boot(monkeypatch, fresh)
    indexes = {r[0] for r in await _execute(fresh, "SELECT indexname FROM pg_indexes WHERE tablename = 'macd_signals'")}
    assert db.LEGACY_SIGNAL_UNIQUE_INDEX not in indexes and "uq_macd_signals_strategy_candle" in indexes
    assert len(await _execute(fresh, "SELECT id FROM macd_signals")) == 1


def test_harness_records_a_research_trial(monkeypatch, tmp_path):
    """The CLI runs its own event loop, so this test is synchronous."""
    url = os.environ["TEST_DATABASE_URL"]
    asyncio.run(_execute(url, "DROP TABLE IF EXISTS research_trials"))
    monkeypatch.setenv("DATABASE_URL", url.replace("postgresql+asyncpg://", "postgresql://"))
    log = tmp_path / "trials.jsonl"
    args = ["--data", str(KLINES_DIR), "--symbols", "BTCUSDT", "--oos-start", "2025-02-01", "--trials-log", str(log)]
    assert backtest_main([*args, "--record-trial", "--purpose", "gate"]) == 0
    [row] = asyncio.run(_execute(
        url,
        "SELECT strategy_key, version, params_hash, date_from, date_to, purpose, "
        "metrics_json -> 'scenarios' -> 'prod' ->> 'n', dataset_desc FROM research_trials",
    ))
    assert row[:3] == ("macd_cross", "2.2.0", MACD.params_hash)
    assert (str(row[3]), str(row[4]), row[5], row[6]) == ("2024-10-01", "2025-03-16", "gate", "114")
    assert "BTCUSDT" in row[7]
    assert len(log.read_text().splitlines()) == 1


async def test_research_trials_table_is_created_on_its_own(fresh):
    await _execute(fresh, "DROP TABLE research_trials")
    trial_id = await db.insert_research_trial(
        {"strategy_key": "k", "version": "1", "params_hash": "h", "dataset_desc": "d", "date_from": "2025-01-01T00:00:00",
         "date_to": None, "metrics": {"n": 0}, "purpose": "adhoc"},
        fresh,
    )
    assert trial_id == 1


async def test_shadow_trade_is_stored_and_booked_under_its_exit_label(fresh):
    """A macd_cross_ema50d_oppexit signal (no target) goes through save, the evaluator and the ledger
    columns of a real database, booked as the harness books it."""
    from types import SimpleNamespace

    from src.backtest.data import load_klines_csv, resample_klines
    from src.backtest.metrics import SCENARIOS, book_trades
    from src.backtest.replay import replay_strategy
    from src.strategy.macd_cross_ema50d_oppexit import MacdCrossEma50dOppExit, MacdCrossEma50dOppExitParams

    btc = load_klines_csv(KLINES_DIR / "BTCUSDT_2h.csv")
    small = MacdCrossEma50dOppExit(MacdCrossEma50dOppExitParams(trend_ema_bars=60))
    trades = replay_strategy(small, btc, "BTCUSDT", context={"1d": resample_klines(btc, "2h", "1d")}).trades
    trade = next(t for t in trades if t.result == "OPPOSITE_CROSS")
    intent = SignalIntent(**_signal(
        side=trade.side, cross_candle_ms=trade.cross_candle_ms, entry_price=trade.entry_price,
        stop_loss=trade.stop_loss, tp1=0.0, risk_reward_ratio=0.0,
    ).to_dict(), strategy_key=SHADOW.key, strategy_version=SHADOW.version)
    saved = await db.save_macd_signal(intent, source="cron")
    assert saved["inserted"] and saved["tp1"] == 0.0

    rows = [[int(r.open_time_ms), f"{r.open}", f"{r.high}", f"{r.low}", f"{r.close}", "1"] for r in btc.itertuples()]

    def klines(**kwargs):
        first = [r[0] for r in rows].index(kwargs["startTime"])
        return rows[first: first + kwargs["limit"]]

    result = await db.evaluate_pending_signals(SimpleNamespace(klines=klines))
    assert result["opposite_cross_count"] == 1 and result["closed"] == []
    [(status,)] = await _execute(fresh, "SELECT win_status FROM macd_signals")
    [(res, exit_price, net_r, booked_by)] = await _execute(
        fresh, "SELECT result, exit_price, net_r, booked_by FROM profitloss_history"
    )
    [expected] = book_trades([trade], SCENARIOS["prod"])
    assert (status, res, exit_price, booked_by) == ("OPPOSITE_CROSS", "OPPOSITE_CROSS", trade.exit_price, "evaluator")
    assert net_r == pytest.approx(expected.net_r, rel=1e-9)
    # A shadow trade never reaches the headline track record
    perf = await db.get_performance()
    assert perf["n_closed"] == 0 and perf["excluded"]["other_version"] == 1
