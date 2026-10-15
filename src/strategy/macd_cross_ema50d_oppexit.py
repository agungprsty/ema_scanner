"""macd_cross_ema50d_oppexit@1.0.0: the shadow candidate of the institutional review (P1), pre-registered in
docs/research/preregistration-macd_cross_ema50d_oppexit-1.0.0.md. Any change here is a new version.

Entry: the production MACD(12,26,9) cross on a closed 2h candle (compute_macd + detect_macd_cross), entered
at the next 2h open, taken only when the cross agrees with the daily trend. The trend is read on the newest
UTC daily candle closed by the close of the cross candle (never one closing later): LONG needs its close
above the EMA50 of daily closes, SHORT below (equal: no trade). The EMA runs over exactly the last 200
closed daily candles, seeded like compute_macd's EMAs (pandas_ta.ema: the SMA of the first 50 closes, then
alpha = 2/51, adjust=False); with fewer than 200 closed daily candles there is no signal.

Stop: the production swing stop (calculate_trade_levels: beyond the extreme of the cross candle and the 5
before it, 0.2% buffer, 1.5% fallback). No fixed target (tp1 = 0).

Exit, checked candle by candle from the entry candle on: (1) the stop is touched, checked first, booked at
the stop (STOPPED); (2) the candle closes with the opposite MACD cross, booked at the open of the next
candle (OPPOSITE_CROSS); (3) the 96th candle from the entry candle closes, booked at its close
(TIME_EXIT). An opposite cross on the 96th candle exits at the 97th candle's open, the same instant as its
close. The MACD for (2) is computed over the 498 candles before the cross candle, the cross candle and the
closed candles after it, the same window in the live evaluator and in a replay, so both find the same exit
candle.
"""

from dataclasses import replace

import numpy as np
import pandas as pd
import pandas_ta as ta

from src.strategy.base import ResolveContext, SignalIntent, Strategy, StrategyParams, TradeLevels
from src.strategy.context import MarketContext
from src.strategy.exits import OPPOSITE_CROSS, STOPPED, TIME_EXIT
from src.strategy.macd import compute_macd, detect_macd_cross
from src.strategy.macd_cross import macd_cross_candidates
from src.strategy.outcome import TradeOutcome

KLINE_COLUMNS = ["open_time_ms", "open", "high", "low", "close"]


class MacdCrossEma50dOppExitParams(StrategyParams):
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    # Klines a live scan fetches, the forming one included
    macd_candles: int = 500
    swing_lookback_candles: int = 5
    trend_ema_period: int = 50
    # Closed daily candles the EMA runs over; fewer means no signal
    trend_ema_bars: int = 200
    max_hold_candles: int = 96


def daily_ema(closes: pd.Series, period: int) -> pd.Series | None:
    """EMA of `closes` as compute_macd computes its EMAs: pandas_ta.ema, SMA of the first `period` values
    as the seed, then alpha = 2 / (period + 1) with adjust=False. TA-Lib is never used."""
    return ta.ema(closes.astype(float).reset_index(drop=True), length=period, talib=False)


def opposite_cross_flags(macd_df: pd.DataFrame, start: int, side: str) -> np.ndarray:
    """For each candle after position `start` of a compute_macd frame, whether it closes with the MACD
    cross opposite to `side`, by the same comparisons as detect_macd_cross (NaN compares false)."""
    macd = macd_df["MACD"].to_numpy(dtype=float)[start:]
    sig = macd_df["MACDs"].to_numpy(dtype=float)[start:]
    if side == "LONG":
        return (macd[:-1] >= sig[:-1]) & (macd[1:] < sig[1:])
    return (macd[:-1] <= sig[:-1]) & (macd[1:] > sig[1:])


