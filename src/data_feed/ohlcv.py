import logging

import pandas as pd
from binance.um_futures import UMFutures

from src.strategy.macd import compute_macd

logger = logging.getLogger(__name__)


def fetch_klines(
    client: UMFutures,
    symbol: str,
    timeframe: str = "2h",
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


def fetch_candles_payload(
    client: UMFutures,
    symbol: str,
    timeframe: str = "2h",
    limit: int = 100,
) -> dict | None:
    safe_limit = max(50, limit)
    df = fetch_klines(client, symbol=symbol, timeframe=timeframe, limit=safe_limit)
    if df is None:
        return None

    df_macd = compute_macd(df)
    if df_macd is None:
        return None

    candles = []
    macd_data = []

    for _, row in df_macd.iterrows():
        ts_sec = int(row["timestamp"].timestamp())
        candles.append({
            "time": ts_sec,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        })

        m = float(row["MACD"]) if not pd.isna(row.get("MACD")) else 0.0
        s = float(row["MACDs"]) if not pd.isna(row.get("MACDs")) else 0.0
        h = float(row["MACDh"]) if not pd.isna(row.get("MACDh")) else 0.0

        macd_data.append({
            "time": ts_sec,
            "macd": round(m, 6),
            "signal": round(s, 6),
            "histogram": round(h, 6),
        })

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": candles,
        "macd": macd_data,
    }
