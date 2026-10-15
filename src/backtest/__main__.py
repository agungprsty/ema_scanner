"""python -m src.backtest --data data/klines --symbols BTCUSDT ETHUSDT --start 2025-01-01 \\
    --end 2026-01-01 --oos-start 2025-07-01 --out report.json [--baseline backtest/baseline.json]"""

import argparse
import csv
import json
import sys
from pathlib import Path

from src.backtest.gate import baseline_from_report, compare_to_baseline
from src.backtest.replay import PRODUCTION_WARMUP
from src.backtest.run import run_backtest
from src.config.settings import MACD_TIMEFRAME


def _symbols(values: list[str] | None, data_dir: Path) -> list[str]:
    if not values:
        suffix = f"_{MACD_TIMEFRAME}.csv"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.backtest", description=__doc__)
    parser.add_argument("--data", required=True, type=Path, help="directory of {SYMBOL}_2h.csv files")
    parser.add_argument("--symbols", nargs="*", help="symbols to replay (default: every CSV in --data)")
    parser.add_argument("--start", help="first cross candle date, UTC (YYYY-MM-DD)")
    parser.add_argument("--end", help="cross candles before this date, UTC (exclusive)")
    parser.add_argument("--oos-start", help="crosses from this date on are out-of-sample")
    parser.add_argument(
        "--warmup",
        type=int,
        default=PRODUCTION_WARMUP,
        help=f"candles of history a cross needs in the file (default {PRODUCTION_WARMUP}, as a live scan)",
    )
    parser.add_argument("--out", type=Path, help="write the JSON report here")
    parser.add_argument("--trades-out", type=Path, help="write every replayed trade as CSV here")
    parser.add_argument("--baseline", type=Path, help="fail unless the report matches this baseline")
    parser.add_argument(
        "--update-baseline", action="store_true", help="write the report's gate metrics to --baseline instead"
    )
    args = parser.parse_args(argv)

    symbols = _symbols(args.symbols, args.data)
    if not symbols:
        print(f"no {MACD_TIMEFRAME} CSV files in {args.data}", file=sys.stderr)
        return 2
    missing = [s for s in symbols if not (args.data / f"{s}_{MACD_TIMEFRAME}.csv").is_file()]
    if missing:
        print(f"missing data for {', '.join(missing)} in {args.data}", file=sys.stderr)
        return 2

    report, trades = run_backtest(args.data, symbols, args.start, args.end, args.oos_start, args.warmup)
    _print_summary(report)
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
