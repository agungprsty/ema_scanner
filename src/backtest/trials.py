"""The multiple-testing log: one JSON line per harness run, appended to research/trials.jsonl.

Every configuration ever backtested is counted here, the ones that were dropped included, so a
result can be judged against how many tries it took (deflated t-stat). Lines are only appended,
never rewritten. Runs can also be stored in the research_trials table (--record-trial).

Lines with "backfilled": true record configurations tried outside this harness (the 2026-10-14
review's variants on 2024-10..2026-09), with their metrics as that source reports them; they have
no params or data hashes.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRIALS_LOG = REPO_ROOT / "research" / "trials.jsonl"
TRIAL_PURPOSES = ("research", "gate", "holdout", "robustness", "adhoc")
HEADLINE_METRICS = (
    "n",
    "mean_gross_r",
    "mean_net_r",
    "t_stat",
    "ci95_net_r",
    "total_net_r",
    "win_rate",
    "profit_factor",
    "max_drawdown_r",
)


def trials_log_path(path: str | Path | None = None) -> Path:
    """`path`, else $RESEARCH_TRIALS_LOG, else research/trials.jsonl in the repository."""
    return Path(path or os.getenv("RESEARCH_TRIALS_LOG") or DEFAULT_TRIALS_LOG)


def _round(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, list):
        return [_round(v) for v in value]
    return value


def _headline(summary: dict) -> dict:
    return {m: _round(summary.get(m)) for m in HEADLINE_METRICS}


def _utc_date(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).date().isoformat() if ms is not None else None


def trial_entry(report: dict, purpose: str = "research", ts: str | None = None) -> dict:
    """The log line for one harness report. `ts` (ISO, UTC) defaults to now."""
    if purpose not in TRIAL_PURPOSES:
        raise ValueError(f"purpose must be one of {TRIAL_PURPOSES}")
    strategy, dataset = report["strategy"], report["dataset"]
    files = dataset["files"]
    first = [f["first_open_ms"] for f in files.values() if f["first_open_ms"] is not None]
    last = [f["last_open_ms"] for f in files.values() if f["last_open_ms"] is not None]
    metrics = {name: _headline(s) for name, s in report["scenarios"].items()}
    for name, s in report["scenarios"].items():
        if "out_of_sample" in s:
            metrics[name]["out_of_sample"] = _headline(s["out_of_sample"])
    return {
        "ts": ts or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "strategy_key": strategy["key"],
        "version": strategy["version"],
        "params_hash": strategy["params_hash"],
        "code_hash": strategy["code_hash"],
        "params": report["params"],
        "purpose": purpose,
        "harness_version": report["harness_version"],
        "cost_model_version": report["costs"]["cost_model_version"],
        "symbols": dataset["symbols"],
        # Signal window as asked for, and the span of the data actually read
        "date_from": dataset["start"] or _utc_date(min(first, default=None)),
        "date_to": dataset["end"] or _utc_date(max(last, default=None)),
        "oos_start": dataset["oos_start"],
        "warmup_candles": dataset["warmup_candles"],
        "data_sha256": {sym: f["sha256"] for sym, f in files.items()},
        "context_source": strategy.get("context_source"),
        "signals": report["signals"],
        "metrics": metrics,
    }


def dataset_description(entry: dict) -> str:
    hashes = ", ".join(f"{sym} {sha[:12]}" for sym, sha in sorted(entry["data_sha256"].items()))
    context = f", context {entry['context_source']}" if entry.get("context_source") else ""
    return (
        f"{entry['params'].get('timeframe', '')} klines of {', '.join(entry['symbols'])} (sha256 {hashes}); "
        f"warmup {entry['warmup_candles']}, OOS from {entry['oos_start'] or '-'}{context}; "
        f"harness v{entry['harness_version']}, costs {entry['cost_model_version']}"
    )


def append_trial(entry: dict, path: str | Path | None = None) -> Path:
    target = trials_log_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
    return target


def research_trial_row(entry: dict) -> dict:
    """The research_trials row for a log line."""
    return {
        "strategy_key": entry["strategy_key"],
        "version": entry["version"],
        "params_hash": entry["params_hash"],
        "dataset_desc": dataset_description(entry),
        "date_from": entry["date_from"],
        "date_to": entry["date_to"],
        "metrics": {
            "signals": entry["signals"],
            "scenarios": entry["metrics"],
            "code_hash": entry["code_hash"],
            "logged_at": entry["ts"],
        },
        "purpose": entry["purpose"],
    }
