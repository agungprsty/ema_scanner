"""Track-record statistics for closed trades measured in R.

Pure functions only: the database layer picks the eligible trades, this module turns them into the
KPIs the API publishes, each with its sample size and, where it makes sense, a 95% interval.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

# Two-sided 95% normal quantile, for the Wilson interval
Z_95 = 1.959963984540054
CONFIDENCE = 0.95
# Below this many closed trades every rate and mean is reported with an "insufficient sample" warning
MIN_SAMPLE = 30
INSUFFICIENT_SAMPLE = "insufficient sample"
# Summed totals (net R) are only displayed from this many closed trades per strategy version, so a
# handful of trades does not anchor anyone; the API still reports them
MIN_SAMPLE_FOR_TOTALS = 100

DEFINITIONS = {
    "win_rate": "Percent of closed trades that hit the target. Closed means target, stop or expired, "
    "so expired trades count against the win rate.",
    "win_rate_ci95": "Wilson score interval for the win rate, in percent.",
    "win_rate_no_target": "For a strategy without a fixed target (no target_rr), a win is a closed trade with a "
    "net R above zero, and there is no single break-even win rate, since wins and losses have no fixed size.",
    "mean_net_r": "Average result per closed trade in R after fees and modelled slippage (cost model).",
    "mean_net_r_ci95": "Student t interval for the mean net R; null with fewer than 2 trades.",
    "profit_factor": "Sum of winning net R divided by the absolute sum of losing net R; null without a losing trade.",
    "max_drawdown_r": "Largest peak-to-trough fall of cumulative net R, with trades ordered by exit time, then id.",
    "break_even_win_rate": "(1 + mean_cost_r) / (target_rr + 1), in percent: the win rate at which a win of "
    "target_rr R and a loss of 1 R, each paying the average cost, net to zero.",
    "eligible": "Only signals of this strategy version from the scheduled scan, entered at the next candle "
    "open and detected on time, count, and only with the outcome the evaluator booked from candles "
    "(hand-entered trades never count), and only when the signal is recorded in the ledger. Every other "
    "signal is listed under excluded by reason: legacy, late (catchup), manual, other_version, "
    "outside_ledger (no ledger events: stored outside the app), or other.",
    "min_sample_for_totals": "The dashboard shows summed totals such as total_net_r only from this many "
    "closed trades per strategy version, to avoid anchoring on a few trades; the API always reports them.",
}


@dataclass(frozen=True)
class ClosedTrade:
    id: int
    result: str
    net_r: float | None
    gross_r: float | None
    cost_r: float | None
    exit_time: str | None


def parse_utc(value: str | None) -> datetime | None:
    """A stored time as an aware UTC datetime, or None when it is not a timestamp.

    Rows hold both "2026-10-13T00:00:00Z" and the older "2026-10-13 00:00:00" (UTC without a zone),
    which do not sort correctly as text.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def chronological(trades: Iterable[ClosedTrade]) -> list[ClosedTrade]:
    """Trades in exit order: by the real exit timestamp, then id. Unreadable exit times go last."""
    far_future = datetime.max.replace(tzinfo=UTC)
    return sorted(trades, key=lambda t: (parse_utc(t.exit_time) or far_future, t.id))


def max_drawdown(values: Iterable[float]) -> float:
    """Largest peak-to-trough fall of the running total, as a non-negative number.

    The running total starts at zero, so losses before the first gain count as drawdown.
    """
    peak = total = worst = 0.0
    for v in values:
        total += v
        peak = max(peak, total)
        worst = max(worst, peak - total)
    return worst


