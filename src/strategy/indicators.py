import pandas as pd
import pandas_ta as ta


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame | None:
    df = df.copy()

    ema5 = ta.ema(df["close"], length=5)
    ema20 = ta.ema(df["close"], length=20)
    atr = ta.atr(df["high"], df["low"], df["close"], length=14)
    rsi = ta.rsi(df["close"], length=14)

    if ema5 is None or ema20 is None or atr is None or rsi is None:
        return None

    df["EMA5"] = ema5
    df["EMA20"] = ema20
    df["ATR"] = atr
    df["RSI"] = rsi

    adx_df = df.ta.adx(length=14)
    df["SMA_VOL20"] = df["volume"].rolling(20).mean()
    df["VOLUME_RATIO"] = df["volume"] / df["SMA_VOL20"]
    df["ATR_PERCENT"] = df["ATR"] / df["close"] * 100

    df = pd.concat([df, adx_df], axis=1)

    df = df.dropna(subset=[
        "EMA5", "EMA20", "ATR", "RSI", "SMA_VOL20",
        "ADX_14", "VOLUME_RATIO", "ATR_PERCENT",
    ]).reset_index(drop=True)

    return df
