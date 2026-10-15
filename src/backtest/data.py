import hashlib
from pathlib import Path

import pandas as pd

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
