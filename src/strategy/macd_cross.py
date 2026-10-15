"""macd_cross@2.2.0: the production MACD cross rules, ported onto the Strategy interface unchanged.

Detection is compute_macd + detect_macd_cross (cross on a closed 2h candle, entry at the next open,
stop beyond the swing of the cross candle and the 5 before it, target at 1.5R) and the exit is
resolve_outcome (stop first when a candle touches both levels, otherwise target, else EXPIRED at the
close of the 48th candle). The parameters are frozen here rather than read from the environment, so
an env override can no longer change a version that is already in the ledger.
"""

import numpy as np
import pandas as pd

from src.strategy.base import ResolveContext, SignalIntent, Strategy, StrategyParams, TradeLevels
from src.strategy.context import MarketContext
from src.strategy.macd import compute_macd, detect_macd_cross
from src.strategy.outcome import TradeOutcome, resolve_outcome


class MacdCrossParams(StrategyParams):
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    # Klines a live scan fetches, the forming one included
    macd_candles: int = 500
    risk_reward_ratio: float = 1.5
    swing_lookback_candles: int = 5
    signal_expiry_candles: int = 48


def macd_cross_candidates(macd_df: pd.DataFrame) -> np.ndarray:
    """Positions in a compute_macd frame whose candle crosses, by the same comparisons as
    detect_macd_cross. NaN compares false, which matches the scan skipping NaN rows."""
    macd = macd_df["MACD"].to_numpy(dtype=float)
    sig = macd_df["MACDs"].to_numpy(dtype=float)
    up = (macd[:-1] <= sig[:-1]) & (macd[1:] > sig[1:])
    down = (macd[:-1] >= sig[:-1]) & (macd[1:] < sig[1:])
    return np.flatnonzero(up | down) + 1


class MacdCross(Strategy):
    key = "macd_cross"
    version = "2.2.0"
    name = "MACD cross (2h)"
    description = (
        "MACD(12,26,9) cross on a closed 2h candle, entry at the next open, swing stop, fixed 1.5R "
        "target, stop first on an ambiguous candle, expiry after 48 candles."
    )
    status = "live"
    timeframe = "2h"
    code_modules = ("src.strategy.macd", "src.strategy.outcome")
    params_model = MacdCrossParams

    @property
    def warmup_bars(self) -> int:
        # A live scan fetches macd_candles klines, the last still forming, so the newest closed
        # candle it can see a cross on has macd_candles - 2 candles of history before it
        return self.params.macd_candles - 2

    @property
    def history_bars(self) -> int:
        return self.params.macd_candles

    @property
    def max_hold_bars(self) -> int:
        return self.params.signal_expiry_candles

    def detect(self, ctx: MarketContext) -> list[SignalIntent]:
        p = self.params
        df = compute_macd(ctx.bars_with_entry(), fast=p.macd_fast, slow=p.macd_slow, signal=p.macd_signal)
        if df is None or len(df) < 3:
            return []
        sig = detect_macd_cross(
            df,
            ctx.symbol,
            lookback_candles=ctx.catchup_bars,
            include_current=False,
            swing_lookback_candles=p.swing_lookback_candles,
            rr_ratio=p.risk_reward_ratio,
        )
        return [SignalIntent.from_signal(sig, self)] if sig is not None else []

    def resolve(self, signal: TradeLevels, candles: list, ctx: ResolveContext) -> TradeOutcome | None:
        expiry = self.params.signal_expiry_candles
        return resolve_outcome(
            signal.side, signal.stop_loss, signal.tp1, candles, expiry, ctx.now_ms, ctx.interval_ms
        )

    def candidate_bars(self, klines: pd.DataFrame) -> np.ndarray:
        p = self.params
        df = compute_macd(klines, fast=p.macd_fast, slow=p.macd_slow, signal=p.macd_signal)
        if df is None or len(df) < 3:
            return np.array([], dtype=int)
        # compute_macd drops the leading candles without a MACD value
        return macd_cross_candidates(df) + (len(klines) - len(df))
