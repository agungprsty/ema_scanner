"""macd_cross_ema50d_oppexit@1.0.0, the pre-registered shadow strategy: its rules on synthetic data, the
daily filter without look-ahead, the live scan and evaluator against the backtest harness on the fixture,
an independent full-series reference, the cost labels of its exits, its frozen hashes against the
pre-registration document, and the pre-registered holdout evaluation."""

import ast
import asyncio
import bisect
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.backtest import prereg
from src.backtest.data import load_klines_csv, resample_klines
from src.backtest.metrics import SCENARIOS, book_trades, summarize
from src.backtest.replay import BacktestTrade, replay_strategy
from src.services import db
from src.services.cron_scheduler import detect_strategy_signals, fetch_plan
from src.strategy import base, registry
from src.strategy.base import ResolveContext
from src.strategy.context import DAY_MS, MarketContext, normalize_klines
from src.strategy.exits import EXIT_RESULTS, OPPOSITE_CROSS, STOPPED, TIME_EXIT, cost_result
from src.strategy.macd import compute_macd
from src.strategy.macd_cross import macd_cross_candidates
from src.strategy.macd_cross_ema50d_oppexit import (
    MacdCrossEma50dOppExit,
    MacdCrossEma50dOppExitParams,
    daily_ema,
    opposite_cross_flags,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
PREREG_DOC = REPO / "docs" / "research" / "preregistration-macd_cross_ema50d_oppexit-1.0.0.md"
H2 = 2 * 60 * 60 * 1000
REF = "macd_cross_ema50d_oppexit@1.0.0"
SHADOW = registry.get(REF)
MACD = registry.get("macd_cross@2.2.0")
# The fixture covers 167 days, fewer than the frozen 200-day EMA window, so the replay and parity tests run
# the same code with a 60-day window; every other parameter is the frozen one
SMALL = MacdCrossEma50dOppExit(MacdCrossEma50dOppExitParams(trend_ema_bars=60))


@pytest.fixture(scope="module")
def btc():
    return load_klines_csv(FIXTURES / "klines" / "BTCUSDT_2h.csv")


@pytest.fixture(scope="module")
def daily(btc):
    return resample_klines(btc, "2h", "1d")


@pytest.fixture(scope="module")
def replay(btc, daily):
    return replay_strategy(SMALL, btc, "BTCUSDT", context={"1d": daily})


# --- registration, freezing, pre-registration -------------------------------------------------


def _strategy_imports(module: str) -> set[str]:
    tree = ast.parse(base.module_source(module))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src.strategy")
    }


def test_shadow_is_registered_frozen_and_runs_without_alerts():
    assert SHADOW.status == "shadow" and SHADOW in registry.runnable()
    assert registry.runnable().index(MACD) < registry.runnable().index(SHADOW)
    assert SHADOW.params_dict == {
        "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "macd_candles": 500, "swing_lookback_candles": 5,
        "trend_ema_period": 50, "trend_ema_bars": 200, "max_hold_candles": 96,
    }
    entry = registry.load_lock()[REF]
    assert (entry["params_hash"], entry["code_hash"]) == (SHADOW.params_hash, SHADOW.code_hash)
    assert registry.emission_refusal(SHADOW, registry.lock_record(SHADOW)) is None
    # The code hash covers the strategy module and every src.strategy module it imports
    own = type(SHADOW).__module__
    assert set(SHADOW.code_modules) == _strategy_imports(own)
    assert entry["code_modules"] == [own, *SHADOW.code_modules]
    assert SHADOW.code_hash == base.source_hash((own, *SHADOW.code_modules))
    # The live strategy is untouched by its arrival
    assert registry.load_lock()["macd_cross@2.2.0"]["code_hash"] == MACD.code_hash


def test_live_fetch_plan_and_windows():
    assert SHADOW.warmup_bars == 498 and SHADOW.history_bars == 500 and SHADOW.resolve_history_bars == 498
    assert SHADOW.max_hold_bars == 97 and SHADOW.context_bars == 201
    assert fetch_plan([MACD, SHADOW]) == {"2h": 500, "1d": 202}
    # The evaluator's request stays within one Binance call
    assert SHADOW.resolve_history_bars + SHADOW.max_hold_bars + 1 <= 1500


