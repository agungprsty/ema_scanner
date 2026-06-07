import pandas as pd
import pandas_ta as ta


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame | None:
    df = df.copy()

    # Use raw ta functions for single-column results to avoid the .ta
    # accessor's _post_process which returns the entire DataFrame when
    # there is insufficient data (e.g., rows < length).
    ema15 = ta.ema(df["close"], length=15)
    ema100 = ta.ema(df["close"], length=100)
    atr = ta.atr(df["high"], df["low"], df["close"], length=14)
    rsi = ta.rsi(df["close"], length=14)

    # If any indicator fails, return None
    if ema15 is None or ema100 is None or atr is None or rsi is None:
        return None

    df["EMA15"] = ema15
    df["EMA100"] = ema100
    df["ATR"] = atr
    df["RSI"] = rsi

    macd = df.ta.macd(fast=12, slow=26, signal=9)
    adx_df = df.ta.adx(length=14)
    df["SMA_VOL20"] = df["volume"].rolling(20).mean()

    df = pd.concat([df, macd, adx_df], axis=1)

    df = df.dropna(subset=[
        "EMA15", "EMA100", "ATR", "RSI", "SMA_VOL20",
        "ADX_14", "MACD_12_26_9", "MACDs_12_26_9",
    ]).reset_index(drop=True)

    return df
