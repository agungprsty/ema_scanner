import pandas as pd
import pytest

from src.strategy.macd import MacdSignal, compute_macd, detect_macd_cross

pytestmark = pytest.mark.unit


def _generate_synthetic_df(trend: str = "bullish") -> pd.DataFrame:
    base = 100.0
    prices = []
    if trend == "bullish":
        for i in range(100):
            if i < 70:
                base -= 0.5
            else:
                base += 1.5
            prices.append(base)
    else:
        for i in range(100):
            if i < 70:
                base += 0.5
            else:
                base -= 1.5
            prices.append(base)

    timestamps = pd.date_range("2026-01-01", periods=100, freq="2h")
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": prices,
        "high": [p + 1.0 for p in prices],
        "low": [p - 1.0 for p in prices],
        "close": prices,
        "volume": [1000.0] * 100,
    })


def test_compute_macd():
    df = _generate_synthetic_df("bullish")
    res = compute_macd(df, fast=12, slow=26, signal=9)
    assert res is not None
    assert "MACD" in res.columns
    assert "MACDs" in res.columns
    assert "MACDh" in res.columns
    assert len(res) > 50


def test_detect_macd_cross():
    df = _generate_synthetic_df("bullish")
    df_macd = compute_macd(df, fast=12, slow=26, signal=9)
    assert df_macd is not None

    sig = detect_macd_cross(df_macd, "BTCUSDT", lookback_candles=15, include_current=False)
    if sig:
        assert isinstance(sig, MacdSignal)
        assert sig.symbol == "BTCUSDT"
        assert sig.side in ("LONG", "SHORT")
        assert sig.entry_price > 0
        assert sig.stop_loss > 0
        assert sig.tp1 > 0
        assert sig.risk_reward_ratio == 1.5
        assert sig.to_dict()["entry_price"] == sig.entry_price


def _cross_frame() -> pd.DataFrame:
    """Prices fall then turn up, so MACD crosses up a few candles before the end."""
    df = _generate_synthetic_df("bullish")
    df["open"] = df["close"] - 0.25
    return compute_macd(df, fast=12, slow=26, signal=9)


def _last_cross_index(df: pd.DataFrame) -> int:
    above = df["MACD"] > df["MACDs"]
    return int(above[above != above.shift()].index[-1])


def test_confirmed_cross_enters_at_next_candle_open():
    full = _cross_frame()
    i = _last_cross_index(full)
    # Treat the candle after the cross as the one still forming
    df = full.iloc[: i + 2].reset_index(drop=True)

    sig = detect_macd_cross(df, "BTCUSDT", lookback_candles=3)

    assert sig is not None
    assert sig.is_confirmed is True
    assert sig.bars_ago == 0
    assert sig.entry_price == pytest.approx(df.iloc[i + 1]["open"])
    assert sig.cross_price != sig.entry_price
    assert sig.tp1 - sig.entry_price == pytest.approx((sig.entry_price - sig.stop_loss) * sig.risk_reward_ratio, rel=1e-4)


def test_cross_on_forming_candle_needs_include_current():
    full = _cross_frame()
    i = _last_cross_index(full)
    df = full.iloc[: i + 1].reset_index(drop=True)

    assert detect_macd_cross(df, "BTCUSDT", lookback_candles=1) is None
    sig = detect_macd_cross(df, "BTCUSDT", lookback_candles=1, include_current=True)
    assert sig is not None
    assert sig.is_confirmed is False
    assert sig.entry_price == pytest.approx(df.iloc[i]["close"])


def test_lookback_reports_age_of_older_cross():
    full = _cross_frame()
    i = _last_cross_index(full)
    df = full.iloc[: i + 4].reset_index(drop=True)

    assert detect_macd_cross(df, "BTCUSDT", lookback_candles=2) is None
    sig = detect_macd_cross(df, "BTCUSDT", lookback_candles=3)
    assert sig is not None
    assert sig.bars_ago == 2