def test_preregistration_document_pins_the_frozen_strategy():
    text = PREREG_DOC.read_text()
    assert SHADOW.params_hash in text and SHADOW.code_hash in text
    assert json.dumps(SHADOW.params_dict, sort_keys=True, separators=(",", ":")) in text
    for module in (type(SHADOW).__module__, *SHADOW.code_modules):
        assert hashlib.sha256(base.module_source(module)).hexdigest() in text, module
    for symbol in prereg.SYMBOLS:
        assert symbol in text
    assert prereg.HOLDOUT_START in text and "2024-09-30" in text


# --- exit rules on synthetic candles --------------------------------------------------------------


def _synthetic(n: int, start_ms: int = 1_735_689_600_000, price: float = 100.0) -> pd.DataFrame:
    open_ms = start_ms + H2 * np.arange(n, dtype=np.int64)
    p = np.full(n, price)
    return normalize_klines(pd.DataFrame({"open_time_ms": open_ms, "open": p, "high": p + 0.5, "low": p - 0.5,
                                          "close": p, "volume": np.ones(n)}))


class _Exit:
    """A shadow strategy whose opposite crosses are given, to test the exit loop on its own."""

    def __init__(self, flags, side="LONG", stop=90.0):
        self.strategy = MacdCrossEma50dOppExit()
        self.strategy._opposite_crosses = lambda history, closed, s: np.array(flags[: len(closed)], dtype=bool)
        hist = _synthetic(499)
        self.cross_ms = int(hist["open_time_ms"].iat[-1])
        self.history = MarketContext("X", self.cross_ms + H2, "2h", {"2h": hist})
        self.signal = SimpleNamespace(side=side, entry_price=100.0, stop_loss=stop, tp1=0.0,
                                      cross_candle_ms=self.cross_ms)

    def candles(self, n, changes=None):
        rows = [[self.cross_ms + (j + 1) * H2, 100.0 + j, 100.5 + j, 99.5 + j, 100.25 + j] for j in range(n)]
        for j, (field, value) in (changes or {}).items():
            rows[j][{"high": 2, "low": 3}[field]] = value
        return rows

    def resolve(self, candles, now_ms=None):
        now_ms = now_ms if now_ms is not None else int(candles[-1][0]) + H2 if candles else self.cross_ms + H2
        return self.strategy.resolve(self.signal, candles, ResolveContext(now_ms, H2, self.history))


def test_stop_is_checked_first_and_booked_at_the_stop():
    t = _Exit(flags=[False, False, True])
    out = t.resolve(t.candles(5, {2: ("low", 89.0)}))
    assert (out.result, out.win_status, out.exit_price) == (STOPPED, STOPPED, 90.0)
    assert out.exit_time_ms == t.cross_ms + 3 * H2  # the open of the candle that touched it

    short = _Exit(flags=[False] * 10, side="SHORT", stop=103.0)
    out = short.resolve(short.candles(6))
    assert out.result == STOPPED and out.exit_time_ms == short.cross_ms + 4 * H2  # high 103.5 on the fourth candle


def test_opposite_cross_exits_at_the_next_open():
    t = _Exit(flags=[False, False, False, False, True])
    out = t.resolve(t.candles(8))
    assert (out.result, out.exit_price, out.exit_time_ms) == (OPPOSITE_CROSS, 105.0, t.cross_ms + 6 * H2)
    # On the entry candle itself
    t = _Exit(flags=[True])
    out = t.resolve(t.candles(3))
    assert (out.result, out.exit_price, out.exit_time_ms) == (OPPOSITE_CROSS, 101.0, t.cross_ms + 2 * H2)


def test_opposite_cross_waits_for_the_next_candle_to_open():
    t = _Exit(flags=[False, False, True])
    assert t.resolve(t.candles(3)) is None
    assert t.resolve(t.candles(4)).exit_time_ms == t.cross_ms + 4 * H2


def test_time_exit_at_the_close_of_the_96th_candle():
    t = _Exit(flags=[False] * 97)
    out = t.resolve(t.candles(97))
    assert (out.result, out.exit_price, out.exit_time_ms) == (TIME_EXIT, 100.25 + 95, t.cross_ms + 97 * H2)
    assert t.resolve(t.candles(95)) is None
    # Only once that candle has closed
    candles = t.candles(96)
    assert t.resolve(candles, now_ms=int(candles[-1][0]) + H2 - 1) is None
    assert t.resolve(candles).result == TIME_EXIT


