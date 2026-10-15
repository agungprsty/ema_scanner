import hashlib
from pathlib import Path

import pandas as pd

from src.strategy.context import DAY_MS, normalize_klines, timeframe_ms

KLINE_COLUMNS = ["open_time_ms", "open", "high", "low", "close", "volume", "quote_volume"]


def kline_csv_path(data_dir: str | Path, symbol: str, timeframe: str = "2h") -> Path:
    return Path(data_dir) / f"{symbol}_{timeframe}.csv"


def load_klines_csv(path: str | Path) -> pd.DataFrame:
    """Klines from a `{SYMBOL}_2h.csv` file, shaped like `fetch_klines` returns them.

    Prices are parsed with Python's own float conversion (round_trip), the same as
    `pd.to_numeric` on the strings the Binance API sends, so levels match production to the bit.
    """
    df = pd.read_csv(path, float_precision="round_trip")
    missing = [c for c in KLINE_COLUMNS[:5] if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing columns {missing}")
    df = df.drop_duplicates(subset="open_time_ms").sort_values("open_time_ms").reset_index(drop=True)
    df["open_time_ms"] = df["open_time_ms"].astype("int64")
    df["timestamp"] = pd.to_datetime(df["open_time_ms"], unit="ms")
    return df


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resample_klines(klines: pd.DataFrame, base_timeframe: str, timeframe: str) -> pd.DataFrame:
    """Higher-timeframe candles built from `klines`, aligned to UTC epoch boundaries (so a 1d candle
    is a UTC day, 00:00 to 24:00, like Binance's), with `bars` counting the base candles in each.

    open is the first base open, high the max, low the min, close the last close and volume (and
    quote_volume) the sum: exactly a Binance 1d kline when the day has all 12 2h candles. A day
    with a gap in the 2h data is kept but differs from the exchange's candle, so `bars` tells it
    apart. The last candle may still be forming at the end of the data; MarketContext only ever
    returns candles whose close time (open + interval) has passed the decision, so it is never
    read before it is complete.
    """
    base_ms, target_ms = timeframe_ms(base_timeframe), timeframe_ms(timeframe)
    if target_ms % base_ms or DAY_MS % target_ms:
        raise ValueError(f"cannot resample {base_timeframe} into {timeframe}: it must divide one day evenly")
    df = normalize_klines(klines)
    bucket = df["open_time_ms"] - df["open_time_ms"] % target_ms
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    for col in ("volume", "quote_volume"):
        if col in df.columns:
            agg[col] = "sum"
    out = df.groupby(bucket, sort=True).agg(agg)
    out["bars"] = df.groupby(bucket, sort=True).size()
    out.index.name = "open_time_ms"
    out = out.reset_index()
    return normalize_klines(out)


def load_context_klines(
    data_dir: str | Path, symbol: str, base: pd.DataFrame, base_timeframe: str, timeframes, source: str
) -> tuple[dict[str, pd.DataFrame], dict]:
    """Klines per context timeframe, resampled from the base series (`source="resample"`) or read
    from `{SYMBOL}_{tf}.csv` next to it (`source="csv"`). Returns (frames, description)."""
    frames, described = {}, {}
    for tf in timeframes:
        if source == "csv":
            path = kline_csv_path(data_dir, symbol, tf)
            frames[tf] = normalize_klines(load_klines_csv(path))
            described[tf] = {"source": "csv", "file": path.name, "sha256": file_sha256(path)}
        elif source == "resample":
            frames[tf] = resample_klines(base, base_timeframe, tf)
            described[tf] = {"source": f"resampled from {base_timeframe}"}
        else:
            raise ValueError(f"unknown context source {source!r}")
    return frames, described
