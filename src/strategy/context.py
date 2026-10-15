"""What a strategy may see when it decides: closed candles only, per timeframe, as of one instant.

Every comparison is done on integer epoch milliseconds (`open_time_ms`), never on datetime values.
pandas 3 parses `pd.to_datetime(ms, unit="ms")` to datetime64[ms] where pandas 2 gave [ns], so any
code that turns a timestamp column into integers and assumes nanoseconds is off by 10^6 and can
silently let future candles through. `normalize_klines` derives milliseconds through
`.dt.as_unit("ms")`, which is right for either resolution.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS
# Candle length per timeframe this code understands
TIMEFRAME_MS = {
    "1h": HOUR_MS,
    "2h": 2 * HOUR_MS,
    "4h": 4 * HOUR_MS,
    "12h": 12 * HOUR_MS,
    "1d": DAY_MS,
}


def timeframe_ms(timeframe: str) -> int:
    try:
        return TIMEFRAME_MS[timeframe]
    except KeyError:
        raise ValueError(f"unsupported timeframe {timeframe!r}") from None


def normalize_klines(df: pd.DataFrame) -> pd.DataFrame:
    """Klines sorted by open time, one row per candle, with an int64 `open_time_ms` column and a
    `timestamp` column (open time). Either column may be the source: frames from `fetch_klines`
    only carry `timestamp`, CSV frames carry both."""
    out = df.copy()
    if "open_time_ms" in out.columns:
        out["open_time_ms"] = out["open_time_ms"].astype("int64")
    elif "timestamp" in out.columns:
        ts = pd.to_datetime(out["timestamp"])
        if ts.dt.tz is not None:
            ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
        out["open_time_ms"] = ts.dt.as_unit("ms").astype("int64")
    else:
        raise ValueError("klines need an open_time_ms or a timestamp column")
    if "timestamp" not in out.columns:
        out["timestamp"] = pd.to_datetime(out["open_time_ms"], unit="ms")
    out = out.drop_duplicates(subset="open_time_ms").sort_values("open_time_ms").reset_index(drop=True)
    return out


@dataclass(frozen=True)
class MarketContext:
    """Closed candles of one symbol per timeframe, as of `decision_ms`.

    `decision_ms` is the instant the strategy decides: the close of the newest closed candle of the
    base timeframe. A candle is closed once `open_time_ms + interval <= decision_ms`; nothing later
    is ever returned. `next_open` is the open of the base candle that starts at `decision_ms`, the
    first price anyone can trade once the decision is made (None when it is not known yet).
    `catchup_bars` is how many of the newest closed base candles detection re-checks: 1 in a replay,
    which decides after every close, and CRON_LOOKBACK_CANDLES live, to catch crosses missed while
    the app was down.
    """

    symbol: str
    decision_ms: int
    base_timeframe: str
    frames: Mapping[str, pd.DataFrame] = field(repr=False)
    next_open: float | None = None
    catchup_bars: int = 1

    def __post_init__(self):
        if self.base_timeframe not in self.frames:
            raise ValueError(f"no {self.base_timeframe} klines in the context")
        for tf, df in self.frames.items():
            timeframe_ms(tf)
            if "open_time_ms" not in df.columns or df["open_time_ms"].dtype != np.int64:
                raise ValueError(f"{tf} klines are not normalized; pass them through normalize_klines")

    @classmethod
    def from_klines(
        cls,
        symbol: str,
        now_ms: int,
        base_timeframe: str,
        frames: Mapping[str, pd.DataFrame],
        catchup_bars: int = 1,
    ) -> "MarketContext":
        """Context for a live decision at `now_ms` from freshly fetched klines, whose last row is
        usually the forming candle. The decision instant is the latest base-timeframe boundary at or
        before `now_ms`, and the forming base candle contributes its open only."""
        interval = timeframe_ms(base_timeframe)
        decision_ms = int(now_ms) - int(now_ms) % interval
        normalized = {tf: normalize_klines(df) for tf, df in frames.items()}
        base = normalized[base_timeframe]
        forming = base[base["open_time_ms"] == decision_ms]
        next_open = float(forming["open"].iloc[0]) if len(forming) else None
        return cls(symbol, decision_ms, base_timeframe, normalized, next_open, catchup_bars)

    def interval_ms(self, timeframe: str | None = None) -> int:
        return timeframe_ms(timeframe or self.base_timeframe)

    def _closed_count(self, timeframe: str, at_ms: int | None) -> int:
        at = self.decision_ms if at_ms is None else min(int(at_ms), self.decision_ms)
        open_ms = self.frames[timeframe]["open_time_ms"].to_numpy()
        # Candles closed by `at`: open_time_ms + interval <= at, i.e. open_time_ms <= at - interval
        return int(np.searchsorted(open_ms, at - timeframe_ms(timeframe), side="right"))

    def closed(self, timeframe: str | None = None, at_ms: int | None = None) -> pd.DataFrame:
        """Candles of `timeframe` closed by `at_ms` (default and upper bound: the decision instant),
        oldest first."""
        tf = timeframe or self.base_timeframe
        if tf not in self.frames:
            raise KeyError(f"no {tf} klines in the context")
        return self.frames[tf].iloc[: self._closed_count(tf, at_ms)]

    def asof(self, timeframe: str | None = None, at_ms: int | None = None) -> pd.Series | None:
        """The newest candle of `timeframe` closed by `at_ms` (capped at the decision instant), or
        None when none has closed yet. Its close time is never later than the decision."""
        tf = timeframe or self.base_timeframe
        n = self._closed_count(tf, at_ms)
        return self.frames[tf].iloc[n - 1] if n else None

    def bars_with_entry(self) -> pd.DataFrame:
        """Closed base candles plus the candle opening at the decision instant, shaped like a live
        kline frame whose last row is still forming. Only that row's open is real: its high, low and
        close are set to the open and its volume to 0, so nothing the market has not printed yet can
        leak in. Without a known `next_open` the closed candles are returned alone."""
        closed = self.closed()
        if self.next_open is None:
            return closed
        entry = {col: [np.nan] for col in closed.columns}
        for col in ("open", "high", "low", "close"):
            entry[col] = [float(self.next_open)]
        entry["open_time_ms"] = [self.decision_ms]
        if "volume" in closed.columns:
            entry["volume"] = [0.0]
        row = pd.DataFrame(entry)
        row["open_time_ms"] = row["open_time_ms"].astype("int64")
        row["timestamp"] = pd.to_datetime(row["open_time_ms"], unit="ms").astype(closed["timestamp"].dtype)
        return pd.concat([closed, row[closed.columns]], ignore_index=True)
