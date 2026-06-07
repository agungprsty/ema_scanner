import logging
from dataclasses import dataclass

import pandas as pd

from src.config.settings import (
    ADX_MIN, SIGNAL_COOLDOWN_CANDLES,
    ATR_SL_MULTIPLIER, ATR_TP_MULTIPLIER,
    PULLBACK_EMA_LENGTH,
    RSI_LONG_MAX, RSI_LONG_MIN, RSI_SHORT_MIN, RSI_SHORT_MAX,
    PULLBACK_DISTANCE_PCT, VOLUME_RATIO_MAX,
)

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol: str
    side: str
    entry_price: float
    sl_price: float
    tp_price: float
    atr: float
    timestamp: str
    reason: str


def _detect_reversal_candle(row: dict, prev: dict, side: str) -> bool:
    if side == "LONG":
        return row["close"] > row["open"]
    else:
        return row["close"] < row["open"]


def hard_filter_checklist(
    df: pd.DataFrame,
    btc_bias: str,
    btc_strength: float,
    symbol: str,
    df_macro: pd.DataFrame | None = None,
    cooldown_map: dict[str, int] | None = None,
    current_index: int = 0,
) -> Signal | None:
    if df is None or len(df) < 50:
        return None

    if cooldown_map is not None:
        last_idx = cooldown_map.get(symbol, -SIGNAL_COOLDOWN_CANDLES)
        if current_index - last_idx < SIGNAL_COOLDOWN_CANDLES:
            return None

    curr = df.iloc[-1]
    if len(df) >= 2:
        prev = df.iloc[-2]
    else:
        return None

    close = curr["close"]
    ema_pullback = curr.get(f"EMA{PULLBACK_EMA_LENGTH}", 0)
    atr = curr.get("ATR", 0)
    rsi = curr.get("RSI", 50)
    adx = curr.get("ADX_14", 0)
    vol_ratio = curr.get("VOLUME_RATIO", 0)

    if any(pd.isna(v) for v in [ema_pullback, atr, rsi, adx]):
        return None

    long_passed = _check_long(
        close, ema_pullback, rsi, adx, vol_ratio,
        btc_bias, btc_strength, curr, prev,
    )
    if long_passed:
        sl = close - atr * ATR_SL_MULTIPLIER
        tp = close + atr * ATR_TP_MULTIPLIER
        return Signal(
            symbol=symbol, side="LONG",
            entry_price=close, sl_price=sl, tp_price=tp,
            atr=atr, timestamp=str(df.index[-1]),
            reason="hard_filter_long",
        )

    short_passed = _check_short(
        close, ema_pullback, rsi, adx, vol_ratio,
        btc_bias, btc_strength, curr, prev,
    )
    if short_passed:
        sl = close + atr * ATR_SL_MULTIPLIER
        tp = close - atr * ATR_TP_MULTIPLIER
        return Signal(
            symbol=symbol, side="SHORT",
            entry_price=close, sl_price=sl, tp_price=tp,
            atr=atr, timestamp=str(df.index[-1]),
            reason="hard_filter_short",
        )

    return None


def _check_long(
    close: float, ema_pb: float,
    rsi: float, adx: float, vol_ratio: float,
    btc_bias: str, btc_strength: float,
    curr: pd.Series, prev: pd.Series,
) -> bool:
    if btc_bias == "BEARISH":
        return False

    # Layer 1 — 4h MACRO: EMA50 > EMA200
    ema50_4h = curr.get("EMA50_4h")
    ema200_4h = curr.get("EMA200_4h")
    if any(pd.isna(v) for v in [ema50_4h, ema200_4h]):
        return False
    if ema50_4h <= ema200_4h:
        return False

    # Layer 2 — 1h TREND: close > EMA20
    close_1h = curr.get("close_1h")
    ema20_1h = curr.get("EMA20_1h")
    if any(pd.isna(v) for v in [close_1h, ema20_1h]):
        return False
    if close_1h <= ema20_1h:
        return False

    # Layer 3 — 15m ENTRY: pullback ke EMA20
    dist_to_ema = (ema_pb - close) / ema_pb * 100
    if not (0 <= dist_to_ema <= PULLBACK_DISTANCE_PCT):
        return False
    if not (RSI_LONG_MIN <= rsi <= RSI_LONG_MAX):
        return False
    if vol_ratio > VOLUME_RATIO_MAX:
        return False
    if adx < ADX_MIN:
        return False
    if not _detect_reversal_candle(dict(curr), dict(prev), "LONG"):
        return False
    return True


def _check_short(
    close: float, ema_pb: float,
    rsi: float, adx: float, vol_ratio: float,
    btc_bias: str, btc_strength: float,
    curr: pd.Series, prev: pd.Series,
) -> bool:
    if btc_bias == "BULLISH":
        return False

    # Layer 1 — 4h MACRO: EMA50 < EMA200
    ema50_4h = curr.get("EMA50_4h")
    ema200_4h = curr.get("EMA200_4h")
    if any(pd.isna(v) for v in [ema50_4h, ema200_4h]):
        return False
    if ema50_4h >= ema200_4h:
        return False

    # Layer 2 — 1h TREND: close < EMA20
    close_1h = curr.get("close_1h")
    ema20_1h = curr.get("EMA20_1h")
    if any(pd.isna(v) for v in [close_1h, ema20_1h]):
        return False
    if close_1h >= ema20_1h:
        return False

    # Layer 3 — 15m ENTRY: pullback up to EMA20
    dist_to_ema = (close - ema_pb) / ema_pb * 100
    if not (0 <= dist_to_ema <= PULLBACK_DISTANCE_PCT):
        return False
    if not (RSI_SHORT_MIN <= rsi <= RSI_SHORT_MAX):
        return False
    if vol_ratio > VOLUME_RATIO_MAX:
        return False
    if adx < ADX_MIN:
        return False
    if not _detect_reversal_candle(dict(curr), dict(prev), "SHORT"):
        return False
    return True
