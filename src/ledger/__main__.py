"""Verify the signal ledger.

    python -m src.ledger verify                        # the database in DATABASE_URL
    python -m src.ledger verify --database-url URL     # any database (postgresql+asyncpg://...)
    python -m src.ledger verify --jsonl ledger.jsonl   # an export of GET /api/ledger.jsonl ("-" for stdin)
    python -m src.ledger verify --jsonl ledger.jsonl --head HASH   # HASH must be an event of the chain

Prints the report as JSON and exits 0 when every check passes, 1 when one fails, 2 on a usage error.
--head (repeatable) takes a head recorded elsewhere, such as a daily head copied from
/api/ledger/head on an earlier day: a chain that was rewritten from scratch, however consistent,
no longer contains it.
"""

import argparse
import asyncio
import json
import os
import sys

from src.ledger.verify import verify_database_url, verify_jsonl


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m src.ledger", description="Verify the signal ledger hash chain.")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="recompute the hash chain and the checks built on it")
    source = verify.add_mutually_exclusive_group()
    source.add_argument("--database-url", help="database to verify (default: $DATABASE_URL)")
    source.add_argument("--jsonl", help='export of GET /api/ledger.jsonl to verify, or "-" for stdin')
    verify.add_argument(
        "--head", action="append", default=[], help="a head hash recorded earlier that the chain must contain"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    if args.jsonl:
        if args.jsonl == "-":
            report = verify_jsonl(sys.stdin, args.head)
        else:
            with open(args.jsonl, encoding="utf-8") as fh:
                report = verify_jsonl(fh, args.head)
    else:
        url = args.database_url or os.getenv("DATABASE_URL")
        if not url:
            print("No database: pass --database-url or --jsonl, or set DATABASE_URL", file=sys.stderr)
            return 2
        report = asyncio.run(verify_database_url(url, args.head))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