def profit_factor(values: Iterable[float]) -> float | None:
    """Gross gains over gross losses; None when nothing was lost (the ratio is unbounded)."""
    gains = losses = 0.0
    for v in values:
        if v > 0:
            gains += v
        else:
            losses -= v
    return gains / losses if losses > 0 else None


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson score interval for a proportion, as fractions; None for an empty sample."""
    if n <= 0:
        return None
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)), -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1 + aa * d
            d = 1 / (d if abs(d) > tiny else tiny)
            c = 1 + aa / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1) < 1e-15:
            break
    return h


def _regularized_beta(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    log_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(log_front) * _betacf(a, b, x) / a
    return 1 - math.exp(log_front) * _betacf(b, a, 1 - x) / b


def student_t_cdf(t: float, df: float) -> float:
    tail = 0.5 * _regularized_beta(df / 2, 0.5, df / (df + t * t))
    return 1 - tail if t >= 0 else tail


def student_t_quantile(p: float, df: float) -> float:
    """Inverse Student t CDF, by bisection; accurate to well below the 3 decimals published."""
    if not 0 < p < 1:
        raise ValueError("p must be between 0 and 1")
    if p < 0.5:
        return -student_t_quantile(1 - p, df)
    lo, hi = 0.0, 1.0
    while student_t_cdf(hi, df) < p:
        hi *= 2
    for _ in range(200):
        mid = (lo + hi) / 2
        if student_t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def t_interval(values: Sequence[float], confidence: float = CONFIDENCE) -> tuple[float, float] | None:
    """Student t confidence interval for the mean; None with fewer than two values."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    half = student_t_quantile(0.5 + confidence / 2, n - 1) * sd / math.sqrt(n)
    return mean - half, mean + half


def break_even_win_rate(target_rr: float, mean_cost_r: float | None) -> float | None:
    """Win rate (fraction) at which wins of target_rr R and losses of 1 R, each paying the average cost,
    break even: p * (target_rr - c) - (1 - p) * (1 + c) = 0, so p = (1 + c) / (target_rr + 1)."""
    if mean_cost_r is None or target_rr <= -1:
        return None
    return (1 + mean_cost_r) / (target_rr + 1)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _pct(value: float | None) -> float | None:
    return round(value * 100, 1) if value is not None else None


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def is_win(trade: ClosedTrade, target_rr: float | None) -> bool:
    """A strategy with a fixed target wins when the target is hit; one without wins on a net R above zero."""
    if target_rr is None:
        return trade.net_r is not None and trade.net_r > 0
    return trade.result == "PROFIT"


def summarize_trades(trades: Iterable[ClosedTrade], target_rr: float | None) -> dict:
    """KPIs of closed trades in R. Rates are percents, R values are per trade unless named total.

    `target_rr` is the planned reward-to-risk of a strategy with a fixed target; None for a strategy
    without one, whose win rate then counts trades closed with a positive net R and which has no
    break-even win rate."""
    ordered = chronological(trades)
    n = len(ordered)
    wins = sum(1 for t in ordered if is_win(t, target_rr))
    if target_rr is None:
        losses = n - wins
        expired = 0
    else:
        losses = sum(1 for t in ordered if t.result == "LOSS")
        expired = sum(1 for t in ordered if t.result == "EXPIRED")

    net = [t.net_r for t in ordered if t.net_r is not None]
    gross = [t.gross_r for t in ordered if t.gross_r is not None]
    cost = [t.cost_r for t in ordered if t.cost_r is not None]
    mean_cost_r = _mean(cost)

    win_ci = wilson_interval(wins, n)
    mean_ci = t_interval(net)
    profit = profit_factor(net)
    break_even = break_even_win_rate(target_rr, mean_cost_r) if target_rr is not None else None
    exits = [dt for dt in (parse_utc(t.exit_time) for t in ordered) if dt is not None]

    return {
        "n_closed": n,
        "wins": wins,
        "losses": losses,
        "expired": expired,
        "win_rate": _pct(wins / n) if n else None,
        "win_rate_ci95": [_pct(win_ci[0]), _pct(win_ci[1])] if win_ci else None,
        "mean_net_r": _round(_mean(net), 3),
        "mean_net_r_ci95": [round(mean_ci[0], 3), round(mean_ci[1], 3)] if mean_ci else None,
        "total_net_r": round(sum(net), 3),
        "mean_gross_r": _round(_mean(gross), 3),
        "mean_cost_r": _round(mean_cost_r, 3),
        "profit_factor": _round(profit, 2),
        "max_drawdown_r": round(max_drawdown(net), 3),
        "break_even_win_rate": _pct(break_even),
        "target_rr": target_rr,
        "win_rule": "target_hit" if target_rr is not None else "net_r_positive",
        "sample_size_warning": INSUFFICIENT_SAMPLE if n < MIN_SAMPLE else None,
        "min_sample": MIN_SAMPLE,
        "min_sample_for_totals": MIN_SAMPLE_FOR_TOTALS,
        "first_exit_time": _iso(min(exits)) if exits else None,
        "last_exit_time": _iso(max(exits)) if exits else None,
    }


