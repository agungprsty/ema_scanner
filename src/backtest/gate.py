"""Regression gate: a backtest report must reproduce the committed baseline exactly.

Any change to the strategy, its parameters or the cost model moves these numbers, so the PR that
makes it has to regenerate backtest/baseline.json and show the new figures in its diff.
"""

import copy

GATE_METRICS = ("n", "mean_net_r", "total_net_r")
GATE_DECIMALS = 4


def _rounded(value):
    return round(value, GATE_DECIMALS) if isinstance(value, float) else value


def baseline_from_report(report: dict) -> dict:
    dataset = report["dataset"]
    # A copy, so editing the baseline (or the report) later cannot change the other
    return copy.deepcopy({
        "harness_version": report["harness_version"],
        "dataset": {
            "symbols": dataset["symbols"],
            "start": dataset["start"],
            "end": dataset["end"],
            "oos_start": dataset["oos_start"],
            "warmup_candles": dataset["warmup_candles"],
            "sha256": {sym: f["sha256"] for sym, f in dataset["files"].items()},
        },
        "params": report["params"],
        "costs": report["costs"],
        "metrics": {
            name: {m: _rounded(s.get(m)) for m in GATE_METRICS} for name, s in report["scenarios"].items()
        },
    })


def _diff(path: str, expected, actual, out: list[str]) -> None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual), key=str):
            _diff(f"{path}.{key}", expected.get(key), actual.get(key), out)
    elif expected != actual:
        out.append(f"{path}: baseline {expected!r} != current {actual!r}")


def compare_to_baseline(report: dict, baseline: dict) -> list[str]:
    """Every field where `report` departs from `baseline`; empty when the gate passes."""
    differences: list[str] = []
    _diff("baseline", baseline, baseline_from_report(report), differences)
    return differences
