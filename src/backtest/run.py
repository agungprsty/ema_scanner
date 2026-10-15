from datetime import UTC, datetime
from pathlib import Path

from src.backtest.data import file_sha256, kline_csv_path, load_context_klines, load_klines_csv
from src.backtest.metrics import (
    BOOTSTRAP_SAMPLES,
    BOOTSTRAP_SEED,
    SCENARIOS,
    book_trades,
    cost_model_description,
    scenario_report,
)
from src.backtest.replay import BacktestTrade, replay_strategy
from src.strategy import registry
from src.strategy.base import Strategy

# Bump when the harness itself changes how it counts, so old reports are not compared blindly
HARNESS_VERSION = 1


def date_to_ms(value: str | None) -> int | None:
    """Midnight UTC of a YYYY-MM-DD date (or a full ISO timestamp, read as UTC) in epoch ms."""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


# The strategy a run replays unless told otherwise: the one the Phase 0 gate baselines cover
DEFAULT_STRATEGY = registry.DEFAULT_REF


def run_backtest(
    data_dir: str | Path,
    symbols: list[str],
    start: str | None = None,
    end: str | None = None,
    oos_start: str | None = None,
    warmup: int | None = None,
    strategy: Strategy | str | None = None,
    context_source: str = "resample",
) -> tuple[dict, list[BacktestTrade]]:
    """Replay `strategy` (a registered one, default macd_cross@2.2.0) on `symbols` from `data_dir` and
    report every cost scenario. `warmup` defaults to the strategy's warmup_bars; context timeframes
    are resampled from the base klines or read from CSV (`context_source`). Returns (report, trades)."""
    if strategy is None or isinstance(strategy, str):
        strategy = registry.get(strategy or DEFAULT_STRATEGY)
    warmup = strategy.warmup_bars if warmup is None else warmup
    start_ms, end_ms, oos_ms = date_to_ms(start), date_to_ms(end), date_to_ms(oos_start)
    trades: list[BacktestTrade] = []
    files = {}
    context_files = {}
    crosses = unresolved = 0
    for symbol in symbols:
        path = kline_csv_path(data_dir, symbol, strategy.timeframe)
        klines = load_klines_csv(path)
        context, described = load_context_klines(
            data_dir, symbol, klines, strategy.timeframe, strategy.context_timeframes, context_source
        )
        if described:
            context_files[symbol] = described
        replay = replay_strategy(
            strategy, klines, symbol, start_ms=start_ms, end_ms=end_ms, warmup=warmup, context=context
        )
        trades.extend(replay.trades)
        unresolved += replay.unresolved
        crosses += len(replay.trades) + replay.unresolved
        files[symbol] = {
            "file": path.name,
            "rows": len(klines),
            "first_open_ms": int(klines["open_time_ms"].iloc[0]) if len(klines) else None,
            "last_open_ms": int(klines["open_time_ms"].iloc[-1]) if len(klines) else None,
            "sha256": file_sha256(path),
        }

    trades.sort(key=lambda t: (t.cross_candle_ms, t.symbol, t.side))
    report = {
        "harness_version": HARNESS_VERSION,
        "strategy": {
            "key": strategy.key,
            "version": strategy.version,
            "status": strategy.status,
            "params_hash": strategy.params_hash,
            "code_hash": strategy.code_hash,
            "context_timeframes": list(strategy.context_timeframes),
            "context_source": context_source if strategy.context_timeframes else None,
        },
        "dataset": {
            "symbols": list(symbols),
            "start": start,
            "end": end,
            "oos_start": oos_start,
            "warmup_candles": warmup,
            "files": files,
            **({"context_files": context_files} if context_files else {}),
        },
        "params": strategy.report_params(),
        "costs": cost_model_description(symbols),
        "ci_method": {"bootstrap": "weekly_block", "samples": BOOTSTRAP_SAMPLES, "seed": BOOTSTRAP_SEED},
        "signals": {"crosses": crosses, "resolved": len(trades), "unresolved": unresolved},
        "scenarios": {
            name: scenario_report(book_trades(trades, scenario), oos_ms) for name, scenario in SCENARIOS.items()
        },
    }
    return report, trades
