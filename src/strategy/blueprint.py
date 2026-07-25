import logging
from dataclasses import dataclass

import pandas as pd

from src.config.settings import (
    LONG_ATR_SL_MULTIPLIER, LONG_LOOKBACK_CANDLES,
    SHORT_ATR_SL_MULTIPLIER, SHORT_LOOKBACK_CANDLES,
)

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol: str
    side: str
    entry_price: float
    sl_price: float
    tp1_price: float
    atr_1h: float
    timestamp: str
    reason: str
    ema50_4h: float = 0.0
    cross_candle_ms: int = 0


def macroscan_4h(df_4h: pd.DataFrame) -> str:
    if df_4h is None or len(df_4h) < 50:
        return "NEUTRAL"

    last = df_4h.iloc[-1]
    close = last["close"]
    ema50 = last.get("EMA50", 0)

    if pd.isna(ema50) or ema50 <= 0 or len(df_4h) < 4:
        return "NEUTRAL"

    ema50_now = ema50
    ema50_3ago = df_4h.iloc[-4].get("EMA50", 0)

    if pd.isna(ema50_3ago) or ema50_3ago <= 0:
        return "NEUTRAL"

    if close > ema50 and ema50_now > ema50_3ago:
        return "BULLISH"

    if close < ema50 and ema50_now < ema50_3ago:
        return "BEARISH"

    return "NEUTRAL"


def check_entry(
    df_1h: pd.DataFrame,
    btc_bias: str,
    symbol: str,
    macro_bias: str,
    lookback_candles: int = 5,
) -> Signal | None:
    if df_1h is None or len(df_1h) < 50:
        return None

    if macro_bias == "NEUTRAL":
        return None

    scan_long = macro_bias == "BULLISH"
    scan_short = macro_bias == "BEARISH"

    n = min(lookback_candles, len(df_1h) - 1)
    for i in range(len(df_1h) - 1, len(df_1h) - n - 1, -1):
        curr = df_1h.iloc[i]
        prev = df_1h.iloc[i - 1]

        volume = curr["volume"]
        ema20_curr = curr.get("EMA20", 0)
        ema50_curr = curr.get("EMA50", 0)
        ema20_prev = prev.get("EMA20", 0)
        ema50_prev = prev.get("EMA50", 0)

        if any(pd.isna(v) for v in [ema20_curr, ema50_curr, ema20_prev, ema50_prev]):
            continue

        diff_prev = ema20_prev - ema50_prev
        diff_curr = ema20_curr - ema50_curr
        delta = diff_curr - diff_prev
        if delta == 0:
            continue
        ratio = -diff_prev / delta
        cross_price = ema20_prev + ratio * (ema20_curr - ema20_prev)
        if cross_price <= 0:
            continue

        ma_vol = curr.get("SMA_VOL20", 0)
        if pd.isna(ma_vol) or ma_vol <= 0 or volume <= ma_vol:
            continue

        atr = df_1h.iloc[-1].get("ATR", 0)
        if pd.isna(atr) or atr <= 0:
            continue

        ema50_4h = curr.get("EMA50_4h", 0)
        if pd.isna(ema50_4h) or ema50_4h <= 0:
            continue

        cross_ms = int(curr["timestamp"].timestamp() * 1000)

        if scan_long:
            if ema20_prev <= ema50_prev and ema20_curr > ema50_curr:
                if btc_bias == "BEARISH":
                    continue

                lookback_sl = min(LONG_LOOKBACK_CANDLES, len(df_1h))
                lowest_low = df_1h["low"].iloc[-lookback_sl:].min()
                sl_price = lowest_low - LONG_ATR_SL_MULTIPLIER * atr

                return Signal(
                    symbol=symbol,
                    side="LONG",
                    entry_price=cross_price,
                    sl_price=sl_price,
                    tp1_price=0,
                    atr_1h=atr,
                    timestamp=str(curr.name),
                    reason="golden_cross_1h",
                    ema50_4h=ema50_4h,
                    cross_candle_ms=cross_ms,
                )

        if scan_short:
            if ema20_prev >= ema50_prev and ema20_curr < ema50_curr:
                if btc_bias == "BULLISH":
                    continue

                lookback_sl = min(SHORT_LOOKBACK_CANDLES, len(df_1h))
                highest_high = df_1h["high"].iloc[-lookback_sl:].max()
                sl_price = highest_high + SHORT_ATR_SL_MULTIPLIER * atr

                return Signal(
                    symbol=symbol,
                    side="SHORT",
                    entry_price=cross_price,
                    sl_price=sl_price,
                    tp1_price=0,
                    atr_1h=atr,
                    timestamp=str(curr.name),
                    reason="death_cross_1h",
                    ema50_4h=ema50_4h,
                    cross_candle_ms=cross_ms,
                )

    return None
