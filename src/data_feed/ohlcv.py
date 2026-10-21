import logging

import pandas as pd
from binance.um_futures import UMFutures

from src.config.settings import TIMEFRAME

logger = logging.getLogger(__name__)


def fetch_klines(
    client: UMFutures,
    symbol: str,
    timeframe: str = TIMEFRAME,
    limit: int = 100,
) -> pd.DataFrame | None:
    try:
        resp = client.klines(symbol=symbol, interval=timeframe, limit=limit)
        if isinstance(resp, dict) and "data" in resp:
            resp = resp["data"]

        if not resp or len(resp) < 35:
            logger.warning(
                "fetch_klines %s %s: insufficient candles (len=%s)",
                symbol,
                timeframe,
                len(resp) if resp else 0,
            )
            return None

        df = pd.DataFrame(
            resp,
            columns=[
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_av",
                "trades",
                "tb_base_av",
                "tb_quote_av",
                "ignore",
            ],
        )

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except Exception as e:
        logger.exception("fetch_klines %s %s: %s", symbol, timeframe, e)
        return None


def fetch_candles_range(
    client: UMFutures,
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
) -> list[dict] | None:
    """Candles whose open time falls between start_ms and end_ms, oldest first, for charting."""
    resp = client.klines(symbol=symbol, interval=timeframe, startTime=start_ms, endTime=end_ms, limit=1500)
    if isinstance(resp, dict) and "data" in resp:
        resp = resp["data"]
    if not resp:
        return None
    return [
        {
            "time": int(k[0]) // 1000,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }
        for k in resp
    ]
