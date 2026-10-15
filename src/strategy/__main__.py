"""python -m src.strategy list | verify | freeze key@version --notes "..." | rehash key@version --reason "..."

list    every registered strategy with its status, hashes and whether the lock matches the code
verify  exit 1 unless every live/shadow strategy is frozen in the lock and matches the code
freeze  record a version's params_hash and code_hash in versions.lock.json (never overwrites)
rehash  for a frozen version whose code changed without changing results (prove it with the backtest
        gate first): move the old code_hash to code_hash_history and record the current one
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from src.strategy import registry
from src.strategy.base import RUNNABLE_STATUSES


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_lock(lock: dict, path: Path) -> None:
    path.write_text(json.dumps(dict(sorted(lock.items())), indent=2) + "\n")


def _state(strategy, lock: dict) -> str:
    entry = lock.get(strategy.ref)
    if entry is None:
        return "not frozen"
    if entry["params_hash"] != strategy.params_hash:
        return "PARAMS DRIFT"
    if entry["code_hash"] != strategy.code_hash:
        return "CODE DRIFT"
    return "frozen"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.strategy", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lock", type=Path, default=registry.LOCK_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("verify")
    freeze = sub.add_parser("freeze")
    freeze.add_argument("ref")
    freeze.add_argument("--notes", default="")
    freeze.add_argument("--at", help="freeze time to record (ISO, UTC); default now")
    rehash = sub.add_parser("rehash")
    rehash.add_argument("ref")
    rehash.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    lock = registry.load_lock(args.lock)

    if args.command == "list":
        for s in registry.all_strategies():
            print(f"{s.ref:28s} {s.status:8s} params {s.params_hash[:12]} code {s.code_hash[:12]}  {_state(s, lock)}")
        return 0

    if args.command == "verify":
        problems = [
            f"{s.ref}: {_state(s, lock)}"
            for s in registry.all_strategies()
            if s.status in RUNNABLE_STATUSES and _state(s, lock) != "frozen"
        ]
        problems += [
            f"{s.ref}: {_state(s, lock)}"
            for s in registry.all_strategies()
            if s.status not in RUNNABLE_STATUSES and _state(s, lock) in ("PARAMS DRIFT", "CODE DRIFT")
        ]
        for line in problems:
            print(line, file=sys.stderr)
        if not problems:
            print("every live and shadow strategy is frozen and matches its lock entry")
        return 1 if problems else 0

    try:
        strategy = registry.get(args.ref)
    except (KeyError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2

    if args.command == "freeze":
        entry = lock.get(strategy.ref)
        if entry is not None:
            if _state(strategy, lock) == "frozen":
                print(f"{strategy.ref} is already frozen with these hashes")
                return 0
            print(f"{strategy.ref} is frozen with other hashes ({_state(strategy, lock)}); a frozen version "
                  "never changes. Register a new version, or use rehash for a proven no-op refactor.", file=sys.stderr)
            return 1
        lock[strategy.ref] = {
            "params": strategy.params_dict,
            "params_hash": strategy.params_hash,
            "code_hash": strategy.code_hash,
            "code_modules": [type(strategy).__module__, *strategy.code_modules],
            "frozen_at": args.at or _now(),
            "notes": args.notes,
        }
        _write_lock(lock, args.lock)
        print(f"froze {strategy.ref} in {args.lock}")
        return 0

    # rehash
    entry = lock.get(strategy.ref)
    if entry is None:
        print(f"{strategy.ref} is not frozen; use freeze", file=sys.stderr)
        return 1
    if entry["params_hash"] != strategy.params_hash:
        print(f"{strategy.ref}: parameters changed; that needs a new version, not a rehash", file=sys.stderr)
        return 1
    if entry["code_hash"] == strategy.code_hash:
        print(f"{strategy.ref}: code_hash unchanged")
        return 0
    entry.setdefault("code_hash_history", []).append(
        {"code_hash": entry["code_hash"], "replaced_at": _now(), "reason": args.reason}
    )
    entry["code_hash"] = strategy.code_hash
    _write_lock(lock, args.lock)
    print(f"rehashed {strategy.ref} in {args.lock}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
