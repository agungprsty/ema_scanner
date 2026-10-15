"""Verify the ledger from a database or from an exported JSONL file.

Four checks, each reported on its own:
- chain: every event links to the one before it and hashes to what it claims (first_bad_event names
  the first that does not);
- heads: every published daily head names an event of the verified chain with that hash;
- candles (database only): every persisted candle window hashes to its stored sha256, which is also
  the one its signal's resolved event carries;
- rows: the live macd_signals / profitloss_history state of every signal equals what its latest
  snapshot event says, so a row changed outside the ledger shows up; a signal or trade the events
  record but whose row is gone (deleted outside the ledger) is reported as missing.

Once the chain breaks, the snapshots after the break cannot be trusted, so the rows section is not
checked (it says why), heads naming an event past the break are counted as unverifiable rather than
mismatched, and candle windows are only checked against their own stored hash.

Only SQL text and the pure chain module are used, so the CLI needs nothing but a database URL.
"""

import json
from collections.abc import Iterable, Mapping
from datetime import datetime

from sqlalchemy import text

from src.ledger.chain import ChainVerifier, candles_sha256, check_heads, iso_ms

EVENTS_SQL = (
    "SELECT id, signal_id, event_type, payload, occurred_at, prev_hash, hash FROM signal_events "
    "WHERE id > :after ORDER BY id LIMIT :limit"
)
HEADS_SQL = "SELECT date, last_event_id, head_hash FROM ledger_heads ORDER BY date"
CANDLES_SQL = "SELECT signal_id, candles, sha256 FROM signal_candles WHERE signal_id > :after ORDER BY signal_id LIMIT :limit"
ROWS_SQL = """
    SELECT s.id AS signal_id, s.symbol, s.side, s.timeframe, s.cross_candle_ms, s.entry_price, s.stop_loss, s.tp1,
           s.win_status, s.strategy_key, s.strategy_version, s.source, s.entry_basis,
           p.id AS trade_id, p.result, p.exit_price, p.exit_time, p.gross_r, p.cost_r, p.net_r,
           p.cost_model_version, p.booked_by
    FROM macd_signals s
    LEFT JOIN profitloss_history p ON p.signal_id = s.id
    ORDER BY s.id
"""
BATCH = 1000
# Lists in a report stop here; the counts beside them stay exact
MAX_LISTED = 50

SIGNAL_FIELDS = (
    "symbol", "side", "timeframe", "cross_candle_ms", "entry_price", "stop_loss", "tp1", "win_status",
    "strategy_key", "strategy_version", "source", "entry_basis",
)
TRADE_FIELDS = ("result", "exit_price", "exit_time", "gross_r", "cost_r", "net_r", "cost_model_version", "booked_by")


