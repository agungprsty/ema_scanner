from dataclasses import dataclass


@dataclass
class Trade:
    status: str  # PENDING, OPEN, TP_HIT, SL_HIT, EXPIRED or NOT_FILLED
    fill_price: float | None = None
    fill_ms: int | None = None
    exit_price: float | None = None
    closed_ms: int | None = None


def resolve(
    side: str,
    entry: float,
    tp: float,
    sl: float,
    candles: list,
    fill_window: int,
    max_hold: int,
    interval_ms: int,
    now_ms: int,
) -> Trade:
    """Replay a limit-entry trade from the klines that follow its signal candle, oldest first.

    Only candles closed by `now_ms` count, so the same klines always give the same Trade. The
    limit fills on the first of `fill_window` candles that touches `entry`, at the open when
    price gapped through it. From the fill candle on, for up to `max_hold` candles: an open
    already past the stop or target exits there; a candle touching both counts as the stop,
    since candle data cannot tell which came first. On the fill candle the target only counts
    when the open was already at or past the entry, otherwise it may have been hit before the
    fill.
    """
    long = side == "LONG"
    closed = [c for c in candles if int(c[0]) + interval_ms <= now_ms]

    for f, candle in enumerate(closed[:fill_window]):
        open_ = float(candle[1])
        if (float(candle[3]) <= entry) if long else (float(candle[2]) >= entry):
            fill = min(open_, entry) if long else max(open_, entry)
            fill_ms = int(candle[0])
            break
    else:
        if len(closed) >= fill_window:
            return Trade("NOT_FILLED", closed_ms=int(closed[fill_window - 1][0]) + interval_ms)
        return Trade("PENDING")

    window = closed[f : f + max_hold]
    for j, candle in enumerate(window):
        open_ms = int(candle[0])
        open_, high, low = float(candle[1]), float(candle[2]), float(candle[3])
        tp_ok = j > 0 or (open_ <= entry if long else open_ >= entry)
        hit_sl = low <= sl if long else high >= sl
        hit_tp = tp_ok and (high >= tp if long else low <= tp)
        past_tp = tp_ok and (open_ >= tp if long else open_ <= tp)
        # An open past the target exits there even when the candle later drops through the stop
        if hit_sl and not past_tp:
            return Trade("SL_HIT", fill, fill_ms, min(open_, sl) if long else max(open_, sl), open_ms)
        if hit_tp:
            return Trade("TP_HIT", fill, fill_ms, max(open_, tp) if long else min(open_, tp), open_ms)

    if len(window) >= max_hold:
        last = window[-1]
        return Trade("EXPIRED", fill, fill_ms, float(last[4]), int(last[0]) + interval_ms)
    return Trade("OPEN", fill, fill_ms)


def trade_return(
    side: str, fill: float, exit_price: float, risk_percent: float, fee_percent: float
) -> tuple[float, float | None]:
    """Return (PnL percent after fees, R multiple) against the planned `risk_percent`.

    R is scaled by the risk planned at the signal, not by the distance from the fill to the
    stop, so a fill that gapped close to the stop does not blow R up.
    """
    if fill <= 0:
        return 0.0, None
    direction = 1 if side == "LONG" else -1
    pnl_percent = direction * (exit_price - fill) / fill * 100 - fee_percent
    r_multiple = pnl_percent / risk_percent if risk_percent > 0 else None
    return pnl_percent, r_multiple