def test_opposite_cross_on_the_last_candle_exits_at_the_97th_open():
    t = _Exit(flags=[False] * 95 + [True])
    out = t.resolve(t.candles(97))
    assert (out.result, out.exit_price, out.exit_time_ms) == (OPPOSITE_CROSS, 196.0, t.cross_ms + 97 * H2)
    # The same instant a time exit would have closed at
    assert out.exit_time_ms == t.cross_ms + 96 * H2 + H2


def test_forming_candle_can_stop_but_not_cross():
    t = _Exit(flags=[False, False, False, True])
    candles = t.candles(4)
    forming_now = int(candles[3][0]) + 60_000
    # Candle 4 is still forming: its cross is not final, so the trade stays open
    assert t.resolve(candles, now_ms=forming_now) is None
    # A touch of the stop is a printed price even on the forming candle
    out = t.resolve(t.candles(4, {3: ("low", 80.0)}), now_ms=forming_now)
    assert out.result == STOPPED and out.exit_time_ms == int(candles[3][0])


def test_resolve_needs_the_full_macd_history():
    t = _Exit(flags=[True])
    with pytest.raises(ValueError, match="history"):
        t.strategy.resolve(t.signal, t.candles(3), ResolveContext(t.cross_ms + 4 * H2, H2, None))
    short = MarketContext("X", t.cross_ms + H2, "2h", {"2h": _synthetic(499).iloc[1:]})
    assert t.strategy.resolve(t.signal, t.candles(3), ResolveContext(t.cross_ms + 4 * H2, H2, short)) is None
    other = SimpleNamespace(**{**vars(t.signal), "cross_candle_ms": t.cross_ms - H2})
    assert t.strategy.resolve(other, t.candles(3), ResolveContext(t.cross_ms + 4 * H2, H2, t.history)) is None


def test_opposite_cross_flags_are_the_crosses_detect_macd_cross_sees(btc):
    df = compute_macd(btc)
    crosses = macd_cross_candidates(df)
    golden = {int(i) for i in crosses if df["MACD"].iat[i] > df["MACDs"].iat[i]}
    death = {int(i) for i in crosses if df["MACD"].iat[i] < df["MACDs"].iat[i]}
    assert set(np.flatnonzero(opposite_cross_flags(df, 0, "LONG")) + 1) == death
    assert set(np.flatnonzero(opposite_cross_flags(df, 0, "SHORT")) + 1) == golden


# --- the daily filter -----------------------------------------------------------------------------


