"""The pre-registered holdout test of macd_cross_ema50d_oppexit@1.0.0, as fixed in
docs/research/preregistration-macd_cross_ema50d_oppexit-1.0.0.md before any holdout run.

python -m src.backtest.prereg --data <sealed holdout dir> --out holdout-report.json

The dataset, the primary metric (mean net R per trade under cost model cm1) and the four pass criteria
are constants here; nothing on the command line can change them, except --start/--end for a sanity
run on other data, which is then labelled as not the pre-registered test and never logged as one.
"""

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

from src.backtest.metrics import BookedTrade, CostScenario, book_trades, weekly_block_ci
from src.backtest.replay import BacktestTrade
from src.backtest.run import run_backtest
from src.backtest.trials import TRIAL_PURPOSES, append_trial, trial_entry
from src.config.settings import (
    CM1_FEE_PERCENT,
    CM1_SLIPPAGE_BPS_DEFAULT,
    CM1_SLIPPAGE_BPS_MAP,
    CM1_STOP_SLIPPAGE_MULTIPLIER,
)
from src.strategy import registry

STRATEGY_REF = "macd_cross_ema50d_oppexit@1.0.0"
WATCHLIST = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "NEARUSDT", "ZECUSDT")
CONTROL = ("BNBUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT")
SYMBOLS = WATCHLIST + CONTROL
# Cross candles opening in [HOLDOUT_START, HOLDOUT_END), i.e. 2020-01-01 to 2024-09-30 inclusive, UTC
HOLDOUT_START = "2020-01-01"
HOLDOUT_END = "2024-10-01"

MIN_T_STAT = 2.0
IMPACT_ROUND_TRIP_PERCENT = 0.10
TOP_TRIM_FRACTION = 0.01
MIN_POSITIVE_SYMBOLS = 7


def _cm1_slippage(symbol: str) -> float:
    return CM1_SLIPPAGE_BPS_MAP.get(symbol.upper(), CM1_SLIPPAGE_BPS_DEFAULT)


# cm1 by its constants, whatever the environment overrides: the pre-registration names these numbers
CM1 = CostScenario("cm1", CM1_FEE_PERCENT, _cm1_slippage, CM1_STOP_SLIPPAGE_MULTIPLIER)
CM1_IMPACT = CostScenario(
    "cm1_impact10", CM1_FEE_PERCENT + IMPACT_ROUND_TRIP_PERCENT, _cm1_slippage, CM1_STOP_SLIPPAGE_MULTIPLIER
)


def mean_stats(booked: list[BookedTrade]) -> dict:
    """n, mean net R, its t-stat (sample sd, ddof 1), the weekly-block bootstrap 95% CI, and the mean
    gross R for reference."""
    n = len(booked)
    if n == 0:
        return {"n": 0, "mean_net_r": None, "t_stat": None, "ci95_net_r": None, "mean_gross_r": None}
    net = np.array([b.net_r for b in booked])
    sd = float(net.std(ddof=1)) if n > 1 else 0.0
    t = float(net.mean() / (sd / math.sqrt(n))) if sd > 0 else None
    cross_ms = np.array([b.trade.cross_candle_ms for b in booked], dtype=np.int64)
    return {
        "n": n,
        "mean_net_r": float(net.mean()),
        "t_stat": t,
        "ci95_net_r": weekly_block_ci(net, cross_ms),
        "mean_gross_r": float(np.mean([b.gross_r for b in booked])),
    }


def trim_top(booked: list[BookedTrade], fraction: float = TOP_TRIM_FRACTION) -> list[BookedTrade]:
    """`booked` without its ceil(fraction * n) trades of highest net R."""
    k = math.ceil(fraction * len(booked))
    if k == 0:
        return list(booked)
    order = sorted(range(len(booked)), key=lambda i: booked[i].net_r, reverse=True)
    dropped = set(order[:k])
    return [b for i, b in enumerate(booked) if i not in dropped]


def min_positive_symbols(basket_size: int) -> int:
    """7 of the 12 symbols; a 6-symbol secondary basket is held to the same share, 4 of 6."""
    return math.ceil(MIN_POSITIVE_SYMBOLS * basket_size / len(SYMBOLS))


def _positive(stats: dict) -> bool:
    return stats["mean_net_r"] is not None and stats["mean_net_r"] > 0


def basket_report(trades: list[BacktestTrade], symbols: tuple[str, ...]) -> dict:
    """Every pre-registered figure for the trades of `symbols`, with each criterion's pass flag."""
    trades = [t for t in trades if t.symbol in symbols]
    primary = book_trades(trades, CM1)
    stats = mean_stats(primary)
    impact = mean_stats(book_trades(trades, CM1_IMPACT))
    trimmed = trim_top(primary)
    trimmed_stats = {**mean_stats(trimmed), "dropped": len(primary) - len(trimmed)}
    per_symbol = {s: mean_stats([b for b in primary if b.trade.symbol == s]) for s in symbols}
    positive_symbols = sorted(s for s, st in per_symbol.items() if _positive(st))
    ci = stats["ci95_net_r"]
    criteria = {
        "1_mean_ci_t": _positive(stats)
        and ci is not None
        and ci[0] > 0
        and stats["t_stat"] is not None
        and stats["t_stat"] >= MIN_T_STAT,
        "2_impact_10bps": _positive(impact),
        "3_without_top_1pct": _positive(trimmed_stats),
        "4_positive_symbols": len(positive_symbols) >= min_positive_symbols(len(symbols)),
    }
    results = sorted({t.result for t in trades})
    return {
        "symbols": list(symbols),
        "primary_cm1": stats,
        "impact_10bps": impact,
        "without_top_1pct": trimmed_stats,
        "per_symbol": per_symbol,
        "positive_symbols": positive_symbols,
        "exits": {r: sum(1 for t in trades if t.result == r) for r in results},
        "criteria": criteria,
    }


