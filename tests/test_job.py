import logging

import pandas as pd
import pytest

from src.services import cron_scheduler as cron_module
from src.services.cron_scheduler import TIMEFRAME_MS, run_job

pytestmark = pytest.mark.unit

START = 1_792_540_800_000
CLOSES = [100.0 + i for i in range(40)] + [89.0]  # steady climb, then a crash that crosses RSI 28
LAST_OPEN = START + (len(CLOSES) - 1) * TIMEFRAME_MS


def _frame() -> pd.DataFrame:
    opens = [START + i * TIMEFRAME_MS for i in range(len(CLOSES))]
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(opens, unit="ms"),
            "high": [c + 1 for c in CLOSES],
            "low": [c - 1 for c in CLOSES],
            "close": CLOSES,
            "close_time": [o + TIMEFRAME_MS - 1 for o in opens],
        }
    )


EVALUATED = {
    "evaluated": 2,
    "filled": 1,
    "tp": 0,
    "sl": 0,
    "expired": 0,
    "not_filled": 1,
    "failed": 0,
    "transitions": [{"id": 9, "status": "NOT_FILLED"}],
}


@pytest.fixture
def job(monkeypatch):
    state = {
        "watchlist": ["BTCUSDT"],
        "now": LAST_OPEN + TIMEFRAME_MS + 15_000,
        "missing": set(),
        "fetched": [],
        "inserted": [],
        "new_rows": None,
        "claimed": None,
        "evaluate_error": None,
        "delivered": True,
        "evaluated": 0,
        "notified": [],
        "released": [],
    }

    async def fake_db_connected():
        return True

    async def fake_watchlist(active_only=False):
        return [{"symbol": s} for s in state["watchlist"]]

    def fake_fetch(client, sym, timeframe, limit):
        state["fetched"].append(sym)
        return None if sym in state["missing"] else _frame()

    async def fake_insert(signals):
        state["inserted"].append(signals)
        rows = [{"id": i, "symbol": s.symbol} for i, s in enumerate(signals, 1)]
        return rows if state["new_rows"] is None else state["new_rows"]

    async def fake_claim(since_ms):
        state["claimed_since"] = since_ms
        return state["claimed"] or []

    async def fake_release(signal_ids):
        state["released"].append(signal_ids)

    async def fake_evaluate(client):
        state["evaluated"] += 1
        if state["evaluate_error"]:
            raise state["evaluate_error"]
        return EVALUATED

    async def fake_notify(new_rows, transitions):
        state["notified"].append((new_rows, transitions))
        return state["delivered"]

    monkeypatch.setattr(cron_module, "is_db_connected", fake_db_connected)
    monkeypatch.setattr(cron_module, "get_watchlist", fake_watchlist)
    monkeypatch.setattr(cron_module, "fetch_klines", fake_fetch)
    monkeypatch.setattr(cron_module, "insert_signals", fake_insert)
    monkeypatch.setattr(cron_module, "claim_unalerted_signals", fake_claim)
    monkeypatch.setattr(cron_module, "release_signal_alerts", fake_release)
    monkeypatch.setattr(cron_module, "evaluate_active", fake_evaluate)
    monkeypatch.setattr(cron_module, "notify_run", fake_notify)
    monkeypatch.setattr(cron_module, "now_ms", lambda: state["now"])
    return state


async def test_closed_cross_is_stored_with_the_candle_low_as_entry(job):
    res = await run_job(object())

    [hits] = job["inserted"]
    assert [(s.symbol, s.side, s.signal_candle_ms) for s in hits] == [("BTCUSDT", "LONG", LAST_OPEN)]
    assert hits[0].entry_price == 88.0
    assert hits[0].bars_ago == 0
    assert res["new_signals"] == 1
    assert res["symbols_scanned"] == 1
    assert res["not_filled"] == 1


async def test_forming_candle_is_dropped_by_close_time(job):
    job["now"] = LAST_OPEN + TIMEFRAME_MS - 1  # last candle's close_time is not in the past yet

    res = await run_job(object())

    assert job["inserted"] == [[]]
    assert res["new_signals"] == 0
    assert job["evaluated"] == 1


async def test_every_unalerted_row_is_alerted(job):
    job["watchlist"] = ["BTCUSDT", "ETHUSDT"]
    job["new_rows"] = [{"id": 5, "symbol": "ETHUSDT"}]
    job["claimed"] = [{"id": 3, "symbol": "SOLUSDT"}, {"id": 5, "symbol": "ETHUSDT"}]  # 3: stored by a run that died

    res = await run_job(object())

    assert [s.symbol for s in job["inserted"][0]] == ["BTCUSDT", "ETHUSDT"]
    assert job["claimed_since"] == job["now"] - (cron_module.CRON_LOOKBACK_CANDLES + 1) * TIMEFRAME_MS
    assert job["notified"] == [(job["claimed"], EVALUATED["transitions"])]
    assert job["released"] == []
    assert res["new_signals"] == 1


async def test_undelivered_alert_is_released_for_the_next_run(job):
    job["claimed"] = [{"id": 3, "symbol": "BTCUSDT"}]
    job["delivered"] = False

    await run_job(object())

    assert job["released"] == [[3]]


async def test_failed_evaluation_still_alerts_stored_signals(job):
    job["claimed"] = [{"id": 1, "symbol": "BTCUSDT"}]
    job["evaluate_error"] = RuntimeError("db blip")

    with pytest.raises(RuntimeError, match="db blip"):
        await run_job(object())

    assert job["notified"] == [(job["claimed"], [])]


async def test_symbols_whose_candles_failed_are_reported(job):
    job["watchlist"] = ["BTCUSDT", "ETHUSDT"]
    job["missing"] = {"ETHUSDT"}

    res = await run_job(object())

    assert res["failed_symbols"] == ["ETHUSDT"]
    assert [s.symbol for s in job["inserted"][0]] == ["BTCUSDT"]


async def test_symbols_are_capped(job, monkeypatch):
    monkeypatch.setattr(cron_module, "MAX_SYMBOLS", 1)
    job["watchlist"] = ["BTCUSDT", "ETHUSDT"]

    res = await run_job(object())

    assert job["fetched"] == ["BTCUSDT"]
    assert res["symbols_scanned"] == 1


async def test_database_not_ready_fails_before_scanning(job, monkeypatch):
    async def db_down():
        return False

    monkeypatch.setattr(cron_module, "is_db_connected", db_down)

    with pytest.raises(RuntimeError, match="Database not ready"):
        await run_job(object())
    assert job["fetched"] == []
    assert job["evaluated"] == 0


async def test_empty_watchlist_warns_and_still_evaluates_open_trades(job, caplog):
    job["watchlist"] = []

    with caplog.at_level(logging.WARNING, logger=cron_module.__name__):
        res = await run_job(object())

    assert "Watchlist is empty" in caplog.text
    assert job["inserted"] == [[]]
    assert job["evaluated"] == 1
    assert res["symbols_scanned"] == 0
