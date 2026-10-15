"""Candle fingerprints: what a detection read, and the window a resolution was settled on.

A detection's input_candles_sha256 covers the closed candles of every timeframe in the strategy's
MarketContext (the forming candle is not part of it; its open is the entry price, stored on the
signal). Rows are [open_time_ms, open, high, low, close, volume] as numbers, oldest first, keyed by
timeframe, in the canonical JSON of src.ledger.chain.
"""

from src.ledger.chain import candles_sha256

FRAME_COLUMNS = ("open", "high", "low", "close", "volume")


def frame_rows(df) -> list[list]:
    """[open_time_ms, open, high, low, close, volume] per row of a normalized klines frame."""
    if df is None or len(df) == 0:
        return []
    cols = [c for c in FRAME_COLUMNS if c in df.columns]
    rows = []
    for values in df[["open_time_ms", *cols]].itertuples(index=False, name=None):
        rows.append([int(values[0]), *(float(v) for v in values[1:])])
    return rows


def context_fingerprint(ctx) -> tuple[str, dict]:
    """(sha256, summary) of the closed candles of every timeframe in a MarketContext. The summary
    gives, per timeframe, the candle count and the first and last open time, so anyone holding the
    klines can rebuild exactly what was hashed."""
    rows = {tf: frame_rows(ctx.closed(tf)) for tf in sorted(ctx.frames)}
    summary = {
        tf: {
            "count": len(r),
            "first_open_ms": r[0][0] if r else None,
            "last_open_ms": r[-1][0] if r else None,
        }
        for tf, r in rows.items()
    }
    return candles_sha256(rows), {"decision_ms": int(ctx.decision_ms), "timeframes": summary}


def kline_rows(raw_klines) -> list[list]:
    """Binance kline rows cut to [open_time_ms, open, high, low, close, volume], the prices kept
    exactly as the exchange sent them (text), for persisting the window a trade was settled on."""
    return [[int(k[0]), *list(k[1:6])] for k in raw_klines]


def chart_candles(rows: list[list], interval_ms: int, as_of_ms: int | None = None) -> list[dict]:
    """Persisted kline rows as chart candles ({time (seconds), open, high, low, close, volume}).
    A candle not yet closed at `as_of_ms` (the resolution instant) is flagged partial."""
    out = []
    for row in rows:
        open_ms = int(row[0])
        candle = {
            "time": open_ms // 1000,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]) if len(row) > 5 else 0.0,
        }
        if as_of_ms is not None and open_ms + interval_ms > as_of_ms:
            candle["partial"] = True
        out.append(candle)
    return out
