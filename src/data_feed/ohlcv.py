import logging

import pandas as pd
from binance.um_futures import UMFutures

logger = logging.getLogger(__name__)


def fetch_klines(
    client: UMFutures,
    symbol: str,
    timeframe: str = "4h",
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
        logger.error("fetch_klines %s %s: %s", symbol, timeframe, e, exc_info=True)
        return None
