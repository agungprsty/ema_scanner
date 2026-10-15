from datetime import UTC, datetime
from pathlib import Path

from src.backtest.data import file_sha256, kline_csv_path, load_klines_csv
from src.backtest.metrics import (
    BOOTSTRAP_SAMPLES,
    BOOTSTRAP_SEED,
    SCENARIOS,
    book_trades,
    cost_model_description,
    scenario_report,
)
from src.backtest.replay import PRODUCTION_WARMUP, BacktestTrade, replay_symbol
from src.config.settings import (
    MACD_CANDLES,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    MACD_TIMEFRAME,
    RISK_REWARD_RATIO,
    SIGNAL_EXPIRY_CANDLES,
    SWING_LOOKBACK_CANDLES,
)

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


def strategy_params() -> dict:
    return {
        "timeframe": MACD_TIMEFRAME,
        "macd_fast": MACD_FAST,
        "macd_slow": MACD_SLOW,
        "macd_signal": MACD_SIGNAL,
        "macd_candles": MACD_CANDLES,
        "risk_reward_ratio": RISK_REWARD_RATIO,
        "swing_lookback_candles": SWING_LOOKBACK_CANDLES,
        "signal_expiry_candles": SIGNAL_EXPIRY_CANDLES,
    }


def run_backtest(
    data_dir: str | Path,
    symbols: list[str],
    start: str | None = None,
    end: str | None = None,
    oos_start: str | None = None,
    warmup: int = PRODUCTION_WARMUP,
) -> tuple[dict, list[BacktestTrade]]:
    """Replay `symbols` from `data_dir` and report every cost scenario. Returns (report, trades)."""
    start_ms, end_ms, oos_ms = date_to_ms(start), date_to_ms(end), date_to_ms(oos_start)
    trades: list[BacktestTrade] = []
    files = {}
    crosses = unresolved = 0
    for symbol in symbols:
        path = kline_csv_path(data_dir, symbol, MACD_TIMEFRAME)
        klines = load_klines_csv(path)
        replay = replay_symbol(klines, symbol, start_ms=start_ms, end_ms=end_ms, warmup=warmup)
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
        "dataset": {
            "symbols": list(symbols),
            "start": start,
            "end": end,
            "oos_start": oos_start,
            "warmup_candles": warmup,
            "files": files,
        },
        "params": strategy_params(),
        "costs": cost_model_description(symbols),
        "ci_method": {"bootstrap": "weekly_block", "samples": BOOTSTRAP_SAMPLES, "seed": BOOTSTRAP_SEED},
        "signals": {"crosses": crosses, "resolved": len(trades), "unresolved": unresolved},
        "scenarios": {
            name: scenario_report(book_trades(trades, scenario), oos_ms) for name, scenario in SCENARIOS.items()
        },
    }
    return report, trades
