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
    ma50_4h: float = 0.0


def macroscan_4h(df_4h: pd.DataFrame) -> bool:
    if df_4h is None or len(df_4h) < 50:
        return False

    last = df_4h.iloc[-1]
    close = last["close"]
    ma50 = last.get("MA50", 0)

    if pd.isna(ma50) or ma50 <= 0:
        return False

    # Condition a: Close > MA50 4H
    if close <= ma50:
        return False

    # Condition b: MA50[current] >= MA50[3 candles ago] (flattening or turning up)
    if len(df_4h) < 4:
        return False

    ma50_now = ma50
    ma50_3ago = df_4h.iloc[-4].get("MA50", 0)

    if pd.isna(ma50_3ago) or ma50_3ago <= 0:
        return False

    if ma50_now < ma50_3ago:
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

    ma20_curr = curr.get("MA20", 0)
    ma50_curr = curr.get("MA50", 0)
    ma20_prev = prev.get("MA20", 0)
    ma50_prev = prev.get("MA50", 0)

    if any(pd.isna(v) for v in [ma20_curr, ma50_curr, ma20_prev, ma50_prev]):
        return None

    # Condition a: Golden Cross — MA20 crosses above MA50
    if not (ma20_prev <= ma50_prev and ma20_curr > ma50_curr):
        return None

    # Calculate exact cross price via linear interpolation
    diff_prev = ma20_prev - ma50_prev
    diff_curr = ma20_curr - ma50_curr
    delta = diff_curr - diff_prev
    if delta == 0:
        return None
    ratio = -diff_prev / delta
    cross_price = ma20_prev + ratio * (ma20_curr - ma20_prev)
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

    # TP1 = MA50_4H (must already be merged into df_1h)
    ma50_4h = curr.get("MA50_4h", 0)
    if pd.isna(ma50_4h) or ma50_4h <= 0:
        return None
    tp1_price = ma50_4h

    return Signal(
        symbol=symbol,
        side="LONG",
        entry_price=cross_price,
        sl_price=sl_price,
        tp1_price=tp1_price,
        atr_1h=atr,
        timestamp=str(df_1h.index[-1]),
        reason="golden_cross_1h",
        ma50_4h=ma50_4h,
    )
