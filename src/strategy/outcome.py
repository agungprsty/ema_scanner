from dataclasses import dataclass

CANDLE_INTERVAL_MS = {"2h": 2 * 60 * 60 * 1000}


@dataclass
class TradeOutcome:
    result: str  # PROFIT, LOSS or EXPIRED
    win_status: str  # TP1_HIT, SL_HIT or EXPIRED
    exit_price: float
    exit_time_ms: int


def resolve_outcome(
    side: str,
    stop_loss: float,
    tp1: float,
    candles: list,
    expiry_candles: int,
    now_ms: int,
    interval_ms: int,
) -> TradeOutcome | None:
    """Decide how a signal ended from the klines that follow its cross candle, oldest first.

    A candle that touches both levels counts as a stop, since candle data cannot tell which came
    first. When neither level is hit within `expiry_candles`, the trade closes at the last of
    those candles' close, once that candle has closed.
    """
    for candle in candles[:expiry_candles]:
        open_ms = int(candle[0])
        high = float(candle[2])
        low = float(candle[3])
        if side == "LONG":
            hit_tp = tp1 > 0 and high >= tp1
            hit_sl = stop_loss > 0 and low <= stop_loss
        else:
            hit_tp = tp1 > 0 and low <= tp1
            hit_sl = stop_loss > 0 and high >= stop_loss
        if hit_sl:
            return TradeOutcome("LOSS", "SL_HIT", stop_loss, open_ms)
        if hit_tp:
            return TradeOutcome("PROFIT", "TP1_HIT", tp1, open_ms)

    if expiry_candles > 0 and len(candles) >= expiry_candles:
        last = candles[expiry_candles - 1]
        close_ms = int(last[0]) + interval_ms
        if close_ms <= now_ms:
            return TradeOutcome("EXPIRED", "EXPIRED", float(last[4]), close_ms)
    return None


def trade_return(
    side: str, entry: float, stop_loss: float, exit_price: float, fee_percent: float
) -> tuple[float, float | None]:
    """Return (PnL percent after fees, R multiple) for a trade risked down to `stop_loss`."""
    if entry <= 0:
        return 0.0, None
    direction = 1 if side == "LONG" else -1
    pnl_percent = direction * (exit_price - entry) / entry * 100 - fee_percent
    risk_percent = abs(entry - stop_loss) / entry * 100
    r_multiple = pnl_percent / risk_percent if risk_percent > 0 else None
    return pnl_percent, r_multiple
