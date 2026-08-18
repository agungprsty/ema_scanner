import logging
from dataclasses import dataclass

import pandas as pd
import pandas_ta as ta

from src.config.settings import MACD_FAST, MACD_SIGNAL, MACD_SLOW

logger = logging.getLogger(__name__)


@dataclass
class MacdSignal:
    symbol: str
    side: str
    reason: str
    cross_candle_ms: int
    cross_time: str
    cross_price: float
    macd: float
    signal: float
    histogram: float
    close: float
    bars_ago: int
    is_confirmed: bool = True


def compute_macd(
    df: pd.DataFrame,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> pd.DataFrame | None:
    macd_df = ta.macd(df["close"], fast=fast, slow=slow, signal=signal)
    if macd_df is None:
        return None

    df = df.copy()
    df = pd.concat([df, macd_df], axis=1)
    df = df.dropna(subset=[f"MACD_{fast}_{slow}_{signal}"]).reset_index(drop=True)
    df = df.rename(
        columns={
            f"MACD_{fast}_{slow}_{signal}": "MACD",
            f"MACDh_{fast}_{slow}_{signal}": "MACDh",
            f"MACDs_{fast}_{slow}_{signal}": "MACDs",
        }
    )
    return df


def _calc_cross_price(df: pd.DataFrame, idx: int) -> float:
    curr = df.iloc[idx]
    prev = df.iloc[idx - 1]
    macd_curr = curr.get("MACD", 0)
    macd_prev = prev.get("MACD", 0)
    sig_curr = curr.get("MACDs", 0)
    sig_prev = prev.get("MACDs", 0)
    delta_macd = macd_curr - macd_prev
    delta_sig = sig_curr - sig_prev
    if delta_macd == delta_sig:
        return float(curr["close"])
    ratio = (sig_prev - macd_prev) / (delta_macd - delta_sig)
    price = prev["close"] + ratio * (curr["close"] - prev["close"])
    return float(price) if price > 0 else float(curr["close"])


def detect_macd_cross(
    df: pd.DataFrame,
    symbol: str,
    lookback_candles: int = 5,
    include_current: bool = False,
) -> MacdSignal | None:
    if df is None or len(df) < 2:
        return None

    last_idx = len(df) - 1
    n = min(lookback_candles, len(df) - 1)
    for i in range(len(df) - 1, len(df) - n - 1, -1):
        curr = df.iloc[i]
        prev = df.iloc[i - 1]

        macd_curr = curr.get("MACD", 0)
        macd_prev = prev.get("MACD", 0)
        sig_curr = curr.get("MACDs", 0)
        sig_prev = prev.get("MACDs", 0)

        if any(pd.isna(v) for v in [macd_curr, macd_prev, sig_curr, sig_prev]):
            continue

        if macd_prev <= sig_prev and macd_curr > sig_curr:
            side, reason = "LONG", "macd_golden_cross"
        elif macd_prev >= sig_prev and macd_curr < sig_curr:
            side, reason = "SHORT", "macd_death_cross"
        else:
            continue

        is_confirmed = not (include_current and i == last_idx)

        return MacdSignal(
            symbol=symbol,
            side=side,
            reason=reason,
            cross_candle_ms=int(curr["timestamp"].timestamp() * 1000),
            cross_time=curr["timestamp"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            cross_price=_calc_cross_price(df, i),
            macd=float(macd_curr),
            signal=float(sig_curr),
            histogram=float(macd_curr - sig_curr),
            close=float(curr["close"]),
            bars_ago=last_idx - i,
            is_confirmed=is_confirmed,
        )

    return None
