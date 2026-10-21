from dataclasses import dataclass

import pandas as pd

from src.config.settings import RSI_OVERBOUGHT, RSI_OVERSOLD, RSI_PERIOD, SL_PERCENT, TP_PERCENT


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


def levels(side: str, entry: float) -> tuple[float, float]:
    """Return (take_profit, stop_loss) placed TP_PERCENT / SL_PERCENT away from the limit entry."""
    if side == "LONG":
        return entry * (1 + TP_PERCENT / 100), entry * (1 - SL_PERCENT / 100)
    return entry * (1 - TP_PERCENT / 100), entry * (1 + SL_PERCENT / 100)


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
