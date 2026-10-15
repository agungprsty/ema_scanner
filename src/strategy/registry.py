"""Registered strategies by (key, version), and the rule that decides whether one may emit signals.

A version is frozen when `versions.lock.json` (committed next to this file) records its params_hash
and code_hash. On boot the registry is mirrored into the strategy_versions table; a frozen row there
never changes its hashes again. The scheduled scan only emits for a live or shadow version whose
stored row is frozen and whose hashes equal those of the running code, and logs and skips any other.
Without a database the lock file stands in for the stored rows. tests/test_strategy_registry.py fails
when the code drifts from the lock, so a change to a frozen strategy is caught before it ships: give
it a new version (or, for a refactor the backtest gate proves byte-identical, record the old hash
under the entry's code_hash_history; see src/strategy/README.md).
"""

import json
import logging
from pathlib import Path

from src.strategy.base import RUNNABLE_STATUSES, STRATEGY_STATUSES, Strategy
from src.strategy.macd_cross import MacdCross
from src.strategy.macd_cross_ema50d_oppexit import MacdCrossEma50dOppExit

logger = logging.getLogger(__name__)

LOCK_PATH = Path(__file__).with_name("versions.lock.json")

_REGISTRY: dict[tuple[str, str], Strategy] = {}

# What evaluates stored rows whose (key, version) is not registered, such as "legacy" rows: the
# rules that have booked every outcome so far
DEFAULT_REF = "macd_cross@2.2.0"


def parse_ref(ref: str) -> tuple[str, str]:
    """("macd_cross", "2.2.0") from "macd_cross@2.2.0"."""
    key, sep, version = (ref or "").strip().partition("@")
    if not sep or not key or not version:
        raise ValueError(f"strategy reference must look like key@version, not {ref!r}")
    return key, version


def register(strategy: Strategy, replace: bool = False) -> Strategy:
    if strategy.status not in STRATEGY_STATUSES:
        raise ValueError(f"{strategy.ref}: status must be one of {STRATEGY_STATUSES}")
    ident = (strategy.key, strategy.version)
    if ident in _REGISTRY and not replace:
        raise ValueError(f"{strategy.ref} is already registered")
    _REGISTRY[ident] = strategy
    return strategy


def unregister(key: str, version: str) -> None:
    _REGISTRY.pop((key, version), None)


def lookup(key: str | None, version: str | None) -> Strategy | None:
    return _REGISTRY.get((key or "", version or ""))


def get(ref: str) -> Strategy:
    key, version = parse_ref(ref)
    strategy = lookup(key, version)
    if strategy is None:
        known = ", ".join(sorted(s.ref for s in _REGISTRY.values())) or "none"
        raise KeyError(f"{ref} is not registered (registered: {known})")
    return strategy


def all_strategies() -> list[Strategy]:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def runnable() -> list[Strategy]:
    """Strategies the scheduled scan runs: live first, then shadow."""
    order = {status: i for i, status in enumerate(RUNNABLE_STATUSES)}
    return sorted(
        (s for s in all_strategies() if s.status in order), key=lambda s: (order[s.status], s.key, s.version)
    )


def resolver_for(key: str | None, version: str | None) -> Strategy | None:
    """The strategy that settles a stored signal: its own when registered; DEFAULT_REF for an
    unregistered version of DEFAULT_REF's own strategy (legacy rows, versions from before the
    registry, rows without a key); None for any other unregistered strategy, which must not be
    settled by rules it never had."""
    strategy = lookup(key, version)
    if strategy is not None:
        return strategy
    default = get(DEFAULT_REF)
    return default if not key or key == default.key else None


def load_lock(path: Path = LOCK_PATH) -> dict[str, dict]:
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


def lock_record(strategy: Strategy, lock: dict[str, dict] | None = None) -> dict | None:
    """The lock entry of `strategy` as a stored row would carry it, or None when it is not frozen."""
    entry = (load_lock() if lock is None else lock).get(strategy.ref)
    if entry is None:
        return None
    return {"frozen": True, "params_hash": entry["params_hash"], "code_hash": entry["code_hash"]}


def emission_refusal(strategy: Strategy, record: dict | None) -> str | None:
    """Why `strategy` may not emit signals given its stored strategy_versions row; None when it may."""
    if strategy.status not in RUNNABLE_STATUSES:
        return f"status is {strategy.status}"
    if record is None:
        return "no strategy_versions row"
    if not record.get("frozen"):
        return "version is not frozen"
    if record.get("params_hash") != strategy.params_hash:
        return f"params_hash {strategy.params_hash[:12]} differs from frozen {str(record.get('params_hash'))[:12]}"
    if record.get("code_hash") != strategy.code_hash:
        return f"code_hash {strategy.code_hash[:12]} differs from frozen {str(record.get('code_hash'))[:12]}"
    return None


def emitting_strategies(stored: dict[str, dict] | None, lock: dict[str, dict] | None = None) -> tuple[
    list[Strategy], dict[str, str]
]:
    """(strategies allowed to emit, {ref: refusal reason}) among the runnable ones.

    `stored` maps "key@version" to its strategy_versions row; None means no database, and the lock
    file is used in its place. Every refusal is logged as an error.
    """
    allowed: list[Strategy] = []
    refused: dict[str, str] = {}
    for strategy in runnable():
        record = stored.get(strategy.ref) if stored is not None else lock_record(strategy, lock)
        reason = emission_refusal(strategy, record)
        if reason is None:
            allowed.append(strategy)
        else:
            refused[strategy.ref] = reason
            logger.error("Strategy %s refused to emit signals: %s", strategy.ref, reason)
    return allowed, refused


register(MacdCross())
# Pre-registered shadow candidate (docs/research/preregistration-macd_cross_ema50d_oppexit-1.0.0.md)
register(MacdCrossEma50dOppExit())
