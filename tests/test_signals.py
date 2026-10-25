import math

import pandas as pd
import pytest

from src.strategy import signals as signals_mod
from src.strategy.signals import Signal, compute_ema, compute_rsi, compute_supertrend, detect_signals, levels

pytestmark = pytest.mark.unit

T0 = pd.Timestamp("2026-01-01")
T0_MS = 1_767_225_600_000

# Generated once from backtest.calculate_rsi(period=14); backtest itself is not imported in CI
PARITY_CLOSES = [
    100, 101.5, 100.8, 102.3, 103.1, 102.0, 101.2, 103.8, 104.5, 104.1,
    105.6, 104.9, 103.7, 104.2, 106.0, 105.3, 104.0, 102.6, 103.4, 101.9,
]  # fmt: skip
PARITY_RSI = [82.0130282568, 78.6445463656, 72.6746814859, 66.7943104801, 68.3692977891, 62.3935757651]

# Supertrend(10, 3) generated once from a line-by-line port of TradingView's pine_supertrend
# (same ATR seeding as production); one downtrend that flips up on candle 30
ST_HIGHS = [
    101.19, 99.88, 100.42, 101.82, 99.45, 97.91, 98.26, 97.69, 97.8, 96.59,
    97.72, 98.75, 98.2, 99.89, 100.41, 99.33, 99.89, 99.27, 99.61, 100.1,
    99.09, 98.67, 100.29, 100.23, 99.75, 99.36, 99.65, 100.05, 100.95, 101.15,
    103.4, 102.95, 102.39, 101.85, 102.48, 104.0, 103.52, 102.7, 101.85, 102.35,
]  # fmt: skip
ST_LOWS = [
    99.63, 98.72, 99.53, 100.66, 98.57, 96.49, 96.94, 96.42, 96.26, 95.24,
    96.23, 97.39, 97.57, 98.81, 99.86, 98.75, 99.17, 97.92, 98.55, 98.88,
    98.5, 97.18, 99.15, 98.63, 98.62, 97.98, 98.84, 99.09, 99.24, 100.53,
    102.45, 102.02, 101.65, 100.93, 101.22, 102.75, 102.38, 101.44, 99.98, 101.31,
]  # fmt: skip
ST_CLOSES = [
    100.37, 99.12, 100.02, 101.15, 98.81, 97.24, 97.4, 97.02, 97.0, 95.97,
    97.03, 97.96, 98.04, 99.39, 99.95, 98.92, 99.37, 98.22, 99.27, 99.21,
    98.99, 98.17, 99.64, 99.45, 98.94, 98.52, 99.15, 99.59, 100.09, 100.61,
    103.18, 102.69, 102.07, 101.1, 101.84, 103.19, 103.05, 102.05, 101.06, 101.84,
]  # fmt: skip
ST_LINE = [math.nan] * 9 + [100.8695019485] * 21 + [98.4041130687] * 5 + [98.952070676] * 5
ST_UP = [math.nan] * 9 + [0.0] * 21 + [1.0] * 10


def _frame(closes) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range(T0, periods=len(closes), freq="1h"),
        "open": closes,
        "high": [c + 1.0 for c in closes],
        "low": [c - 1.0 for c in closes],
        "close": closes,
    })


def test_compute_rsi_matches_backtest():
    rsi = compute_rsi(pd.Series(PARITY_CLOSES, dtype=float))
    assert rsi.iloc[:14].isna().all()
    assert list(rsi.iloc[14:]) == pytest.approx(PARITY_RSI, abs=1e-8)


def test_compute_ema_seeds_after_period_then_recurses():
    close = pd.Series([1.0, 2.0, 3.0, 4.0, 10.0])
    ema = compute_ema(close, period=3)
    assert ema.iloc[:2].isna().all()
    # Seeded from the first close and smoothed with alpha 2 / (period + 1), like the chart's EMA
    expected = [1.0]
    for c in close.iloc[1:]:
        expected.append(0.5 * c + 0.5 * expected[-1])
    assert list(ema.iloc[2:]) == pytest.approx(expected[2:])


def _ohlc(highs, lows, closes) -> pd.DataFrame:
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


def test_compute_supertrend_matches_tradingview_port():
    line, up = compute_supertrend(_ohlc(ST_HIGHS, ST_LOWS, ST_CLOSES), period=10, factor=3.0)
    assert list(line) == pytest.approx(ST_LINE, abs=1e-8, nan_ok=True)
    assert list(up) == pytest.approx(ST_UP, nan_ok=True)


