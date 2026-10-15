import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from src.backtest.replay import BacktestTrade
from src.config.settings import (
    COST_MODEL_VERSION,
    ROUND_TRIP_FEE_PERCENT,
    STOP_SLIPPAGE_MULTIPLIER,
    slippage_bps_for,
)
from src.strategy.outcome import cost_adjusted_return

DAY_MS = 24 * 60 * 60 * 1000
WEEK_MS = 7 * DAY_MS
# The Unix epoch fell on a Thursday; shifting by three days makes weeks start on Monday 00:00 UTC
WEEK_OFFSET_MS = 3 * DAY_MS
BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 20261015


@dataclass(frozen=True)
class CostScenario:
    name: str
    fee_percent: float
    slippage_bps: Callable[[str], float]
    stop_slippage_multiplier: float

    def describe(self, symbols: Iterable[str]) -> dict:
        return {
            "fee_percent": self.fee_percent,
            "slippage_bps": {s: self.slippage_bps(s) for s in sorted(symbols)},
            "stop_slippage_multiplier": self.stop_slippage_multiplier,
        }


SCENARIOS = {
    # Exactly what evaluate_pending_signals books: fee plus modelled slippage (cost model cm1)
    "prod": CostScenario("prod", ROUND_TRIP_FEE_PERCENT, slippage_bps_for, STOP_SLIPPAGE_MULTIPLIER),
    # The production fee alone, comparable with the review's "prod 0.10% RT" row
    "fee_only": CostScenario("fee_only", ROUND_TRIP_FEE_PERCENT, lambda _symbol: 0.0, 0.0),
    "gross": CostScenario("gross", 0.0, lambda _symbol: 0.0, 0.0),
}


@dataclass(frozen=True)
class BookedTrade:
    trade: BacktestTrade
    gross_r: float
    cost_r: float
    net_r: float


def book_trades(trades: Iterable[BacktestTrade], scenario: CostScenario) -> list[BookedTrade]:
    """Each trade in R under `scenario`, through the same cost function the ledger books with."""
    booked = []
    for t in trades:
        costs = cost_adjusted_return(
            t.side,
            t.entry_price,
            t.stop_loss,
            t.exit_price,
            t.result,
            scenario.fee_percent,
            scenario.slippage_bps(t.symbol),
            scenario.stop_slippage_multiplier,
        )
        # Zero planned risk has no R; the ledger stores those with a null r_multiple too
        if costs.net_r is None:
            continue
        booked.append(BookedTrade(t, costs.gross_r, costs.cost_r, costs.net_r))
    return booked


def half_year(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return f"{dt.year}H{1 if dt.month <= 6 else 2}"


def weekly_block_ci(
    net_r: np.ndarray, cross_ms: np.ndarray, samples: int = BOOTSTRAP_SAMPLES, seed: int = BOOTSTRAP_SEED
) -> list[float] | None:
    """95% CI of the mean R, resampling whole calendar weeks so trades that crowd into the same
    market move are not counted as independent evidence."""
    weeks = (cross_ms + WEEK_OFFSET_MS) // WEEK_MS
    _, block = np.unique(weeks, return_inverse=True)
    n_blocks = int(block.max()) + 1 if len(block) else 0
    if n_blocks < 2:
        return None
    sums = np.bincount(block, weights=net_r, minlength=n_blocks)
    counts = np.bincount(block, minlength=n_blocks)
    picks = np.random.default_rng(seed).integers(0, n_blocks, size=(samples, n_blocks))
    means = sums[picks].sum(axis=1) / counts[picks].sum(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return [float(lo), float(hi)]


def max_drawdown_r(booked: list[BookedTrade]) -> float:
    """Deepest fall of cumulative net R from its running peak, trades ordered by exit time."""
    ordered = sorted(booked, key=lambda b: (b.trade.exit_time_ms, b.trade.cross_candle_ms, b.trade.symbol))
    equity = np.concatenate([[0.0], np.cumsum([b.net_r for b in ordered])])
    return float((np.maximum.accumulate(equity) - equity).max())


def _num(x: float | None) -> float | None:
    return None if x is None or not math.isfinite(x) else float(x)


def summarize(booked: list[BookedTrade]) -> dict:
    n = len(booked)
    results = [b.trade.result for b in booked]
    outcomes = {k: results.count(k) for k in ("PROFIT", "LOSS", "EXPIRED")}
    if n == 0:
        return {"n": 0, "outcomes": outcomes}
    net = np.array([b.net_r for b in booked])
    gross = np.array([b.gross_r for b in booked])
    cost = np.array([b.cost_r for b in booked])
    sd = float(net.std(ddof=1)) if n > 1 else None
    wins, losses = net[net > 0].sum(), -net[net < 0].sum()
    return {
        "n": n,
        "outcomes": outcomes,
        # Win rate is targets hit over closed trades; expiries are reported apart, never as wins
        "win_rate": outcomes["PROFIT"] / n,
        "expired_rate": outcomes["EXPIRED"] / n,
        "mean_gross_r": float(gross.mean()),
        "mean_cost_r": float(cost.mean()),
        "mean_net_r": float(net.mean()),
        "sd_net_r": sd,
        "t_stat": _num(net.mean() / (sd / math.sqrt(n))) if sd else None,
        "ci95_net_r": weekly_block_ci(net, np.array([b.trade.cross_candle_ms for b in booked], dtype=np.int64)),
        "total_net_r": float(net.sum()),
        "profit_factor": _num(wins / losses) if losses > 0 else None,
        "max_drawdown_r": max_drawdown_r(booked),
    }


def _group(booked: list[BookedTrade], key: Callable[[BookedTrade], str]) -> dict:
    groups: dict[str, list[BookedTrade]] = {}
    for b in booked:
        groups.setdefault(key(b), []).append(b)
    return {k: summarize(groups[k]) for k in sorted(groups)}


def scenario_report(booked: list[BookedTrade], oos_start_ms: int | None) -> dict:
    report = summarize(booked)
    report["by_symbol"] = _group(booked, lambda b: b.trade.symbol)
    report["by_side"] = _group(booked, lambda b: b.trade.side)
    report["by_half_year"] = _group(booked, lambda b: half_year(b.trade.cross_candle_ms))
    if oos_start_ms is not None:
        report["in_sample"] = summarize([b for b in booked if b.trade.cross_candle_ms < oos_start_ms])
        report["out_of_sample"] = summarize([b for b in booked if b.trade.cross_candle_ms >= oos_start_ms])
    return report


def cost_model_description(symbols: Iterable[str]) -> dict:
    symbols = list(symbols)
    return {
        "cost_model_version": COST_MODEL_VERSION,
        "scenarios": {name: s.describe(symbols) for name, s in SCENARIOS.items()},
    }