def _as_json(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def event_from_row(row: Mapping) -> dict:
    occurred = row["occurred_at"]
    return {
        "id": int(row["id"]),
        "signal_id": row["signal_id"],
        "event_type": row["event_type"],
        "payload": _as_json(row["payload"]),
        "occurred_at": iso_ms(occurred) if isinstance(occurred, datetime) else occurred,
        "prev_hash": row["prev_hash"],
        "hash": row["hash"],
    }


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-12 * max(1.0, abs(float(a)), abs(float(b)))
    return a == b


class LedgerVerifier:
    """Feeds the chain verifier and remembers, per signal, what the events say its state is."""

    def __init__(self, expected_heads: Iterable[str] = ()):
        self.chain = ChainVerifier(keep_hashes=True)
        # Hashes recorded elsewhere (a published daily head) that must be part of the verified chain
        self.expected_heads = [h.strip().lower() for h in expected_heads if h and h.strip()]
        # signal_id -> (event id, signal snapshot, trade snapshot or None) of its latest snapshot event
        self.latest: dict[int, tuple[int, dict, dict | None]] = {}
        # signal_id -> candle window sha256 named by its resolved event
        self.resolved_candles: dict[int, str] = {}
        self.heads_checked = 0
        self.heads_unverifiable = 0
        self.bad_heads: list[dict] = []
        self.candles_checked = 0
        self.bad_candles: list[dict] = []
        self.rows_checked = 0
        self.drifted: list[dict] = []
        self.without_events: list[int] = []
        self.seen_rows: set[int] = set()

    def add_event(self, ev: Mapping) -> bool:
        if not self.chain.add(ev):
            return False
        payload = ev.get("payload") or {}
        signal_id = ev.get("signal_id")
        if signal_id is not None and isinstance(payload.get("signal"), Mapping):
            self.latest[int(signal_id)] = (ev["id"], payload["signal"], payload.get("trade"))
        if signal_id is not None and ev.get("event_type") == "resolved":
            sha = (payload.get("candles") or {}).get("sha256")
            if sha:
                self.resolved_candles[int(signal_id)] = sha
        return True

    def check_heads(self, heads: Iterable[Mapping]) -> None:
        heads = list(heads)
        report = self.chain.report
        if not report.ok:
            # A head past the last verified event cannot be checked against a broken chain
            last_ok = report.head_event_id or 0
            beyond = [h for h in heads if h.get("last_event_id") is not None and int(h["last_event_id"]) > last_ok]
            self.heads_unverifiable += len(beyond)
            heads = [h for h in heads if h not in beyond]
        self.heads_checked += len(heads)
        self.bad_heads.extend(check_heads(heads, report.hashes))

    def check_candles(self, signal_id: int, candles, stored_sha: str) -> None:
        self.candles_checked += 1
        actual = candles_sha256(_as_json(candles))
        if actual != stored_sha:
            self.bad_candles.append({"signal_id": signal_id, "reason": "candles do not hash to the stored sha256"})
        elif not self.chain.report.ok:
            # The resolved events past a break are not verified, so there is nothing to compare with
            return
        elif signal_id not in self.resolved_candles:
            self.bad_candles.append({"signal_id": signal_id, "reason": "no resolved event names this window"})
        elif self.resolved_candles[signal_id] != actual:
            self.bad_candles.append({"signal_id": signal_id, "reason": "differs from the window its resolved event names"})

    def check_row(self, row: Mapping) -> None:
        """Compare one signal's live state (signal fields, plus trade fields when booked) with its
        latest snapshot event."""
        self.rows_checked += 1
        signal_id = int(row["signal_id"])
        self.seen_rows.add(signal_id)
        latest = self.latest.get(signal_id)
        if latest is None:
            self.without_events.append(signal_id)
            return
        event_id, signal, trade = latest
        fields = [f for f in SIGNAL_FIELDS if f in row and not _same(row[f], signal.get(f))]
        if trade is not None:
            if all(f not in row for f in TRADE_FIELDS):
                # The events record a booked trade, but its profitloss_history row is gone
                fields.append("trade.missing")
            else:
                fields += [f"trade.{f}" for f in TRADE_FIELDS if f in row and not _same(row[f], trade.get(f))]
        if fields:
            self.drifted.append({"signal_id": signal_id, "latest_event_id": event_id, "fields": fields})

    def missing_rows(self) -> list[dict]:
        """Signals the events record whose macd_signals row no longer exists (deleted outside the ledger)."""
        return [
            {"signal_id": signal_id, "latest_event_id": self.latest[signal_id][0]}
            for signal_id in sorted(set(self.latest) - self.seen_rows)
        ]

    def result(self, candles_checked: bool = True) -> dict:
        report = self.chain.report
        heads = {"checked": self.heads_checked, "ok": not self.bad_heads, "mismatched": self.bad_heads[:MAX_LISTED]}
        if not report.ok:
            heads["unverifiable"] = self.heads_unverifiable
            # Snapshots past the break are unverified: comparing rows with them would list false failures
            rows = {"ok": None, "skipped": f"chain broken at event {report.first_bad_event}"}
        else:
            missing = self.missing_rows()
            rows = {
                "checked": self.rows_checked,
                "ok": not self.drifted and not self.without_events and not missing,
                "drifted_count": len(self.drifted),
                "drifted": self.drifted[:MAX_LISTED],
                "without_events_count": len(self.without_events),
                "without_events": self.without_events[:MAX_LISTED],
                "missing_count": len(missing),
                "missing": missing[:MAX_LISTED],
            }
        sections = {"chain": report.to_dict(), "heads": heads, "rows": rows}
        if self.expected_heads:
            by_hash = {h: event_id for event_id, h in report.hashes.items()}
            found = [{"hash": h, "event_id": by_hash.get(h), "found": h in by_hash} for h in self.expected_heads]
            sections["expected_heads"] = {"ok": all(f["found"] for f in found), "heads": found}
        if candles_checked:
            sections["candles"] = {
                "checked": self.candles_checked,
                "ok": not self.bad_candles,
                "mismatched": self.bad_candles[:MAX_LISTED],
            }
        ok = report.ok and all(section["ok"] is not False for name, section in sections.items() if name != "chain")
        return {
            "ok": ok,
            "first_bad_event": report.first_bad_event,
            "head_event_id": report.head_event_id,
            "head_hash": report.head_hash,
            **sections,
        }


async def verify_database(conn, expected_heads: Iterable[str] = ()) -> dict:
    """Verify the ledger tables through `conn` (an AsyncConnection or AsyncSession)."""
    verifier = LedgerVerifier(expected_heads)
    after = 0
    while True:
        rows = (await conn.execute(text(EVENTS_SQL), {"after": after, "limit": BATCH})).mappings().all()
        if not rows:
            break
        for row in rows:
            if not verifier.add_event(event_from_row(row)):
                break
        if not verifier.chain.report.ok:
            break
        after = int(rows[-1]["id"])

    verifier.check_heads((await conn.execute(text(HEADS_SQL))).mappings().all())
    after = 0
    while True:
        rows = (await conn.execute(text(CANDLES_SQL), {"after": after, "limit": BATCH})).mappings().all()
        if not rows:
            break
        for row in rows:
            verifier.check_candles(int(row["signal_id"]), row["candles"], row["sha256"])
        after = int(rows[-1]["signal_id"])
    for row in (await conn.execute(text(ROWS_SQL))).mappings().all():
        row = dict(row)
        if row.pop("trade_id") is None:
            for f in TRADE_FIELDS:
                row.pop(f, None)
        verifier.check_row(row)
    return verifier.result()


async def verify_database_url(url: str, expected_heads: Iterable[str] = ()) -> dict:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await verify_database(conn, expected_heads)
    finally:
        await engine.dispose()


def verify_jsonl(lines: Iterable[str], expected_heads: Iterable[str] = ()) -> dict:
    """Verify an export of GET /api/ledger.jsonl: its events, its daily heads, and its signal rows
    against their events. Candle windows are not in the export, so they are not checked."""
    verifier = LedgerVerifier(expected_heads)
    heads, rows = [], []
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            verifier.chain.report.fail(None, f"line {number} is not JSON")
            break
        kind = record.get("type")
        if kind == "event":
            verifier.add_event(record)
        elif kind == "head":
            heads.append(record)
        elif kind == "signal":
            rows.append(record)
    verifier.check_heads(heads)
    for row in rows:
        verifier.check_row(_jsonl_row(row))
    return verifier.result(candles_checked=False)


def _jsonl_row(record: Mapping) -> dict:
    """A ledger export row as check_row reads it; trade fields only when the signal has a booked trade."""
    row = {"signal_id": record["signal_id"], **{f: record.get(f) for f in SIGNAL_FIELDS}}
    row["side"] = record.get("side")
    if record.get("booked_by") is not None:
        row.update({f: record.get(f) for f in TRADE_FIELDS})
    return row