def _ema_by_hand(values: list[float], n: int) -> float:
    ema = sum(values[:n]) / n
    alpha = 2 / (n + 1)
    for v in values[n:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def test_daily_ema_is_sma_seeded_like_compute_macd():
    closes = pd.Series(100 + 10 * np.sin(np.arange(200) / 7.0))
    ema = daily_ema(closes, 50)
    assert ema.iloc[:49].isna().all()
    assert ema.iloc[49] == pytest.approx(closes.iloc[:50].mean(), rel=1e-12)
    assert ema.iloc[-1] == pytest.approx(_ema_by_hand(closes.tolist(), 50), rel=1e-12)


def _daily_frames(closes: list[float], first_day_ms: int = 1_704_067_200_000) -> dict:
    """2h and 1d frames: one day per close, its 12 2h candles flat at that close."""
    n = len(closes)
    day_open = first_day_ms + DAY_MS * np.arange(n, dtype=np.int64)
    daily = normalize_klines(pd.DataFrame({"open_time_ms": day_open, "open": closes, "high": closes, "low": closes,
                                           "close": closes, "volume": np.ones(n)}))
    two_h = normalize_klines(pd.DataFrame({
        "open_time_ms": first_day_ms + H2 * np.arange(12 * n, dtype=np.int64),
        **{c: np.repeat(closes, 12) for c in ("open", "high", "low", "close")},
        "volume": np.ones(12 * n),
    }))
    return {"2h": two_h, "1d": daily}


def test_daily_trend_compares_the_last_close_with_its_ema():
    rising = [100.0 + i for i in range(205)]
    frames = _daily_frames(rising)
    at = int(frames["1d"]["open_time_ms"].iat[-1]) + DAY_MS
    trend = SHADOW.daily_trend(MarketContext("X", at, "2h", frames), at)
    assert trend["side"] == "LONG" and trend["day_close"] == rising[-1]
    assert trend["ema"] == pytest.approx(_ema_by_hand(rising[-200:], 50), rel=1e-12)

    falling = _daily_frames(rising[::-1])
    assert SHADOW.daily_trend(MarketContext("X", at, "2h", falling), at)["side"] == "SHORT"
    flat = _daily_frames([100.0] * 205)
    assert SHADOW.daily_trend(MarketContext("X", at, "2h", flat), at)["side"] is None
    # Fewer than 200 closed days: no trend, so no signal
    short = _daily_frames(rising[:199])
    at_short = int(short["1d"]["open_time_ms"].iat[-1]) + DAY_MS
    assert SHADOW.daily_trend(MarketContext("X", at_short, "2h", short), at_short) is None


def test_daily_filter_never_reads_a_day_closing_after_the_cross():
    """A cross candle closing at 12:00 reads the day that closed at 00:00; the day it sits in closes at 24:00
    and must not count, even when the context holds it (and later days) in full."""
    closes = [100.0 + i for i in range(205)]
    frames = _daily_frames(closes)
    today = int(frames["1d"]["open_time_ms"].iat[-1])
    noon = today + 12 * 60 * 60 * 1000
    ctx = MarketContext("X", noon, "2h", frames)
    trend = SHADOW.daily_trend(ctx, noon)
    assert trend["day_open_ms"] == today - DAY_MS and trend["day_close"] == closes[-2]

    # Crash today's close (and add tomorrow): a filter that peeked would flip to SHORT
    crashed = {**frames, "1d": frames["1d"].copy()}
    crashed["1d"].loc[crashed["1d"].index[-1], "close"] = 1.0
    tomorrow = crashed["1d"].iloc[[-1]].assign(open_time_ms=today + DAY_MS, close=1.0)
    crashed["1d"] = normalize_klines(pd.concat([crashed["1d"], tomorrow], ignore_index=True))
    assert SHADOW.daily_trend(MarketContext("X", noon, "2h", crashed), noon) == trend
    # Asked about a later instant, the context still caps at its decision
    assert SHADOW.daily_trend(MarketContext("X", noon, "2h", crashed), noon + 3 * DAY_MS) == trend

    # The 22:00 candle closes at midnight, exactly when today's daily candle closes: that day counts
    midnight = today + DAY_MS
    assert SHADOW.daily_trend(MarketContext("X", midnight, "2h", frames), midnight)["day_open_ms"] == today
    assert SHADOW.daily_trend(MarketContext("X", midnight, "2h", crashed), midnight)["side"] == "SHORT"


def test_signals_are_the_live_crosses_that_agree_with_the_daily_trend(btc, daily, replay):
    """Every cross the live strategy takes is taken here only in the trend's direction, at the same entry and
    stop, without a target; a daily frame cut at each decision gives the same signals (no look-ahead)."""
    base = normalize_klines(btc)
    taken = 0
    for k in MACD.candidate_bars(base):
        k = int(k)
        if k < SMALL.warmup_bars or k + 1 >= len(base):
            continue
        decision = int(base["open_time_ms"].iat[k]) + H2
        frames = {"2h": base, "1d": daily}
        ctx = MarketContext("BTCUSDT", decision, "2h", frames, next_open=float(base["open"].iat[k + 1]))
        [live] = MACD.detect(ctx)
        trend = SMALL.daily_trend(ctx, decision)
        found = SMALL.detect(ctx)
        cut = {"2h": base.iloc[: k + 1], "1d": daily[daily["open_time_ms"] + DAY_MS <= decision]}
        assert [s.to_dict() for s in SMALL.detect(MarketContext("BTCUSDT", decision, "2h", cut,
                                                                next_open=ctx.next_open))] == [
            s.to_dict() for s in found
        ]
        if trend is None or trend["side"] != live.side:
            assert found == []
            continue
        [sig] = found
        assert (sig.side, sig.entry_price, sig.stop_loss) == (live.side, live.entry_price, live.stop_loss)
        assert (sig.tp1, sig.risk_reward_ratio) == (0.0, 0.0)
        assert (sig.strategy_key, sig.strategy_version) == (SMALL.key, SMALL.version)
        assert sig.features == {"trend_day_open_ms": trend["day_open_ms"], "trend_day_close": trend["day_close"],
                                "trend_ema": trend["ema"]}
        taken += 1
    assert taken > 20 and taken == len(replay.trades) + replay.unresolved


# --- live paths against the harness ---------------------------------------------------------------


def _reference(klines: pd.DataFrame, daily: pd.DataFrame, p: MacdCrossEma50dOppExitParams):
    """The rules written out independently: MACD once over the whole series, the EMA by hand, the stop and
    the exit loop inline. Returns (signals, trades) as tuples."""
    k_df = normalize_klines(klines)
    df = compute_macd(k_df, fast=p.macd_fast, slow=p.macd_slow, signal=p.macd_signal)
    off = len(k_df) - len(df)
    o, h, lo, c = (k_df[col].to_numpy(dtype=float) for col in ("open", "high", "low", "close"))
    t = k_df["open_time_ms"].to_numpy()
    m, s = df["MACD"].to_numpy(dtype=float), df["MACDs"].to_numpy(dtype=float)
    up = np.zeros(len(k_df), bool)
    dn = np.zeros(len(k_df), bool)
    up[off + 1:] = (m[:-1] <= s[:-1]) & (m[1:] > s[1:])
    dn[off + 1:] = (m[:-1] >= s[:-1]) & (m[1:] < s[1:])
    d_open = daily["open_time_ms"].to_numpy()
    d_close = daily["close"].to_numpy(dtype=float)
    signals, trades = [], []
    for i in np.flatnonzero(up | dn):
        if i < p.macd_candles - 2 or i + 1 >= len(k_df):
            continue
        side = "LONG" if up[i] else "SHORT"
        decision = t[i] + H2
        n_days = bisect.bisect_right(d_open.tolist(), decision - DAY_MS)
        if n_days < p.trend_ema_bars:
            continue
        window = d_close[n_days - p.trend_ema_bars: n_days].tolist()
        ema = _ema_by_hand(window, p.trend_ema_period)
        if not ((side == "LONG" and window[-1] > ema) or (side == "SHORT" and window[-1] < ema)):
            continue
        entry = round(o[i + 1], 6)
        w0 = max(0, i - p.swing_lookback_candles)
        if side == "LONG":
            stop = lo[w0: i + 1].min() * 0.998
            stop = entry * 0.985 if stop >= entry else stop
        else:
            stop = h[w0: i + 1].max() * 1.002
            stop = entry * 1.015 if stop <= entry else stop
        stop = round(stop, 6)
        signals.append((int(t[i]), side, entry, stop))
        opposite = dn if side == "LONG" else up
        for j in range(1, p.max_hold_candles + 1):
            bar = i + j
            if bar >= len(k_df):
                break
            if (side == "LONG" and lo[bar] <= stop) or (side == "SHORT" and h[bar] >= stop):
                trades.append((int(t[i]), side, entry, stop, STOPPED, stop, int(t[bar])))
                break
            if opposite[bar]:
                if bar + 1 < len(k_df):
                    trades.append((int(t[i]), side, entry, stop, OPPOSITE_CROSS, o[bar + 1], int(t[bar + 1])))
                break
            if j == p.max_hold_candles:
                trades.append((int(t[i]), side, entry, stop, TIME_EXIT, c[bar], int(t[bar]) + H2))
    return signals, trades


def _as_tuple(tr: BacktestTrade):
    return (tr.cross_candle_ms, tr.side, tr.entry_price, tr.stop_loss, tr.result, tr.exit_price, tr.exit_time_ms)


def test_harness_matches_an_independent_full_series_reference(btc, daily, replay):
    signals, trades = _reference(btc, daily, SMALL.params)
    assert [_as_tuple(t) for t in replay.trades] == trades
    assert len(signals) == len(trades) + replay.unresolved
    results = {r for *_, r, _, _ in trades}
    assert {STOPPED, OPPOSITE_CROSS} <= results


def _binance_rows(klines: pd.DataFrame) -> list[list]:
    return [
        [int(r.open_time_ms), f"{r.open}", f"{r.high}", f"{r.low}", f"{r.close}", "1.0",
         int(r.open_time_ms) + H2 - 1, "100.0", 10, "0.5", "50.0", "0"]
        for r in normalize_klines(klines).itertuples()
    ]


def test_live_evaluator_settles_each_trade_on_the_same_candle_as_the_harness(btc, replay):
    """The evaluator path (Binance-shaped klines from startTime, the forming candle showing only its open,
    run after every 2h close) books exactly the harness's exit, at the first run that can know it."""
    rows = _binance_rows(btc)
    open_ms = [r[0] for r in rows]
    hist, limit = SMALL.resolve_history_bars, SMALL.resolve_history_bars + SMALL.max_hold_bars + 1
    assert len(replay.trades) > 20
    for trade in replay.trades:
        rec = SimpleNamespace(symbol="BTCUSDT", side=trade.side, entry_price=trade.entry_price,
                              stop_loss=trade.stop_loss, tp1=0.0, cross_candle_ms=trade.cross_candle_ms,
                              timeframe="2h")
        first = bisect.bisect_left(open_ms, trade.cross_candle_ms - hist * H2)
        outcome = None
        for now_open in range(trade.entry_time_ms, open_ms[-1] + H2, H2):
            visible = rows[first: bisect.bisect_right(open_ms, now_open)][:limit]
            if visible[-1][0] == now_open:  # forming: nothing but its open has printed
                op = visible[-1][1]
                visible = [*visible[:-1], [now_open, op, op, op, op, "0"]]
            outcome = db.settle_signal(SMALL, rec, visible, now_open + 15_000)
            if outcome is not None:
                break
        assert outcome is not None, trade
        assert (outcome.result, outcome.exit_price, outcome.exit_time_ms) == (
            trade.result, trade.exit_price, trade.exit_time_ms
        )
        # Booked by the first run that can know it, never earlier: an opposite cross or a time exit at the
        # instant it fills; a stop once its candle has printed the touch (its open, or else its close)
        if trade.result == STOPPED:
            assert trade.exit_time_ms <= now_open <= trade.exit_time_ms + H2
        else:
            assert now_open == trade.exit_time_ms


def test_live_scan_detects_what_the_harness_replays(btc, daily, replay):
    """The cron path (fetch_klines frames from Binance-shaped rows, the forming 2h and 1d candles in full)
    finds each harness signal right after its cross candle closes, and nothing else."""
    from src.data_feed.ohlcv import fetch_klines

    rows_2h = _binance_rows(btc)
    rows_1d = [
        [r[0], r[1], r[2], r[3], r[4], r[5], r[0] + DAY_MS - 1, "1", 1, "0", "0", "0"]
        for r in _binance_rows(load_klines_csv(FIXTURES / "klines" / "BTCUSDT_1d.csv"))
    ]
    plan = fetch_plan([SMALL])
    signals, _ = _reference(btc, daily, SMALL.params)
    expected = {s[0]: s for s in signals}
    base = normalize_klines(btc)
    positions = set(int(k) + 1 for k in SMALL.candidate_bars(base)) | set(range(500, len(rows_2h), 37))
    found = {}
    for end in sorted(positions):
        if end < SMALL.history_bars or end >= len(rows_2h):
            continue
        now_open = rows_2h[end][0]
        visible = {"2h": rows_2h[: end + 1], "1d": [r for r in rows_1d if r[0] <= now_open]}
        client = SimpleNamespace(klines=lambda symbol, interval, limit: visible[interval][-limit:])
        frames = {tf: fetch_klines(client, "BTCUSDT", tf, limit) for tf, limit in plan.items()}
        for sig in detect_strategy_signals(SMALL, "BTCUSDT", frames, now_open + 15_000):
            if sig.bars_ago == 0:
                found[sig.cross_candle_ms] = (sig.cross_candle_ms, sig.side, sig.entry_price, sig.stop_loss)
    assert found == expected and len(found) > 20


def test_frozen_rules_replay_nothing_without_200_closed_days(btc, daily):
    assert replay_strategy(SHADOW, btc, "BTCUSDT", context={"1d": daily}).trades == []


# --- the ledger -----------------------------------------------------------------------------------


def test_exit_labels_are_priced_by_the_cost_model():
    assert cost_result(STOPPED) == "LOSS"
    assert cost_result(OPPOSITE_CROSS) == cost_result(TIME_EXIT) == "EXPIRED"
    assert [cost_result(r) for r in ("PROFIT", "LOSS", "EXPIRED")] == ["PROFIT", "LOSS", "EXPIRED"]
    assert set(EXIT_RESULTS) <= set(db.COUNTED_RESULTS)

    stop = db.trade_costs("SOLUSDT", "LONG", 100.0, 98.0, 98.0, STOPPED)[0]
    assert stop == db.trade_costs("SOLUSDT", "LONG", 100.0, 98.0, 98.0, "LOSS")[0]
    # 8 bps in, 12 bps out (1.5x) and the 0.1% fee, over a 2% risk
    assert stop.cost_r == pytest.approx((0.08 + 0.12 * 0.98 + 0.1) / 2, rel=1e-9)
    for label in (OPPOSITE_CROSS, TIME_EXIT):
        market = db.trade_costs("SOLUSDT", "LONG", 100.0, 98.0, 103.0, label)[0]
        assert market == db.trade_costs("SOLUSDT", "LONG", 100.0, 98.0, 103.0, "EXPIRED")[0]
        assert market.cost_r == pytest.approx((0.08 + 0.08 * 1.03 + 0.1) / 2, rel=1e-9)


def test_backtest_books_exit_labels_like_the_ledger():
    trades = [
        BacktestTrade("SOLUSDT", "LONG", 0, H2, 100.0, 98.0, 0.0, STOPPED, 98.0, 2 * H2),
        BacktestTrade("SOLUSDT", "SHORT", H2, 2 * H2, 100.0, 102.0, 0.0, OPPOSITE_CROSS, 97.0, 3 * H2),
    ]
    booked = book_trades(trades, SCENARIOS["prod"])
    for b in booked:
        expected = db.trade_costs(b.trade.symbol, b.trade.side, b.trade.entry_price, b.trade.stop_loss,
                                  b.trade.exit_price, b.trade.result)[0]
        assert b.net_r == pytest.approx(expected.net_r, rel=1e-12)
    s = summarize(booked)
    assert s["outcomes"] == {"PROFIT": 0, "LOSS": 0, "EXPIRED": 0, OPPOSITE_CROSS: 1, STOPPED: 1}
    assert s["win_rate"] == 0.0 and s["positive_net_rate"] == 0.5


class _Result:
    def __init__(self, value=None, rows=None):
        self._value, self._rows = value, rows or []

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


class _Session:
    def __init__(self, results):
        self.results = list(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        return self.results.pop(0) if self.results else _Result()

    async def commit(self):
        pass


async def test_evaluator_books_shadow_exits_under_their_labels(monkeypatch, btc, replay):
    """The production evaluator, with the registered strategy, books a shadow trade as the harness did."""
    trade = next(t for t in replay.trades if t.result == OPPOSITE_CROSS)
    rows = _binance_rows(btc)
    rec = SimpleNamespace(
        id=11, symbol="BTCUSDT", side=trade.side, timeframe="2h", win_status="PENDING",
        cross_candle_ms=trade.cross_candle_ms, entry_price=trade.entry_price, cross_price=trade.entry_price * 0.999,
        stop_loss=trade.stop_loss, tp1=0.0, risk_reward_ratio=0.0, cross_time="", is_confirmed=True,
        entry_basis="next_open", source="cron", strategy_key=SHADOW.key, strategy_version=SHADOW.version,
    )
    calls = []

    def klines(**kwargs):
        calls.append(kwargs)
        first = [r[0] for r in rows].index(kwargs["startTime"])
        return rows[first: first + kwargs["limit"]]

    monkeypatch.setattr(db, "_session_maker", lambda: _Session([_Result(rows=[rec]), _Result(value=1)]))
    monkeypatch.setattr(db, "_evaluation_lock", asyncio.Lock())
    monkeypatch.setattr(db, "SHADOW_ALERTS", True)
    result = await db.evaluate_pending_signals(SimpleNamespace(klines=klines))

    assert calls == [{"symbol": "BTCUSDT", "interval": "2h", "startTime": trade.cross_candle_ms - 498 * H2,
                      "limit": 498 + 97 + 1}]
    assert rec.win_status == OPPOSITE_CROSS and result["opposite_cross_count"] == 1
    [booked] = result["closed"]
    assert (booked["result"], booked["exit_price"]) == (OPPOSITE_CROSS, trade.exit_price)
    assert booked["shadow_strategy"] == REF and booked["booked_by"] == "evaluator"
    [expected] = book_trades([trade], SCENARIOS["prod"])
    assert booked["net_r"] == pytest.approx(expected.net_r, rel=1e-12)


# --- the pre-registered evaluation --------------------------------------------------------------


def _trade(symbol: str, week: int, r_gross: float, i: int = 0) -> BacktestTrade:
    """A long risking 1% (entry 100, stop 99) that exits at the opposite cross with `r_gross` R gross."""
    cross = 1_577_836_800_000 + week * 7 * DAY_MS + i * H2
    return BacktestTrade(symbol, "LONG", cross, cross + H2, 100.0, 99.0, 0.0, OPPOSITE_CROSS,
                         100.0 + r_gross, cross + 10 * H2)


def _book(weeks: int, per_week: dict[str, list[float]]) -> list[BacktestTrade]:
    return [_trade(sym, w, r, i) for w in range(weeks) for sym, rs in per_week.items() for i, r in enumerate(rs)]


def test_prereg_thresholds_and_trimming():
    assert prereg.min_positive_symbols(12) == 7 and prereg.min_positive_symbols(6) == 4
    assert (prereg.MIN_T_STAT, prereg.IMPACT_ROUND_TRIP_PERCENT, prereg.TOP_TRIM_FRACTION) == (2.0, 0.10, 0.01)
    assert prereg.CM1.fee_percent == 0.1 and prereg.CM1_IMPACT.fee_percent == pytest.approx(0.2)
    assert prereg.CM1.slippage_bps("BTCUSDT") == 2.0 and prereg.CM1.slippage_bps("LINKUSDT") == 10.0
    booked = book_trades(_book(1, {"BTCUSDT": [float(i) for i in range(150)]}), prereg.CM1)
    trimmed = prereg.trim_top(booked)
    assert len(booked) - len(trimmed) == 2  # ceil(1% of 150)
    assert max(b.net_r for b in trimmed) < sorted(b.net_r for b in booked)[-2]


def test_prereg_passes_a_broad_robust_edge():
    trades = [*_book(120, {sym: [2.0, -0.5] for sym in prereg.SYMBOLS}),
              *(_trade(sym, w, -0.9, 5) for sym in prereg.SYMBOLS for w in range(0, 120, 3))]
    result = prereg.evaluate(trades)
    assert result["verdict"] == "PASS"
    assert all(result["all_symbols"]["criteria"].values())
    assert set(result["secondary"]) == {"control", "watchlist"}
    assert result["secondary"]["control"]["symbols"] == list(prereg.CONTROL)


def test_prereg_fails_an_edge_from_one_outlier_or_few_symbols_or_thin_margin():
    flat = {sym: [0.6, -0.6] for sym in prereg.SYMBOLS}
    outlier = [*_book(120, flat), _trade("BTCUSDT", 5, 900.0, 7)]
    result = prereg.evaluate(outlier)
    assert result["verdict"] == "FAIL" and not result["all_symbols"]["criteria"]["3_without_top_1pct"]

    few = _book(120, {**flat, **{sym: [2.0, -0.5] for sym in prereg.SYMBOLS[:6]}})
    result = prereg.evaluate(few)
    crit = result["all_symbols"]["criteria"]
    assert crit["1_mean_ci_t"] and not crit["4_positive_symbols"] and result["verdict"] == "FAIL"

    thin = _book(400, {sym: [0.8, -0.5] for sym in prereg.SYMBOLS})  # ~ +0.07R gross, under 10 bps impact
    crit = prereg.evaluate(thin)["all_symbols"]["criteria"]
    assert not crit["2_impact_10bps"]


def test_prereg_cli_guards_the_holdout(tmp_path, capsys):
    assert prereg.main(["--data", str(tmp_path)]) == 2
    assert "missing data" in capsys.readouterr().err
    assert prereg.main(["--data", str(tmp_path), "--start", "2025-01-01", "--purpose", "holdout"]) == 2
    assert "pre-registered window" in capsys.readouterr().err


def test_prereg_stats_use_the_weekly_block_bootstrap():
    booked = book_trades([_trade("BTCUSDT", w, r + 0.3 * (w % 5), i) for w in range(30)
                          for i, r in enumerate([1.0, -0.4])], prereg.CM1)
    stats = prereg.mean_stats(booked)
    net = np.array([b.net_r for b in booked])
    assert stats["n"] == 60 and stats["mean_net_r"] == pytest.approx(net.mean())
    assert stats["t_stat"] == pytest.approx(net.mean() / (net.std(ddof=1) / math.sqrt(60)))
    lo, hi = stats["ci95_net_r"]
    assert lo < stats["mean_net_r"] < hi
    assert prereg.mean_stats([]) == {"n": 0, "mean_net_r": None, "t_stat": None, "ci95_net_r": None,
                                     "mean_gross_r": None}
