from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.config.settings import (
    MACD_CANDLES,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    MACD_TIMEFRAME,
    SIGNAL_EXPIRY_CANDLES,
)
from src.strategy.macd import MacdSignal, compute_macd, detect_macd_cross
from src.strategy.outcome import CANDLE_INTERVAL_MS, resolve_outcome

# A scheduled scan fetches MACD_CANDLES klines, the last still forming, so the newest closed
# candle it can see a cross on has MACD_CANDLES - 2 candles of history before it
PRODUCTION_WARMUP = MACD_CANDLES - 2


@dataclass(frozen=True)
class BacktestTrade:
    symbol: str
    side: str
    cross_candle_ms: int
    entry_time_ms: int
    entry_price: float
    stop_loss: float
    tp1: float
    result: str  # PROFIT, LOSS or EXPIRED
    exit_price: float
    exit_time_ms: int


@dataclass
class SymbolReplay:
    symbol: str
    trades: list[BacktestTrade]
    # Crosses in the window whose outcome the data cannot settle yet (too close to its end)
    unresolved: int


def cross_candidates(df: pd.DataFrame) -> np.ndarray:
    """Positions in a compute_macd frame whose candle crosses, by the same comparisons as
    detect_macd_cross. NaN compares false, which matches the scan skipping NaN rows."""
    macd = df["MACD"].to_numpy(dtype=float)
    sig = df["MACDs"].to_numpy(dtype=float)
    up = (macd[:-1] <= sig[:-1]) & (macd[1:] > sig[1:])
    down = (macd[:-1] >= sig[:-1]) & (macd[1:] < sig[1:])
    return np.flatnonzero(up | down) + 1


def replay_signals(
    df: pd.DataFrame,
    symbol: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    warmup: int = PRODUCTION_WARMUP,
    dropped: int = 0,
) -> list[tuple[int, MacdSignal]]:
    """(position, signal) for every closed-candle cross in a compute_macd frame, oldest first.

    Each cross is priced by detect_macd_cross itself on the frame cut just after the cross
    candle, so the candle after it is the forming one, as in the scheduled scan that first sees
    it. A cross counts when its candle opens in [start_ms, end_ms) and has at least `warmup`
    candles before it in the original klines, of which compute_macd `dropped` the first ones.
    """
    signals = []
    for i in cross_candidates(df):
        i = int(i)
        # The entry candle must exist: it is the forming candle when the cross is detected
        if i + 1 >= len(df) or i + dropped < warmup:
            continue
        cross_ms = int(df["open_time_ms"].iat[i])
        if (start_ms is not None and cross_ms < start_ms) or (end_ms is not None and cross_ms >= end_ms):
            continue
        sig = detect_macd_cross(df.iloc[: i + 2], symbol, lookback_candles=1, include_current=False)
        if sig is not None and sig.cross_candle_ms == cross_ms:
            signals.append((i, sig))
    return signals


def replay_symbol(
    klines: pd.DataFrame,
    symbol: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    warmup: int = PRODUCTION_WARMUP,
) -> SymbolReplay:
    """Every trade production would have booked on `symbol`, one per closed-candle cross.

    MACD is computed once over the whole series. With PRODUCTION_WARMUP candles of history the
    seed of each EMA has decayed below float precision, so the values equal what a scan over the
    last MACD_CANDLES klines computes (test_backtest proves the parity). Each signal is settled by
    resolve_outcome on the SIGNAL_EXPIRY_CANDLES candles after its cross candle, as
    evaluate_pending_signals does; one the data cannot settle yet is counted as unresolved.
    """
    interval_ms = CANDLE_INTERVAL_MS[MACD_TIMEFRAME]
    df = compute_macd(klines, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    if df is None or len(df) < 3:
        return SymbolReplay(symbol, [], 0)
    # compute_macd drops the leading candles without a MACD value
    dropped = len(klines) - len(df)
    rows = df[["open_time_ms", "open", "high", "low", "close"]].to_numpy(dtype=object).tolist()
    # Historical candles have all closed; the last one closes one interval after it opens
    now_ms = int(rows[-1][0]) + interval_ms

    trades: list[BacktestTrade] = []
    unresolved = 0
    for i, sig in replay_signals(df, symbol, start_ms, end_ms, warmup, dropped):
        outcome = resolve_outcome(
            sig.side,
            sig.stop_loss,
            sig.tp1,
            rows[i + 1 : i + 1 + SIGNAL_EXPIRY_CANDLES],
            SIGNAL_EXPIRY_CANDLES,
            now_ms,
            interval_ms,
        )
        if outcome is None:
            unresolved += 1
            continue
        trades.append(
            BacktestTrade(
                symbol=symbol,
                side=sig.side,
                cross_candle_ms=sig.cross_candle_ms,
                entry_time_ms=int(rows[i + 1][0]),
                entry_price=sig.entry_price,
                stop_loss=sig.stop_loss,
                tp1=sig.tp1,
                result=outcome.result,
                exit_price=outcome.exit_price,
                exit_time_ms=outcome.exit_time_ms,
            )
        )
    return SymbolReplay(symbol, trades, unresolved)
