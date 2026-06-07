import pandas as pd
import pandas_ta as ta


def compute_btc_bias(df: pd.DataFrame) -> str:
    if df is None or len(df) < 100:
        return "NEUTRAL"

    close = df["close"]
    vwap = (close * df["volume"]).rolling(168).sum() / df["volume"].rolling(168).sum()
    ema15 = close.ewm(span=15, adjust=False).mean()
    ema100 = close.ewm(span=100, adjust=False).mean()

    last = df.iloc[-1]
    is_bullish = last["close"] > vwap.iloc[-1] and ema15.iloc[-1] > ema100.iloc[-1]
    is_bearish = last["close"] < vwap.iloc[-1] and ema15.iloc[-1] < ema100.iloc[-1]

    if is_bullish:
        return "BULLISH"
    if is_bearish:
        return "BEARISH"
    return "NEUTRAL"
