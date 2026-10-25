import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.config.settings import (
    EMA_PERIOD,
    ROUND_TRIP_FEE_PERCENT,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_PERIOD,
    SL_PERCENT,
    SUPERTREND_ATR_PERIOD,
    SUPERTREND_FACTOR,
    TP_PERCENT,
)


@dataclass
class Signal:
    symbol: str
    side: str
    signal_candle_ms: int
    rsi: float
    prev_rsi: float
    candle_close: float
    entry_price: float
    take_profit: float
    stop_loss: float
    bars_ago: int  # alert text only


def compute_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder RSI, identical to backtest.calculate_rsi; NaN until `period` deltas are seen."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_ema(close: pd.Series, period: int = EMA_PERIOD) -> pd.Series:
    """EMA of the close, NaN until `period` candles are seen; also drawn on the signal chart."""
    return close.ewm(span=period, min_periods=period, adjust=False).mean()


def compute_supertrend(
    df: pd.DataFrame, period: int = SUPERTREND_ATR_PERIOD, factor: float = SUPERTREND_FACTOR
) -> tuple[pd.Series, pd.Series]:
    """(line, uptrend) as TradingView's ta.supertrend: hl2 bands at `factor` Wilder ATRs that only
    tighten while the trend holds. `uptrend` is 1.0 or 0.0, NaN until the ATR is defined; the first
    defined candle starts as a downtrend, like TradingView, so it is never a flip.

    The ATR is seeded from the first true range rather than TradingView's SMA, like compute_rsi;
    the gap decays by (1 - 1/period) per candle, so it is gone long before the scanned candles end.
    """
    high, low, close = (df[c].to_numpy(dtype=float) for c in ("high", "low", "close"))
    prev_close = np.concatenate(([np.nan], close[:-1]))
    # fmax skips the NaN previous close of the first candle, leaving high - low as TradingView does
    tr = np.fmax(high - low, np.fmax(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = pd.Series(tr).ewm(alpha=1 / period, min_periods=period, adjust=False).mean().to_numpy()
    upper, lower = (high + low) / 2 + factor * atr, (high + low) / 2 - factor * atr
    line, up = np.full(len(df), np.nan), np.full(len(df), np.nan)
    for i in range(len(df)):
        if math.isnan(atr[i]):
            continue
        if i == 0 or math.isnan(up[i - 1]):
            up[i] = 0.0
        else:
            if not (lower[i] > lower[i - 1] or close[i - 1] < lower[i - 1]):
                lower[i] = lower[i - 1]
            if not (upper[i] < upper[i - 1] or close[i - 1] > upper[i - 1]):
                upper[i] = upper[i - 1]
            # Against this candle's bands, not the previous ones
            up[i] = float(close[i] > upper[i]) if up[i - 1] == 0 else float(close[i] >= lower[i])
        line[i] = lower[i] if up[i] else upper[i]
    return pd.Series(line, index=df.index), pd.Series(up, index=df.index)


def levels(side: str, entry: float) -> tuple[float, float]:
    """Return (take_profit, stop_loss) for a net TP_PERCENT gain or SL_PERCENT loss after fees.

    The fees are paid on both exits, so the target moves out by them and the stop moves in.
    """
    tp = (TP_PERCENT + ROUND_TRIP_FEE_PERCENT) / 100
    sl = (SL_PERCENT - ROUND_TRIP_FEE_PERCENT) / 100
    if side == "LONG":
        return entry * (1 + tp), entry * (1 - sl)
    return entry * (1 - tp), entry * (1 + sl)


def detect_signals(df: pd.DataFrame, symbol: str, lookback: int) -> list[Signal]:
    """Return every RSI cross in the last `lookback` rows of `df`, oldest first.

    `df` must hold closed candles only, since the limit entry is the signal candle's low or
    high and that is not known while the candle is still forming. Warm-up rows have a NaN RSI,
    which compares False, so they never signal.
    """
    if df is None or len(df) < 2:
        return []

    rsi = compute_rsi(df["close"])
    last = len(df) - 1
    signals = []
    for i in range(max(1, len(df) - lookback), len(df)):
        prev, curr = rsi.iloc[i - 1], rsi.iloc[i]
        if prev > RSI_OVERSOLD and curr <= RSI_OVERSOLD:
            side, entry = "LONG", float(df["low"].iloc[i])
        elif prev < RSI_OVERBOUGHT and curr >= RSI_OVERBOUGHT:
            side, entry = "SHORT", float(df["high"].iloc[i])
        else:
            continue
        take_profit, stop_loss = levels(side, entry)
        signals.append(
            Signal(
                symbol=symbol,
                side=side,
                signal_candle_ms=int(df["timestamp"].iloc[i].timestamp() * 1000),
                rsi=float(curr),
                prev_rsi=float(prev),
                candle_close=float(df["close"].iloc[i]),
                entry_price=entry,
                take_profit=take_profit,
                stop_loss=stop_loss,
                bars_ago=last - i,
            )
        )
    return signals