class MacdCrossEma50dOppExit(Strategy):
    key = "macd_cross_ema50d_oppexit"
    version = "1.0.0"
    name = "MACD cross (2h), 1D EMA50 trend, opposite-cross exit"
    description = (
        "MACD(12,26,9) cross on a closed 2h candle in the direction of the last closed daily close versus its "
        "EMA50, entry at the next open, swing stop, no target; exit at the open after the first opposite cross, "
        "at the stop (checked first), or at the close of the 96th candle."
    )
    status = "shadow"
    timeframe = "2h"
    context_timeframes = ("1d",)
    code_modules = (
        "src.strategy.base",
        "src.strategy.context",
        "src.strategy.exits",
        "src.strategy.macd",
        "src.strategy.macd_cross",
        "src.strategy.outcome",
    )
    params_model = MacdCrossEma50dOppExitParams

    @property
    def warmup_bars(self) -> int:
        # As macd_cross: a live scan's newest closed candle has macd_candles - 2 candles before it
        return self.params.macd_candles - 2

    @property
    def history_bars(self) -> int:
        return self.params.macd_candles

    @property
    def context_bars(self) -> int:
        # One spare closed day, so a catch-up cross from before midnight still sees a full window
        return self.params.trend_ema_bars + 1

    @property
    def max_hold_bars(self) -> int:
        # The 96 candles of the hold, plus the next one whose open fills an opposite cross on the last
        return self.params.max_hold_candles + 1

    @property
    def resolve_history_bars(self) -> int:
        return self.warmup_bars

    def daily_trend(self, ctx: MarketContext, at_ms: int) -> dict | None:
        """The daily trend as of `at_ms` (the close of the cross candle): the newest closed daily candle,
        its close, the EMA and the side they allow; None without a full EMA window."""
        p = self.params
        daily = ctx.closed("1d", at_ms=at_ms).tail(p.trend_ema_bars)
        if len(daily) < p.trend_ema_bars:
            return None
        ema = daily_ema(daily["close"], p.trend_ema_period)
        if ema is None or pd.isna(ema.iloc[-1]):
            return None
        close, level = float(daily["close"].iloc[-1]), float(ema.iloc[-1])
        side = "LONG" if close > level else "SHORT" if close < level else None
        return {"side": side, "day_open_ms": int(daily["open_time_ms"].iloc[-1]), "day_close": close, "ema": level}

    def detect(self, ctx: MarketContext) -> list[SignalIntent]:
        p = self.params
        if len(ctx.closed()) < self.warmup_bars + 1:
            return []
        df = compute_macd(ctx.bars_with_entry(), fast=p.macd_fast, slow=p.macd_slow, signal=p.macd_signal)
        if df is None or len(df) < 3:
            return []
        sig = detect_macd_cross(
            df,
            ctx.symbol,
            lookback_candles=ctx.catchup_bars,
            include_current=False,
            swing_lookback_candles=p.swing_lookback_candles,
            rr_ratio=0.0,
        )
        if sig is None:
            return []
        trend = self.daily_trend(ctx, sig.cross_candle_ms + ctx.interval_ms())
        if trend is None or trend["side"] != sig.side:
            return []
        sig = replace(sig, tp1=0.0, risk_reward_ratio=0.0)
        return [
            SignalIntent.from_signal(
                sig, self, trend_day_open_ms=trend["day_open_ms"], trend_day_close=trend["day_close"],
                trend_ema=trend["ema"],
            )
        ]

    def _opposite_crosses(self, history: pd.DataFrame, closed: list, side: str) -> np.ndarray | None:
        """Opposite-cross flags for the closed candles after the cross candle (the last row of `history`)."""
        p = self.params
        after = pd.DataFrame([[int(c[0]), *(float(v) for v in c[1:5])] for c in closed], columns=KLINE_COLUMNS)
        frame = pd.concat([history[KLINE_COLUMNS].astype({"open_time_ms": "int64"}), after], ignore_index=True)
        for col in KLINE_COLUMNS[1:]:
            frame[col] = frame[col].astype(float)
        macd_df = compute_macd(frame, fast=p.macd_fast, slow=p.macd_slow, signal=p.macd_signal)
        if macd_df is None:
            return None
        # compute_macd drops the leading candles without a MACD value
        start = len(history) - 1 - (len(frame) - len(macd_df))
        if start < 0:
            return None
        return opposite_cross_flags(macd_df, start, side)

    def resolve(self, signal: TradeLevels, candles: list, ctx: ResolveContext) -> TradeOutcome | None:
        """`ctx.history` must hold the closed candles up to and including the cross candle; the MACD reads
        the last resolve_history_bars + 1 of them. Without the full window the trade stays open."""
        if ctx.history is None:
            raise ValueError(f"{self.ref} settles trades from MACD history; pass ResolveContext.history")
        need = self.resolve_history_bars + 1
        history = ctx.history.closed().tail(need)
        if len(history) < need or int(history["open_time_ms"].iloc[-1]) != int(signal.cross_candle_ms):
            return None

        side, stop = signal.side, float(signal.stop_loss)
        rows = candles[: self.max_hold_bars]
        closed = [c for c in rows if int(c[0]) + ctx.interval_ms <= ctx.now_ms]
        opposite = self._opposite_crosses(history, closed, side) if closed else np.array([], dtype=bool)
        if opposite is None:
            return None

        hold = self.params.max_hold_candles
        for j, candle in enumerate(rows[:hold]):
            open_ms, high, low = int(candle[0]), float(candle[2]), float(candle[3])
            # A touch counts as soon as it prints, even on the candle still forming
            if (side == "LONG" and low <= stop) or (side == "SHORT" and high >= stop):
                return TradeOutcome(STOPPED, STOPPED, stop, open_ms)
            if j >= len(closed):
                return None
            if opposite[j]:
                if j + 1 >= len(rows):
                    return None
                nxt = rows[j + 1]
                return TradeOutcome(OPPOSITE_CROSS, OPPOSITE_CROSS, float(nxt[1]), int(nxt[0]))
            if j == hold - 1:
                return TradeOutcome(TIME_EXIT, TIME_EXIT, float(candle[4]), open_ms + ctx.interval_ms)
        return None

    def candidate_bars(self, klines: pd.DataFrame) -> np.ndarray:
        p = self.params
        df = compute_macd(klines, fast=p.macd_fast, slow=p.macd_slow, signal=p.macd_signal)
        if df is None or len(df) < 3:
            return np.array([], dtype=int)
        return macd_cross_candidates(df) + (len(klines) - len(df))
