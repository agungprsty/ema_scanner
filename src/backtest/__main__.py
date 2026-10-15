"""python -m src.backtest --data data/klines --symbols BTCUSDT ETHUSDT --start 2025-01-01 \\
    --end 2026-01-01 --oos-start 2025-07-01 --out report.json [--baseline backtest/baseline.json]
    [--strategy macd_cross@2.2.0] [--purpose research] [--record-trial]

Every run appends one line to research/trials.jsonl (the multiple-testing log)."""

import argparse
import asyncio
import csv
import json
import os
import sys
from pathlib import Path

from src.backtest.gate import baseline_from_report, compare_to_baseline
from src.backtest.run import DEFAULT_STRATEGY, run_backtest
from src.backtest.trials import TRIAL_PURPOSES, append_trial, research_trial_row, trial_entry
from src.config.settings import normalize_database_url
from src.strategy import registry


def _symbols(values: list[str] | None, data_dir: Path, timeframe: str) -> list[str]:
    if not values:
        suffix = f"_{timeframe}.csv"
        return sorted(p.name[: -len(suffix)] for p in data_dir.glob(f"*{suffix}"))
    return [s.strip().upper() for v in values for s in v.split(",") if s.strip()]


def _fmt(x, spec: str = "+.4f") -> str:
    return "-" if x is None else format(x, spec)


def _print_summary(report: dict) -> None:
    sig = report["signals"]
    print(f"crosses {sig['crosses']}  resolved {sig['resolved']}  unresolved {sig['unresolved']}")
    for name, s in report["scenarios"].items():
        if not s["n"]:
            print(f"{name:9s} n 0")
            continue
        ci = s["ci95_net_r"] or [None, None]
        print(
            f"{name:9s} n {s['n']:5d}  win {s['win_rate']:.1%}  mean {_fmt(s['mean_net_r'])}R"
            f"  t {_fmt(s['t_stat'], '+.2f')}  CI [{_fmt(ci[0])}, {_fmt(ci[1])}]"
            f"  total {_fmt(s['total_net_r'], '+.2f')}R  PF {_fmt(s['profit_factor'], '.3f')}"
            f"  maxDD {s['max_drawdown_r']:.1f}R"
        )


def _write_trades(path: Path, trades) -> None:
    fields = list(trades[0].__dataclass_fields__) if trades else ["symbol"]
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(fields)
        for t in trades:
            writer.writerow([getattr(t, f) for f in fields])


async def _record_trial(entry: dict, database_url: str) -> int:
    from src.services.db import insert_research_trial

    return await insert_research_trial(research_trial_row(entry), database_url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.backtest", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data", required=True, type=Path, help="directory of {SYMBOL}_{timeframe}.csv files")
    parser.add_argument(
        "--strategy", default=DEFAULT_STRATEGY, help=f"registered strategy as key@version (default {DEFAULT_STRATEGY})"
    )
    parser.add_argument(
        "--context-source",
        choices=("resample", "csv"),
        default="resample",
        help="higher timeframes the strategy reads: resampled from the base klines (default) or {SYMBOL}_{tf}.csv",
    )
    parser.add_argument("--symbols", nargs="*", help="symbols to replay (default: every CSV in --data)")
    parser.add_argument("--start", help="first cross candle date, UTC (YYYY-MM-DD)")
    parser.add_argument("--end", help="cross candles before this date, UTC (exclusive)")
    parser.add_argument("--oos-start", help="crosses from this date on are out-of-sample")
    parser.add_argument(
        "--warmup",
        type=int,
        help="candles of history a signal needs in the file (default: the strategy's warmup, as a live scan)",
    )
    parser.add_argument("--out", type=Path, help="write the JSON report here")
    parser.add_argument("--trades-out", type=Path, help="write every replayed trade as CSV here")
    parser.add_argument("--baseline", type=Path, help="fail unless the report matches this baseline")
    parser.add_argument(
        "--update-baseline", action="store_true", help="write the report's gate metrics to --baseline instead"
    )
    parser.add_argument("--purpose", choices=TRIAL_PURPOSES, default="research", help="why this run was made")
    parser.add_argument("--trials-log", type=Path, help="multiple-testing log (default research/trials.jsonl)")
    parser.add_argument("--trial-ts", help="timestamp to log the run under (ISO, UTC; default now)")
    parser.add_argument(
        "--record-trial", action="store_true", help="also store the run in research_trials (needs DATABASE_URL)"
    )
    args = parser.parse_args(argv)

    try:
        strategy = registry.get(args.strategy)
    except (KeyError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2
    database_url = os.getenv("DATABASE_URL", "").strip()
    if args.record_trial and not database_url:
        print("--record-trial needs DATABASE_URL", file=sys.stderr)
        return 2
    needed = [strategy.timeframe, *(strategy.context_timeframes if args.context_source == "csv" else ())]
    symbols = _symbols(args.symbols, args.data, strategy.timeframe)
    if not symbols:
        print(f"no {strategy.timeframe} CSV files in {args.data}", file=sys.stderr)
        return 2
    missing = [f"{s}_{tf}" for s in symbols for tf in needed if not (args.data / f"{s}_{tf}.csv").is_file()]
    if missing:
        print(f"missing data for {', '.join(missing)} in {args.data}", file=sys.stderr)
        return 2

    report, trades = run_backtest(
        args.data,
        symbols,
        args.start,
        args.end,
        args.oos_start,
        args.warmup,
        strategy=strategy,
        context_source=args.context_source,
    )
    print(f"{strategy.ref}  params {strategy.params_hash[:12]}  code {strategy.code_hash[:12]}")
    _print_summary(report)
    entry = trial_entry(report, purpose=args.purpose, ts=args.trial_ts)
    print(f"trial logged to {append_trial(entry, args.trials_log)}")
    if args.record_trial:
        trial_id = asyncio.run(_record_trial(entry, normalize_database_url(database_url)))
        print(f"trial stored as research_trials.id {trial_id}")
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    if args.trades_out:
        _write_trades(args.trades_out, trades)

    if args.baseline and args.update_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(baseline_from_report(report), indent=2) + "\n")
        print(f"baseline written to {args.baseline}")
    elif args.baseline:
        differences = compare_to_baseline(report, json.loads(args.baseline.read_text()))
        if differences:
            print(f"BACKTEST GATE FAILED: report differs from {args.baseline}", file=sys.stderr)
            for line in differences:
                print(f"  {line}", file=sys.stderr)
            print(
                "If the change is intended, regenerate the baseline with --update-baseline and commit it.",
                file=sys.stderr,
            )
            return 1
        print(f"backtest gate passed against {args.baseline}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