# Rows of a breakdown table with fewer closed trades than this are shown greyed as "not meaningful"
MIN_BREAKDOWN_SAMPLE = 20
# Width of a bin of the net R histogram
HISTOGRAM_BIN_R = 0.25


def group_summary(label: str, trades: Sequence[ClosedTrade], target_rr: float | None) -> dict:
    """One row of a breakdown table: n, wins, mean net R with its 95% interval, win rate and total."""
    net = [t.net_r for t in trades if t.net_r is not None]
    n = len(trades)
    wins = sum(1 for t in trades if is_win(t, target_rr))
    ci = t_interval(net)
    return {
        "key": label,
        "n": n,
        "wins": wins,
        "win_rate": _pct(wins / n) if n else None,
        "mean_net_r": _round(_mean(net), 3),
        "mean_net_r_ci95": [round(ci[0], 3), round(ci[1], 3)] if ci else None,
        "total_net_r": round(sum(net), 3),
        "meaningful": n >= MIN_BREAKDOWN_SAMPLE,
    }


def breakdown(records: Iterable[tuple[str, ClosedTrade]], target_rr: float | None) -> list[dict]:
    """Breakdown rows from (group label, trade) pairs, largest group first, then by label."""
    groups: dict[str, list[ClosedTrade]] = {}
    for label, trade in records:
        groups.setdefault(label, []).append(trade)
    rows = [group_summary(label, trades, target_rr) for label, trades in groups.items()]
    return sorted(rows, key=lambda r: (-r["n"], r["key"]))


def equity_curve(trades: Iterable[ClosedTrade]) -> list[dict]:
    """Cumulative net R and the drawdown below its running peak, one point per exit instant.

    Trades closing at the same instant (several symbols on one candle close) are one point, so the
    times are strictly increasing; `n` is how many trades the point adds. Trades are walked in the order
    max_drawdown uses (exit time, then id) and a point's drawdown is the deepest reached within its
    instant, so the curve's lowest point equals max_drawdown_r. Unreadable exit times are left out of
    the curve (never out of the KPIs)."""
    points: list[dict] = []
    total = peak = 0.0
    for t in chronological(trades):
        dt = parse_utc(t.exit_time)
        if dt is None or t.net_r is None:
            continue
        ts = int(dt.timestamp())
        total += t.net_r
        peak = max(peak, total)
        drawdown = round(total - peak, 4)
        if points and points[-1]["time"] == ts:
            last = points[-1]
            last.update(equity_r=round(total, 4), drawdown_r=min(last["drawdown_r"], drawdown), n=last["n"] + 1)
        else:
            points.append({"time": ts, "equity_r": round(total, 4), "drawdown_r": drawdown, "n": 1})
    return points


def histogram(values: Iterable[float], bin_width: float = HISTOGRAM_BIN_R) -> list[dict]:
    """Counts of net R per bin [lo, hi), bins aligned on multiples of `bin_width`, empty bins kept."""
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if not vals:
        return []
    first = math.floor(min(vals) / bin_width)
    last = math.floor(max(vals) / bin_width)
    counts = [0] * (last - first + 1)
    for v in vals:
        counts[math.floor(v / bin_width) - first] += 1
    return [
        {"lo": round((first + i) * bin_width, 4), "hi": round((first + i + 1) * bin_width, 4), "count": c}
        for i, c in enumerate(counts)
    ]
