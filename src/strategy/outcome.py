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


@dataclass(frozen=True)
class TradeCosts:
    gross_r: float | None
    cost_r: float | None
    net_r: float | None
    pnl_percent_gross: float
    pnl_percent_net: float


def cost_adjusted_return(
    side: str,
    entry: float,
    stop_loss: float,
    exit_price: float,
    result: str,
    fee_percent: float,
    slippage_bps: float,
    stop_slippage_multiplier: float,
) -> TradeCosts:
    """Gross and net result of a trade booked at its planned levels, under the cost model.

    The entry fills at market `slippage_bps` worse than planned. A stop (LOSS) fills at market
    `slippage_bps * stop_slippage_multiplier` worse than the stop, an EXPIRED close
    `slippage_bps` worse than the close, and a target (PROFIT) at its limit price. `fee_percent`
    is the round-trip fee as a percent of notional. Percentages are of the planned entry and R is
    measured against the planned risk |entry - stop| / entry, so net_r = gross_r - cost_r exactly.
    """
    if entry <= 0:
        return TradeCosts(None, None, None, 0.0, 0.0)
    direction = 1 if side == "LONG" else -1
    slip = slippage_bps / 10_000
    exit_slip = {"PROFIT": 0.0, "LOSS": slip * stop_slippage_multiplier}.get(result, slip)
    # Adverse fills: a long buys higher and sells lower, a short the other way round
    entry_fill = entry * (1 + direction * slip)
    exit_fill = exit_price * (1 - direction * exit_slip)

    gross_percent = direction * (exit_price - entry) / entry * 100
    net_percent = direction * (exit_fill - entry_fill) / entry * 100 - fee_percent
    risk_percent = abs(entry - stop_loss) / entry * 100
    if risk_percent <= 0:
        return TradeCosts(None, None, None, gross_percent, net_percent)
    gross_r = gross_percent / risk_percent
    cost_r = (gross_percent - net_percent) / risk_percent
    return TradeCosts(gross_r, cost_r, gross_r - cost_r, gross_percent, net_percent)
