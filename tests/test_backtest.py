import json
from pathlib import Path

import numpy as np
import pytest

from src.backtest.__main__ import main as backtest_main
from src.backtest.data import load_klines_csv
from src.backtest.gate import baseline_from_report, compare_to_baseline
from src.backtest.metrics import (
    SCENARIOS,
    BookedTrade,
    book_trades,
    half_year,
    max_drawdown_r,
    summarize,
    weekly_block_ci,
)
from src.backtest.replay import PRODUCTION_WARMUP, BacktestTrade, replay_signals, replay_symbol
from src.backtest.run import date_to_ms, run_backtest
from src.config.settings import CRON_LOOKBACK_CANDLES, MACD_CANDLES, MACD_FAST, MACD_SIGNAL, MACD_SLOW
from src.strategy.macd import compute_macd, detect_macd_cross

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
KLINES_DIR = FIXTURES / "klines"
FIXTURE_BASELINE = FIXTURES / "backtest_baseline.json"
FIXTURE_OOS_START = "2025-02-01"
H2 = 2 * 60 * 60 * 1000


@pytest.fixture(scope="module")
def btc():
    return load_klines_csv(KLINES_DIR / "BTCUSDT_2h.csv")


def _key(sig):
    return (sig.cross_candle_ms, sig.side, sig.entry_price, sig.stop_loss, sig.tp1)