def test_supertrend_starts_as_a_downtrend_once_the_atr_is_defined():
    _, up = compute_supertrend(_ohlc(ST_HIGHS, ST_LOWS, ST_CLOSES), period=5, factor=3.0)
    assert up.iloc[:4].isna().all()
    assert up.iloc[4] == 0.0


def test_supertrend_flips_up_on_the_close_through_the_upper_band():
    # Steady fall, then one candle closes far above everything before it
    closes = [100 - 0.5 * i for i in range(30)] + [120.0]
    line, up = compute_supertrend(_frame(closes), period=10, factor=3.0)
    assert up.iloc[-2] == 0.0 and up.iloc[-1] == 1.0
    # The line is the upper band during the downtrend, which only ever moves down
    down = line.iloc[9:-1]
    assert (down.diff().dropna() <= 0).all()
    assert line.iloc[-1] < closes[-1]


def test_supertrend_lower_band_never_falls_while_the_trend_holds():
    closes = [100 + 0.5 * i + (0.8 if i % 2 else -0.8) for i in range(60)]
    line, up = compute_supertrend(_frame(closes), period=10, factor=3.0)
    rising = line[up == 1.0]
    assert len(rising) > 10
    assert (rising.diff().dropna() >= 0).all()


def test_levels():
    # Net of the 0.1% round-trip fee: +2% at the target, -1% at the stop
    assert levels("LONG", 100.0) == (pytest.approx(102.1), pytest.approx(99.1))
    assert levels("SHORT", 100.0) == (pytest.approx(97.9), pytest.approx(100.9))


def _first_index(mask: pd.Series) -> int:
    return int(mask[mask].index[0])


def test_long_on_cross_down_to_oversold():
    # Chop around 100 keeps RSI near 50, then a steady fall drags it under 28
    closes = [100 + (0.5 if i % 2 else -0.5) for i in range(30)] + [100 - 0.7 * i for i in range(1, 15)]
    df = _frame(closes)
    rsi = compute_rsi(df["close"])
    i = _first_index(rsi <= 28)

    sigs = detect_signals(df.iloc[: i + 1], "BTCUSDT", lookback=3)

    assert len(sigs) == 1
    sig = sigs[0]
    assert isinstance(sig, Signal)
    assert (sig.symbol, sig.side, sig.bars_ago) == ("BTCUSDT", "LONG", 0)
    assert sig.prev_rsi > 28 >= sig.rsi
    assert sig.entry_price == df["low"].iloc[i]
    assert sig.candle_close == df["close"].iloc[i]
    assert sig.take_profit == pytest.approx(sig.entry_price * 1.021)
    assert sig.stop_loss == pytest.approx(sig.entry_price * 0.991)
    assert sig.signal_candle_ms == T0_MS + i * 3_600_000


def test_short_on_cross_up_to_overbought():
    closes = [100 + (0.5 if i % 2 else -0.5) for i in range(30)] + [100 + 0.7 * i for i in range(1, 15)]
    df = _frame(closes)
    i = _first_index(compute_rsi(df["close"]) >= 72)

    sigs = detect_signals(df.iloc[: i + 1], "ETHUSDT", lookback=3)

    assert [(s.side, s.bars_ago) for s in sigs] == [("SHORT", 0)]
    sig = sigs[0]
    assert sig.entry_price == df["high"].iloc[i]
    assert sig.take_profit == pytest.approx(sig.entry_price * 0.979)
    assert sig.stop_loss == pytest.approx(sig.entry_price * 1.009)


def _with_rsi(monkeypatch, values):
    monkeypatch.setattr(signals_mod, "compute_rsi", lambda close: pd.Series(values, dtype=float))
    return _frame([100.0] * len(values))


def test_prev_exactly_at_threshold_does_not_signal(monkeypatch):
    assert detect_signals(_with_rsi(monkeypatch, [28.0, 20]), "X", lookback=2) == []
    assert detect_signals(_with_rsi(monkeypatch, [72.0, 80]), "X", lookback=2) == []


def test_nan_warmup_rows_are_skipped(monkeypatch):
    df = _with_rsi(monkeypatch, [math.nan, math.nan, 20, 50])
    assert detect_signals(df, "X", lookback=4) == []


def test_every_hit_in_lookback_is_returned_with_its_age(monkeypatch):
    df = _with_rsi(monkeypatch, [50, 27, 50, 73, 60, 50])

    sigs = detect_signals(df, "X", lookback=5)

    assert [(s.side, s.bars_ago, s.rsi, s.prev_rsi) for s in sigs] == [("LONG", 4, 27, 50), ("SHORT", 2, 73, 50)]
    assert [s.side for s in detect_signals(df, "X", lookback=3)] == ["SHORT"]
