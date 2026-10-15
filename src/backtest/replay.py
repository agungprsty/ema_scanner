from collections.abc import Mapping
from dataclasses import dataclass, replace

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
from src.strategy.base import ResolveContext, Strategy
from src.strategy.context import MarketContext, normalize_klines, timeframe_ms
from src.strategy.macd import MacdSignal, compute_macd, detect_macd_cross
from src.strategy.macd_cross import macd_cross_candidates
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
    """Positions in a compute_macd frame whose candle crosses (see macd_cross_candidates)."""
    return macd_cross_candidates(df)


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


def _resolve_context(
    strategy: Strategy, symbol: str, base: pd.DataFrame, k: int, resolve_ctx: ResolveContext
) -> ResolveContext:
    """`resolve_ctx` plus, for a strategy whose exit reads indicator history, the candles the live
    evaluator fetches up to the cross candle at position `k`: resolve_history_bars of them, then the cross."""
    history = strategy.resolve_history_bars
    if not history:
        return resolve_ctx
    window = base.iloc[max(0, k - history) : k + 1]
    decision_ms = int(base["open_time_ms"].iat[k]) + resolve_ctx.interval_ms
    return replace(resolve_ctx, history=MarketContext(symbol, decision_ms, strategy.timeframe, {strategy.timeframe: window}))


def replay_strategy(
    strategy: Strategy,
    klines: pd.DataFrame,
    symbol: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    warmup: int | None = None,
    context: Mapping[str, pd.DataFrame] | None = None,
    candidates: bool = True,
) -> SymbolReplay:
    """Every trade `strategy` would have booked on `symbol`, through its own detect() and resolve().

    After each closed candle k of the strategy's timeframe the strategy decides on a MarketContext as
    of that close (closed candles of every timeframe only, the next candle's open as the entry
    price), exactly as a scheduled scan would right after the close, re-checking that one candle.
    A signal counts when its candle opens in [start_ms, end_ms) and has at least `warmup` candles
    before it (default: the strategy's warmup_bars). resolve() then settles it from the candles
    after it, up to max_hold_bars; one the data cannot settle yet is counted as unresolved.

    With `candidates`, the strategy's candidate_bars() narrows the closes detect() runs after; it
    must be a superset of where detect() fires (test_strategy_framework checks it for macd_cross).
    `context` holds normalized klines of the strategy's context timeframes, e.g. 1d resampled from
    the 2h series; MarketContext never lets a decision see a candle closing after it.
    """
    interval_ms = timeframe_ms(strategy.timeframe)
    warmup = strategy.warmup_bars if warmup is None else warmup
    base = normalize_klines(klines)
    if len(base) < 3:
        return SymbolReplay(symbol, [], 0)
    frames = {strategy.timeframe: base, **(context or {})}
    missing = [tf for tf in strategy.context_timeframes if tf not in frames]
    if missing:
        raise ValueError(f"{strategy.ref} needs {missing} context klines for {symbol}")
    open_ms = base["open_time_ms"].to_numpy()
    rows = base[["open_time_ms", "open", "high", "low", "close"]].to_numpy(dtype=object).tolist()
    # Historical candles have all closed; the last one closes one interval after it opens
    base_resolve_ctx = ResolveContext(now_ms=int(rows[-1][0]) + interval_ms, interval_ms=interval_ms)
    positions = strategy.candidate_bars(base) if candidates else None
    if positions is None:
        positions = range(len(base))

    trades: list[BacktestTrade] = []
    unresolved = 0
    for k in positions:
        k = int(k)
        # The entry candle must exist: it is the one opening as the decision is made
        if k + 1 >= len(base) or k < warmup:
            continue
        bar_ms = int(open_ms[k])
        if (start_ms is not None and bar_ms < start_ms) or (end_ms is not None and bar_ms >= end_ms):
            continue
        ctx = MarketContext(
            symbol=symbol,
            decision_ms=bar_ms + interval_ms,
            base_timeframe=strategy.timeframe,
            frames=frames,
            next_open=float(rows[k + 1][1]),
            catchup_bars=1,
        )
        for sig in strategy.detect(ctx):
            if sig.cross_candle_ms != bar_ms:
                continue
            resolve_ctx = _resolve_context(strategy, symbol, base, k, base_resolve_ctx)
            outcome = strategy.resolve(sig, rows[k + 1 : k + 1 + strategy.max_hold_bars], resolve_ctx)
            if outcome is None:
                unresolved += 1
                continue
            trades.append(
                BacktestTrade(
                    symbol=symbol,
                    side=sig.side,
                    cross_candle_ms=sig.cross_candle_ms,
                    entry_time_ms=int(rows[k + 1][0]),
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    tp1=sig.tp1,
                    result=outcome.result,
                    exit_price=outcome.exit_price,
                    exit_time_ms=outcome.exit_time_ms,
                )
            )
    return SymbolReplay(symbol, trades, unresolved)


def replay_symbol(
    klines: pd.DataFrame,
    symbol: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    warmup: int = PRODUCTION_WARMUP,
) -> SymbolReplay:
    """Every trade production would have booked on `symbol`, one per closed-candle cross.

    The Phase 0 replay of the MACD rules with the environment's parameters, kept as the reference
    that replay_strategy(macd_cross@2.2.0) is proven against (test_strategy_framework).

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