def test_replay_matches_the_live_scan_bar_by_bar(btc):
    """The harness emits exactly the signals the scheduled scan would, run after every close.

    The live scan sees the last MACD_CANDLES klines (the newest still forming) and re-checks
    CRON_LOOKBACK_CANDLES closed candles; the series is long enough that the window rolls
    for 200 candles, so the full-series MACD of the harness is checked against windowed MACD.
    """
    klines = btc.iloc[: MACD_CANDLES + 200].reset_index(drop=True)
    live = set()
    for forming in range(2, len(klines)):
        window = klines.iloc[max(0, forming + 1 - MACD_CANDLES) : forming + 1].reset_index(drop=True)
        df = compute_macd(window, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
        if df is None or len(df) < 3:
            continue
        sig = detect_macd_cross(df, "BTCUSDT", lookback_candles=CRON_LOOKBACK_CANDLES, include_current=False)
        if sig is not None:
            live.add(_key(sig))

    df = compute_macd(klines, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    replayed = [sig for _, sig in replay_signals(df, "BTCUSDT", warmup=0, dropped=len(klines) - len(df))]

    assert len(live) > 30
    assert len(replayed) == len({_key(s) for s in replayed})
    assert {_key(s) for s in replayed} == live
    assert all(s.bars_ago == 0 and s.entry_basis == "next_open" for s in replayed)


def test_replay_skips_crosses_without_production_history(btc):
    first = btc["open_time_ms"].iloc[PRODUCTION_WARMUP]
    trades = replay_symbol(btc, "BTCUSDT").trades
    assert trades and min(t.cross_candle_ms for t in trades) >= first
    assert len(replay_symbol(btc, "BTCUSDT", warmup=0).trades) > len(trades)


def test_replay_settles_trades_from_the_candle_after_the_cross(btc):
    replay = replay_symbol(btc, "BTCUSDT")
    for t in replay.trades:
        assert t.entry_time_ms == t.cross_candle_ms + H2
        assert t.entry_price == float(btc.loc[btc["open_time_ms"] == t.entry_time_ms, "open"].iloc[0])
        assert t.entry_time_ms <= t.exit_time_ms <= t.entry_time_ms + 48 * H2
    # The newest cross in the fixture sits too close to its end to settle
    assert replay.unresolved == 1


def test_replay_respects_the_date_window(btc):
    start, end = date_to_ms("2025-01-01"), date_to_ms("2025-02-01")
    trades = replay_symbol(btc, "BTCUSDT", start_ms=start, end_ms=end).trades
    assert trades and all(start <= t.cross_candle_ms < end for t in trades)


def test_fixture_reproduces_its_baseline_exactly():
    """The CI gate, offline: a strategy or cost change has to regenerate this baseline too."""
    report, _ = run_backtest(KLINES_DIR, ["BTCUSDT"], oos_start=FIXTURE_OOS_START)
    baseline = json.loads(FIXTURE_BASELINE.read_text())
    assert compare_to_baseline(report, baseline) == []
    assert report["signals"] == {"crosses": 115, "resolved": 114, "unresolved": 1}
    for scenario in SCENARIOS:
        s = report["scenarios"][scenario]
        assert s["outcomes"] == {"PROFIT": 42, "LOSS": 56, "EXPIRED": 16}
        assert s["in_sample"]["n"] + s["out_of_sample"]["n"] == s["n"] == 114


def test_costs_order_the_scenarios(btc):
    report, _ = run_backtest(KLINES_DIR, ["BTCUSDT"], oos_start=FIXTURE_OOS_START)
    s = report["scenarios"]
    assert s["gross"]["mean_net_r"] == pytest.approx(s["gross"]["mean_gross_r"])
    assert s["gross"]["mean_net_r"] > s["fee_only"]["mean_net_r"] > s["prod"]["mean_net_r"]
    assert s["prod"]["mean_gross_r"] == pytest.approx(s["gross"]["mean_gross_r"])
    lo, hi = s["prod"]["ci95_net_r"]
    assert lo < s["prod"]["mean_net_r"] < hi
    assert set(s["prod"]["by_side"]) == {"LONG", "SHORT"}
    assert sum(g["n"] for g in s["prod"]["by_half_year"].values()) == s["prod"]["n"]


def test_gate_reports_every_drifted_metric():
    report, _ = run_backtest(KLINES_DIR, ["BTCUSDT"], oos_start=FIXTURE_OOS_START)
    baseline = baseline_from_report(report)
    baseline["metrics"]["prod"]["mean_net_r"] += 0.0001
    baseline["params"]["risk_reward_ratio"] = 2.0
    baseline["dataset"]["sha256"]["BTCUSDT"] = "0" * 64
    differences = compare_to_baseline(report, baseline)
    assert len(differences) == 3
    assert any("metrics.prod.mean_net_r" in d for d in differences)
    assert any("params.risk_reward_ratio" in d for d in differences)
    assert any("sha256.BTCUSDT" in d for d in differences)


def test_cli_fails_on_baseline_mismatch_and_can_update_it(tmp_path, capsys):
    args = ["--data", str(KLINES_DIR), "--symbols", "BTCUSDT", "--oos-start", FIXTURE_OOS_START]
    out = tmp_path / "report.json"
    assert backtest_main([*args, "--out", str(out), "--baseline", str(FIXTURE_BASELINE)]) == 0
    assert json.loads(out.read_text())["scenarios"]["prod"]["n"] == 114

    stale = json.loads(FIXTURE_BASELINE.read_text())
    stale["metrics"]["gross"]["total_net_r"] = 0.0
    stale_path = tmp_path / "baseline.json"
    stale_path.write_text(json.dumps(stale))
    assert backtest_main([*args, "--baseline", str(stale_path)]) == 1
    assert "BACKTEST GATE FAILED" in capsys.readouterr().err

    assert backtest_main([*args, "--baseline", str(stale_path), "--update-baseline"]) == 0
    assert json.loads(stale_path.read_text()) == json.loads(FIXTURE_BASELINE.read_text())


def test_cli_rejects_missing_data(tmp_path):
    assert backtest_main(["--data", str(tmp_path)]) == 2
    assert backtest_main(["--data", str(KLINES_DIR), "--symbols", "ETHUSDT"]) == 2


def _trade(net_r, result="LOSS", exit_ms=0, cross_ms=0, symbol="BTCUSDT"):
    t = BacktestTrade(symbol, "LONG", cross_ms, cross_ms + H2, 100.0, 95.0, 107.5, result, 0.0, exit_ms)
    return BookedTrade(t, net_r, 0.0, net_r)


def test_max_drawdown_follows_exit_time_not_entry_time():
    booked = [_trade(1.0, exit_ms=3), _trade(-1.0, exit_ms=1), _trade(-1.0, exit_ms=2)]
    # By exit: -1, -1, +1 -> trough -2 from the 0 start
    assert max_drawdown_r(booked) == pytest.approx(2.0)
    assert max_drawdown_r([_trade(1.0, exit_ms=1), _trade(-0.5, exit_ms=2), _trade(2.0, exit_ms=3)]) == 0.5


def test_summary_counts_expiries_apart_from_wins():
    booked = [_trade(1.5, "PROFIT"), _trade(-1.0, "LOSS"), _trade(0.2, "EXPIRED"), _trade(-1.0, "LOSS")]
    s = summarize(booked)
    assert s["win_rate"] == 0.25 and s["expired_rate"] == 0.25
    assert s["profit_factor"] == pytest.approx(1.7 / 2.0)
    assert s["total_net_r"] == pytest.approx(-0.3)
    assert s["t_stat"] == pytest.approx(-0.075 / (np.std([1.5, -1.0, 0.2, -1.0], ddof=1) / 2))
    assert summarize([]) == {"n": 0, "outcomes": {"PROFIT": 0, "LOSS": 0, "EXPIRED": 0}}


def test_weekly_block_ci_is_deterministic_and_resamples_whole_weeks():
    week = 7 * 24 * 60 * 60 * 1000
    monday = date_to_ms("2025-01-06")
    net = np.array([1.0, 1.0, -1.0, -1.0, 0.5])
    cross = np.array([monday, monday + H2, monday + week, monday + week + H2, monday + 2 * week], dtype=np.int64)
    ci = weekly_block_ci(net, cross)
    assert ci == weekly_block_ci(net, cross)
    # Week means are +1, -1 and +0.5; no resample of whole weeks can leave that range
    assert -1.0 <= ci[0] < ci[1] <= 1.0
    assert weekly_block_ci(net[:2], cross[:2]) is None


def test_half_year_labels():
    assert half_year(date_to_ms("2025-06-30T23:59:59")) == "2025H1"
    assert half_year(date_to_ms("2025-07-01")) == "2025H2"


def test_book_trades_uses_the_ledger_cost_model():
    t = BacktestTrade("NEARUSDT", "LONG", 0, H2, 100.0, 95.0, 107.5, "LOSS", 95.0, 2 * H2)
    gross = book_trades([t], SCENARIOS["gross"])[0]
    prod = book_trades([t], SCENARIOS["prod"])[0]
    fee_only = book_trades([t], SCENARIOS["fee_only"])[0]
    assert gross.net_r == pytest.approx(-1.0) and gross.cost_r == 0.0
    # 0.1% fee on 5% risk is 0.02R; prod adds NEAR's 25 bps in and 37.5 bps (1.5x) out
    assert fee_only.net_r == pytest.approx(-1.02)
    assert prod.net_r == pytest.approx(-1.0 - (0.25 + 0.1 + 0.375 * 0.95) / 5)
