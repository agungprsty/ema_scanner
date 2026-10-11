import pandas as pd
from src.strategy.macd import compute_macd, detect_macd_cross, MacdSignal


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
