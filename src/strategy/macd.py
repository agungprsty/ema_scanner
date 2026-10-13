import logging
from dataclasses import asdict, dataclass

import pandas as pd
import pandas_ta as ta

from src.config.settings import (
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    RISK_REWARD_RATIO,
    SWING_LOOKBACK_CANDLES,
)

logger = logging.getLogger(__name__)


@dataclass
class MacdSignal:
    symbol: str
    side: str
    reason: str
    cross_candle_ms: int
    cross_time: str
    cross_price: float
    entry_price: float
    stop_loss: float
    tp1: float
    risk_reward_ratio: float
    macd: float
    signal: float
    histogram: float
    close: float
    bars_ago: int
    is_confirmed: bool = True
    win_status: str = "PENDING"

    def to_dict(self) -> dict:
        return asdict(self)


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


def calculate_trade_levels(
    df: pd.DataFrame,
    idx: int,
    side: str,
    entry_price: float,
    lookback_candles: int = SWING_LOOKBACK_CANDLES,
    rr_ratio: float = RISK_REWARD_RATIO,
) -> tuple[float, float]:
    start_idx = max(0, idx - lookback_candles)
    window = df.iloc[start_idx : idx + 1]

    if side == "LONG":
        swing_low = float(window["low"].min()) if "low" in window else entry_price * 0.985
        stop_loss = swing_low * 0.998
        if stop_loss >= entry_price:
            stop_loss = entry_price * 0.985
        risk = entry_price - stop_loss
        tp1 = entry_price + (risk * rr_ratio)
    else:
        swing_high = float(window["high"].max()) if "high" in window else entry_price * 1.015
        stop_loss = swing_high * 1.002
        if stop_loss <= entry_price:
            stop_loss = entry_price * 1.015
        risk = stop_loss - entry_price
        tp1 = entry_price - (risk * rr_ratio)

    return round(stop_loss, 6), round(tp1, 6)


def detect_macd_cross(
    df: pd.DataFrame,
    symbol: str,
    lookback_candles: int = 5,
    include_current: bool = False,
) -> MacdSignal | None:
    """Find the most recent MACD cross in the last `lookback_candles` closed candles.

    `df` must end with the candle that is still forming, as Binance klines do. A cross on a
    closed candle enters at the open of the next candle, the first price available once the
    cross is confirmed. With `include_current`, a cross on the forming candle is reported too,
    priced at its latest close and marked unconfirmed because it can still disappear.
    """
    if df is None or len(df) < 3:
        return None

    forming_idx = len(df) - 1
    last_closed = forming_idx - 1
    newest = forming_idx if include_current else last_closed
    oldest = max(1, last_closed - lookback_candles + 1)

    for i in range(newest, oldest - 1, -1):
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

        is_confirmed = i != forming_idx
        cross_price = round(_calc_cross_price(df, i), 6)
        entry_price = float(df.iloc[i + 1]["open"]) if is_confirmed else float(curr["close"])

        stop_loss, tp1 = calculate_trade_levels(
            df=df,
            idx=i,
            side=side,
            entry_price=entry_price,
            lookback_candles=SWING_LOOKBACK_CANDLES,
            rr_ratio=RISK_REWARD_RATIO,
        )

        return MacdSignal(
            symbol=symbol,
            side=side,
            reason=reason,
            cross_candle_ms=int(curr["timestamp"].timestamp() * 1000),
            cross_time=curr["timestamp"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            cross_price=cross_price,
            entry_price=round(entry_price, 6),
            stop_loss=stop_loss,
            tp1=tp1,
            risk_reward_ratio=RISK_REWARD_RATIO,
            macd=float(macd_curr),
            signal=float(sig_curr),
            histogram=float(macd_curr - sig_curr),
            close=float(curr["close"]),
            bars_ago=last_closed - i if is_confirmed else 0,
            is_confirmed=is_confirmed,
            win_status="PENDING",
        )

    return None
