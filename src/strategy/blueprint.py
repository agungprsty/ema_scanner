import logging
from dataclasses import dataclass

import pandas as pd

from src.config.settings import (
    SIGNAL_COOLDOWN_CANDLES,
    GC_ATR_SL_MULTIPLIER, GC_LOOKBACK_CANDLES,
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
    ema20_4h: float = 0.0


def macroscan_4h(df_4h: pd.DataFrame) -> bool:
    if df_4h is None or len(df_4h) < 50:
        return False

    last = df_4h.iloc[-1]
    close = last["close"]
    ema20 = last.get("EMA20", 0)

    if pd.isna(ema20) or ema20 <= 0:
        return False

    # Condition a: Close > EMA20 4H
    if close <= ema20:
        return False

    # Condition b: EMA20[current] >= EMA20[3 candles ago] (flattening or turning up)
    if len(df_4h) < 4:
        return False

    ema20_now = ema20
    ema20_3ago = df_4h.iloc[-4].get("EMA20", 0)

    if pd.isna(ema20_3ago) or ema20_3ago <= 0:
        return False

    if ema20_now < ema20_3ago:
        return False

    return True


def check_entry(
    df_1h: pd.DataFrame,
    btc_bias: str,
    symbol: str,
    cooldown_map: dict[str, int] | None = None,
    current_index: int = 0,
) -> Signal | None:
    if df_1h is None or len(df_1h) < 50:
        return None

    if cooldown_map is not None:
        last_idx = cooldown_map.get(symbol, -SIGNAL_COOLDOWN_CANDLES)
        if current_index - last_idx < SIGNAL_COOLDOWN_CANDLES:
            return None

    if btc_bias == "BEARISH":
        return None

    curr = df_1h.iloc[-1]
    if len(df_1h) < 2:
        return None
    prev = df_1h.iloc[-2]

    close = curr["close"]
    volume = curr["volume"]

    ema5_curr = curr.get("EMA5", 0)
    ema20_curr = curr.get("EMA20", 0)
    ema5_prev = prev.get("EMA5", 0)
    ema20_prev = prev.get("EMA20", 0)

    if any(pd.isna(v) for v in [ema5_curr, ema20_curr, ema5_prev, ema20_prev]):
        return None

    # Condition a: Golden Cross — EMA5 crosses above EMA20
    if not (ema5_prev <= ema20_prev and ema5_curr > ema20_curr):
        return None

    # Calculate exact cross price via linear interpolation
    diff_prev = ema5_prev - ema20_prev
    diff_curr = ema5_curr - ema20_curr
    delta = diff_curr - diff_prev
    if delta == 0:
        return None
    ratio = -diff_prev / delta
    cross_price = ema5_prev + ratio * (ema5_curr - ema5_prev)
    if cross_price <= 0:
        return None

    # Condition b: Volume confirmation — volume > MA_Volume 20
    ma_vol = curr.get("SMA_VOL20", 0)
    if pd.isna(ma_vol) or ma_vol <= 0 or volume <= ma_vol:
        return None

    # SL: Lowest Low of 10 candles - 1.5 * ATR(14)
    lookback = min(GC_LOOKBACK_CANDLES, len(df_1h))
    lowest_low = df_1h["low"].iloc[-lookback:].min()
    atr = curr.get("ATR", 0)
    if pd.isna(atr) or atr <= 0:
        return None

    sl_price = lowest_low - GC_ATR_SL_MULTIPLIER * atr

    # TP1 = EMA20_4H (must already be merged into df_1h)
    ema20_4h = curr.get("EMA20_4h", 0)
    if pd.isna(ema20_4h) or ema20_4h <= 0:
        return None
    tp1_price = ema20_4h

    return Signal(
        symbol=symbol,
        side="LONG",
        entry_price=cross_price,
        sl_price=sl_price,
        tp1_price=tp1_price,
        atr_1h=atr,
        timestamp=str(df_1h.index[-1]),
        reason="golden_cross_1h",
        ema20_4h=ema20_4h,
    )