def evaluate(trades: list[BacktestTrade]) -> dict:
    """The pre-registered verdict: PASS only when all four criteria hold on the 12 symbols. The control
    and watchlist baskets are reported the same way as secondary results and do not change the verdict.
    Criterion 4 needs a positive mean in at least 7 of the 12 symbols (in a 6-symbol basket: 4)."""
    primary = basket_report(trades, SYMBOLS)
    return {
        "strategy": STRATEGY_REF,
        "primary_metric": "mean net R per trade under cost model cm1",
        "thresholds": {
            "min_t_stat": MIN_T_STAT,
            "impact_round_trip_percent": IMPACT_ROUND_TRIP_PERCENT,
            "top_trim_fraction": TOP_TRIM_FRACTION,
            "min_positive_symbols": MIN_POSITIVE_SYMBOLS,
        },
        "verdict": "PASS" if all(primary["criteria"].values()) else "FAIL",
        "all_symbols": primary,
        "secondary": {"control": basket_report(trades, CONTROL), "watchlist": basket_report(trades, WATCHLIST)},
    }


def _fmt(stats: dict) -> str:
    if not stats["n"]:
        return "n 0"
    ci = stats["ci95_net_r"] or [math.nan, math.nan]
    t = stats["t_stat"] if stats["t_stat"] is not None else math.nan
    return f"n {stats['n']:5d}  mean {stats['mean_net_r']:+.4f}R  t {t:+.2f}  CI [{ci[0]:+.4f}, {ci[1]:+.4f}]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.backtest.prereg", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data", required=True, type=Path, help="directory of {SYMBOL}_2h.csv for the 12 symbols")
    parser.add_argument("--start", default=HOLDOUT_START, help="sanity runs only: first cross date (UTC)")
    parser.add_argument("--end", default=HOLDOUT_END, help="sanity runs only: crosses before this date (UTC)")
    parser.add_argument("--purpose", choices=TRIAL_PURPOSES, help="trial purpose (default holdout, else adhoc)")
    parser.add_argument("--trials-log", type=Path, help="multiple-testing log (default research/trials.jsonl)")
    parser.add_argument("--out", type=Path, help="write the evaluation and the harness report here as JSON")
    args = parser.parse_args(argv)

    preregistered = (args.start, args.end) == (HOLDOUT_START, HOLDOUT_END)
    purpose = args.purpose or ("holdout" if preregistered else "adhoc")
    if purpose == "holdout" and not preregistered:
        print("a holdout trial uses the pre-registered window; drop --start/--end", file=sys.stderr)
        return 2
    missing = [s for s in SYMBOLS if not (args.data / f"{s}_2h.csv").is_file()]
    if missing:
        print(f"missing data for {', '.join(missing)} in {args.data}", file=sys.stderr)
        return 2

    strategy = registry.get(STRATEGY_REF)
    lock = registry.load_lock().get(STRATEGY_REF)
    if lock is None or (lock["params_hash"], lock["code_hash"]) != (strategy.params_hash, strategy.code_hash):
        print(f"{STRATEGY_REF} does not match its frozen lock entry; refusing to run", file=sys.stderr)
        return 2

    report, trades = run_backtest(args.data, list(SYMBOLS), args.start, args.end, strategy=strategy)
    result = evaluate(trades)
    print(f"{STRATEGY_REF}  params {strategy.params_hash[:12]}  code {strategy.code_hash[:12]}")
    if not preregistered:
        print(f"SANITY RUN on {args.start}..{args.end}: NOT the pre-registered holdout test")
    print(f"crosses {report['signals']['crosses']}  resolved {report['signals']['resolved']}  "
          f"unresolved {report['signals']['unresolved']}")
    for label, basket in (("12 symbols", result["all_symbols"]), *result["secondary"].items()):
        print(f"[{label}] cm1 {_fmt(basket['primary_cm1'])}")
        print(f"    +10bps {_fmt(basket['impact_10bps'])}")
        print(f"    -top1% {_fmt(basket['without_top_1pct'])}")
        print(f"    positive symbols {len(basket['positive_symbols'])}/{len(basket['symbols'])}  "
              f"criteria {basket['criteria']}")
    print(f"verdict: {result['verdict']}{'' if preregistered else ' (sanity run, not a verdict)'}")
    entry = trial_entry(report, purpose=purpose)
    entry["prereg"] = {"verdict": result["verdict"], "preregistered_window": preregistered}
    print(f"trial logged to {append_trial(entry, args.trials_log)}")
    if args.out:
        args.out.write_text(json.dumps({"evaluation": result, "report": report}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
