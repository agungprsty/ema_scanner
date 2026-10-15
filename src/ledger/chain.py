"""The signal event hash chain: canonical JSON, event hashes and the verifier.

Every row of signal_events is linked to the one before it (by id, across all signals):

    hash = sha256(prev_hash || canonical_json({"id", "signal_id", "event_type", "occurred_at", "payload"}))

with prev_hash = GENESIS_HASH for the first event. Changing, removing or reordering any event changes
every hash after it, so a published head (ledger_heads, /api/ledger/head) pins the whole history.

This module is pure: no database, no clock, no settings. The server, the CLI (python -m src.ledger)
and anyone holding an export recompute hashes with the same functions.

Canonical JSON is the payload as PostgreSQL JSONB returns it, so a hash computed before the insert
equals one recomputed from the stored row: keys sorted, no whitespace, ASCII only, and numbers
normalized (a float with an integral value is written as an integer, since JSONB stores 1.0 as a
numeric that reads back as 1; NaN and infinities are refused).
"""

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

GENESIS_HASH = "0" * 64
EVENT_TYPES = ("detected", "entered", "resolved", "restated", "alert_sent", "excluded")
# The fields an event hash covers; anything else stored on the row (recorded_at) is not part of it
CORE_FIELDS = ("id", "signal_id", "event_type", "occurred_at", "payload")


def normalize(value):
    """`value` as JSONB would give it back: dict keys as strings, tuples as lists, datetimes as
    ISO text (UTC, milliseconds) and integral floats as ints."""
    if isinstance(value, Mapping):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"cannot hash a non-finite number: {value!r}")
        return int(value) if value.is_integer() else value
    if isinstance(value, datetime):
        return iso_ms(value)
    if isinstance(value, date):
        return value.isoformat()
    # numpy scalars and the like
    if hasattr(value, "item"):
        return normalize(value.item())
    raise TypeError(f"cannot hash a value of type {type(value).__name__}")


def canonical_json(value) -> str:
    return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iso_ms(dt: datetime) -> str:
    """UTC ISO-8601 with milliseconds, e.g. 2026-10-15T06:00:15.123Z (naive values are taken as UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def truncate_ms(dt: datetime) -> datetime:
    """`dt` cut to whole milliseconds, so the stored timestamp and the hashed text agree."""
    return dt.replace(microsecond=dt.microsecond - dt.microsecond % 1000)


def event_core(event_id, signal_id, event_type: str, occurred_at, payload) -> dict:
    occurred = occurred_at if isinstance(occurred_at, str) else iso_ms(occurred_at)
    return {
        "id": int(event_id) if event_id is not None else None,
        "signal_id": int(signal_id) if signal_id is not None else None,
        "event_type": event_type,
        "occurred_at": occurred,
        "payload": normalize(payload or {}),
    }


def event_hash(prev_hash: str, core: dict) -> str:
    return sha256_hex(prev_hash + canonical_json(core))


def candles_sha256(rows) -> str:
    """SHA-256 of candle rows in canonical JSON (the order given)."""
    return sha256_hex(canonical_json(rows))


@dataclass
class ChainReport:
    """What verify_chain found. `first_bad_event` is the id of the first event that does not check
    out (None when every event does), and `reason` says why."""

    ok: bool = True
    events_checked: int = 0
    head_event_id: int | None = None
    head_hash: str = GENESIS_HASH
    first_bad_event: int | None = None
    reason: str | None = None
    hashes: dict[int, str] = field(default_factory=dict, repr=False)

    def fail(self, event_id: int | None, reason: str) -> "ChainReport":
        self.ok = False
        self.first_bad_event = event_id
        self.reason = reason
        return self

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "events_checked": self.events_checked,
            "head_event_id": self.head_event_id,
            "head_hash": self.head_hash,
            "first_bad_event": self.first_bad_event,
            "reason": self.reason,
        }


class ChainVerifier:
    """Checks events one at a time, in ascending id order: each must link to the one before it and
    hash to what it claims. After the first failure every later event is ignored.

    Each event is a mapping with id, signal_id, event_type, occurred_at (ISO text or datetime),
    payload, prev_hash and hash. `keep_hashes` keeps every verified id -> hash, for checking
    published heads against the chain.
    """

    def __init__(self, keep_hashes: bool = False):
        self.report = ChainReport()
        self._keep_hashes = keep_hashes
        self._prev_hash, self._prev_id = GENESIS_HASH, None

    def add(self, ev: Mapping) -> bool:
        report = self.report
        if not report.ok:
            return False
        event_id = ev.get("id")
        if not isinstance(event_id, int) or isinstance(event_id, bool):
            report.fail(None, f"event without an integer id after id {self._prev_id}")
            return False
        if self._prev_id is not None and event_id <= self._prev_id:
            report.fail(event_id, f"ids out of order: {event_id} after {self._prev_id}")
            return False
        if ev.get("event_type") not in EVENT_TYPES:
            report.fail(event_id, f"unknown event_type {ev.get('event_type')!r}")
            return False
        if ev.get("prev_hash") != self._prev_hash:
            report.fail(event_id, "prev_hash does not match the hash of the event before it")
            return False
        try:
            core = event_core(event_id, ev.get("signal_id"), ev["event_type"], ev.get("occurred_at"), ev.get("payload"))
            expected = event_hash(self._prev_hash, core)
        except (TypeError, ValueError, KeyError) as exc:
            report.fail(event_id, f"cannot recompute the hash: {exc}")
            return False
        if ev.get("hash") != expected:
            report.fail(event_id, "hash does not match the event's contents")
            return False
        self._prev_hash, self._prev_id = expected, event_id
        report.events_checked += 1
        report.head_event_id, report.head_hash = event_id, expected
        if self._keep_hashes:
            report.hashes[event_id] = expected
        return True


def verify_chain(events: Iterable[Mapping], keep_hashes: bool = False) -> ChainReport:
    """Verify a whole chain (see ChainVerifier); stops at the first event that fails."""
    verifier = ChainVerifier(keep_hashes)
    for ev in events:
        if not verifier.add(ev):
            break
    return verifier.report


def check_heads(heads: Iterable[Mapping], hashes: Mapping[int, str]) -> list[dict]:
    """Published daily heads that disagree with the verified chain: the event they name is missing
    or hashes differently. `hashes` comes from verify_chain(..., keep_hashes=True)."""
    bad = []
    for head in heads:
        last_id = head.get("last_event_id")
        if last_id is None:
            continue
        actual = hashes.get(int(last_id))
        if actual != head.get("head_hash"):
            bad.append({
                "date": str(head.get("date")),
                "last_event_id": int(last_id),
                "reason": "event missing from the chain" if actual is None else "head_hash differs from the chain",
            })
    return bad
