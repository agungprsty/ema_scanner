import math

import pandas as pd
import pytest

from src.strategy import rsi as rsi_mod
from src.strategy.rsi import Signal, compute_rsi, detect_signals, levels

pytestmark = pytest.mark.unit

T0 = pd.Timestamp("2026-01-01")
T0_MS = 1_767_225_600_000

# Generated once from backtest.calculate_rsi(period=14); backtest itself is not imported in CI
PARITY_CLOSES = [
    100, 101.5, 100.8, 102.3, 103.1, 102.0, 101.2, 103.8, 104.5, 104.1,
    105.6, 104.9, 103.7, 104.2, 106.0, 105.3, 104.0, 102.6, 103.4, 101.9,
]  # fmt: skip
PARITY_RSI = [82.0130282568, 78.6445463656, 72.6746814859, 66.7943104801, 68.3692977891, 62.3935757651]


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


def test_levels():
    assert levels("LONG", 100.0) == (pytest.approx(102.0), pytest.approx(99.0))
    assert levels("SHORT", 100.0) == (pytest.approx(98.0), pytest.approx(101.0))


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
    assert sig.take_profit == pytest.approx(sig.entry_price * 1.02)
    assert sig.stop_loss == pytest.approx(sig.entry_price * 0.99)
    assert sig.signal_candle_ms == T0_MS + i * 3_600_000


def test_short_on_cross_up_to_overbought():
    closes = [100 + (0.5 if i % 2 else -0.5) for i in range(30)] + [100 + 0.7 * i for i in range(1, 15)]
    df = _frame(closes)
    i = _first_index(compute_rsi(df["close"]) >= 72)

    sigs = detect_signals(df.iloc[: i + 1], "ETHUSDT", lookback=3)

    assert [(s.side, s.bars_ago) for s in sigs] == [("SHORT", 0)]
    sig = sigs[0]
    assert sig.entry_price == df["high"].iloc[i]
    assert sig.take_profit == pytest.approx(sig.entry_price * 0.98)
    assert sig.stop_loss == pytest.approx(sig.entry_price * 1.01)


def _with_rsi(monkeypatch, values):
    monkeypatch.setattr(rsi_mod, "compute_rsi", lambda close: pd.Series(values, dtype=float))
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
