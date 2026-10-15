"""Read side of the signal ledger for the API: chain head, per-signal events, persisted candle
windows, server-side verification and the CSV / JSONL exports.

Writes (events, candle windows, daily heads) live in src.services.db, next to the state changes they
record, so each commits in the same transaction as its change.
"""

import asyncio
import csv
import io
import json
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import desc, func, select, text

from src.config.settings import STRATEGY_KEY, STRATEGY_VERSION
from src.ledger.candles import chart_candles
from src.ledger.chain import GENESIS_HASH, canonical_json, iso_ms, normalize
from src.ledger.verify import verify_database
from src.services import db
from src.services.db import (
    COUNTED_RESULTS,
    LedgerHeadModel,
    MacdSignalModel,
    ProfitLossHistoryModel,
    SignalCandlesModel,
    SignalEventModel,
    exclusion_reason,
)

logger = logging.getLogger(__name__)

# Published daily heads returned with the live head
HEAD_HISTORY_DAYS = 30
# A verification (and an export) is reused while the ledger tables are unchanged, for at most this long
VERIFY_CACHE_SECONDS = 300
EXPORT_CACHE_SECONDS = 60

# A cheap fingerprint of every table verification reads: row counts, highest ids and the newest
# writing transaction (xmin) of each. Any insert, update or delete moves it, a change made with the
# append-only trigger disabled included, so a cached result is never served for a ledger that changed
STATE_FINGERPRINT_SQL = """
    SELECT
        (SELECT count(*)::text || ':' || coalesce(max(id), 0)::text || ':' || coalesce(max(xmin::text::bigint), 0)::text
           FROM signal_events),
        (SELECT count(*)::text || ':' || coalesce(max(xmin::text::bigint), 0)::text FROM ledger_heads),
        (SELECT count(*)::text || ':' || coalesce(max(xmin::text::bigint), 0)::text FROM signal_candles),
        (SELECT count(*)::text || ':' || coalesce(max(id), 0)::text || ':' || coalesce(max(xmin::text::bigint), 0)::text
           FROM macd_signals),
        (SELECT count(*)::text || ':' || coalesce(max(id), 0)::text || ':' || coalesce(max(xmin::text::bigint), 0)::text
           FROM profitloss_history)
"""

LEDGER_COLUMNS = (
    "signal_id",
    "strategy_key",
    "strategy_version",
    "symbol",
    "side",
    "timeframe",
    "cross_candle_ms",
    "cross_time",
    "detected_at",
    "source",
    "entry_basis",
    "is_eligible",
    "exclusion_reason",
    "counted",
    "headline",
    "win_status",
    "entry_price",
    "stop_loss",
    "tp1",
    "risk_reward_ratio",
    "exit_price",
    "exit_time",
    "result",
    "booked_by",
    "pnl_percent",
    "r_multiple",
    "gross_r",
    "cost_r",
    "net_r",
    "cost_model_version",
    "slippage_bps",
    "fee_percent",
    "input_candles_sha256",
    "candles_sha256",
    "event_count",
    "last_event_id",
    "last_event_hash",
)

COLUMN_NOTES = {
    "is_eligible": "on time and entered at the next open (source cron, entry_basis next_open)",
    "exclusion_reason": "why the signal is not in its own version's track record: legacy, catchup, manual, "
    "outside_ledger (no ledger events: stored outside the app) or other",
    "counted": "an eligible signal whose trade the evaluator booked with a net R: what /api/performance counts",
    "headline": f"the version the headline statistics report on ({STRATEGY_KEY}@{STRATEGY_VERSION})",
    "r_multiple": "R as booked at the time; gross_r, cost_r and net_r restate every trade under cost_model_version",
    "last_event_hash": "hash of the signal's latest event in the chain (GET /api/signals/{id}/events)",
}


def _session():
    if db._session_maker is None:
        return None
    return db._session_maker()


async def _chain_head(session) -> dict:
    head = (
        await session.execute(
            select(SignalEventModel.id, SignalEventModel.hash, SignalEventModel.occurred_at)
            .order_by(SignalEventModel.id.desc())
            .limit(1)
        )
    ).first()
    count = (await session.execute(select(func.count(SignalEventModel.id)))).scalar_one() or 0
    if head is None:
        return {"event_id": None, "hash": GENESIS_HASH, "occurred_at": None, "event_count": 0}
    return {"event_id": head.id, "hash": head.hash, "occurred_at": iso_ms(head.occurred_at), "event_count": count}


async def get_ledger_head() -> dict | None:
    """The live chain head and the latest published daily heads; None without a database."""
    maker = _session()
    if maker is None:
        return None
    async with maker as session:
        head = await _chain_head(session)
        daily = (
            await session.execute(select(LedgerHeadModel).order_by(desc(LedgerHeadModel.day)).limit(HEAD_HISTORY_DAYS))
        ).scalars()
        return {"head": head, "genesis_hash": GENESIS_HASH, "daily": [d.to_dict() for d in daily.all()]}


async def get_signal_events(signal_id: int) -> list[dict] | None:
    """A signal's events, oldest first; None when the signal does not exist."""
    maker = _session()
    if maker is None:
        return None
    async with maker as session:
        exists = (
            await session.execute(select(MacdSignalModel.id).where(MacdSignalModel.id == signal_id))
        ).scalar_one_or_none()
        if exists is None:
            return None
        rows = (
            await session.execute(
                select(SignalEventModel).where(SignalEventModel.signal_id == signal_id).order_by(SignalEventModel.id)
            )
        ).scalars()
        return [{**r.to_dict(), "recorded_at": iso_ms(r.recorded_at) if r.recorded_at else None} for r in rows.all()]


# Most events the recent-events feed returns at once
RECENT_EVENTS_MAX = 50


def _event_summary(ev, sig) -> dict:
    """An event as the recent-events feed shows it: what happened to which signal, with its hashes."""
    payload = ev.payload or {}
    return {
        "id": ev.id,
        "signal_id": ev.signal_id,
        "event_type": ev.event_type,
        "occurred_at": iso_ms(ev.occurred_at),
        "hash": ev.hash,
        "prev_hash": ev.prev_hash,
        "symbol": sig.symbol if sig is not None else None,
        "side": sig.side if sig is not None else None,
        "strategy_key": sig.strategy_key if sig is not None else None,
        "strategy_version": sig.strategy_version if sig is not None else None,
        "source": sig.source if sig is not None else None,
        "result": payload.get("result"),
        "net_r": payload.get("net_r"),
        "reason": payload.get("reason"),
        "kind": payload.get("kind"),
    }


async def get_recent_events(limit: int = 10) -> list[dict] | None:
    """The newest events of the chain, newest first, each with its signal's symbol and strategy."""
    maker = _session()
    if maker is None:
        return None
    limit = max(1, min(int(limit), RECENT_EVENTS_MAX))
    async with maker as session:
        rows = (
            await session.execute(
                select(SignalEventModel, MacdSignalModel)
                .outerjoin(MacdSignalModel, MacdSignalModel.id == SignalEventModel.signal_id)
                .order_by(SignalEventModel.id.desc())
                .limit(limit)
            )
        ).all()
        return [_event_summary(ev, sig) for ev, sig in rows]


async def get_signal_candle_window(signal_id: int) -> dict | None:
    """The candle window persisted when the signal resolved, as chart candles; None when there is none."""
    maker = _session()
    if maker is None:
        return None
    try:
        async with maker as session:
            row = await session.get(SignalCandlesModel, signal_id)
            if row is None:
                return None
            return {
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "candles": chart_candles(row.candles, row.interval_ms, row.resolved_at_ms),
                "persisted": True,
                "sha256": row.sha256,
                "resolved_at_ms": row.resolved_at_ms,
            }
    except Exception:
        logger.exception("Failed to read the persisted candles of signal %s", signal_id)
        return None


async def ledger_state(session) -> str:
    """STATE_FINGERPRINT_SQL as one string: equal only while no ledger table has changed."""
    return "|".join(str(v) for v in (await session.execute(text(STATE_FINGERPRINT_SQL))).one())


_verify_cache: dict = {"state": None, "at": 0.0, "result": None}
# One verification at a time: concurrent cold requests wait for it instead of each running their own
_verify_lock = asyncio.Lock()


async def verify_ledger(force: bool = False) -> dict | None:
    """Server-side verification (src.ledger.verify), reused while the ledger tables are unchanged
    (ledger_state) for at most VERIFY_CACHE_SECONDS. `cached_for_seconds` says how old it is."""
    maker = _session()
    if maker is None:
        return None
    async with _verify_lock, maker as session:
        state = await ledger_state(session)
        cached = _verify_cache["result"]
        age = time.monotonic() - _verify_cache["at"]
        if not force and cached is not None and age < VERIFY_CACHE_SECONDS and _verify_cache["state"] == state:
            return {**cached, "cached_for_seconds": int(age)}
        head = await _chain_head(session)
        result = await verify_database(session)
        result = {**result, "verified_at": iso_ms_now(), "event_count": head["event_count"]}
        _verify_cache.update(state=state, at=time.monotonic(), result=result)
    return {**result, "cached_for_seconds": 0}


def iso_ms_now() -> str:
    return iso_ms(datetime.now(UTC))


def _ledger_row(sig, trade, candles_sha, stats) -> dict:
    reason = exclusion_reason(sig.source, sig.entry_basis)
    count, last_id, last_hash = stats or (0, None, None)
    if reason is None and not count:
        # Stored outside the app: never counted (db.in_ledger), and verification reports it
        reason = "outside_ledger"
    counted = bool(
        reason is None
        and trade is not None
        and trade.booked_by == "evaluator"
        and trade.result in COUNTED_RESULTS
        and trade.net_r is not None
        and sig.win_status != "PENDING"
    )
    t = (lambda name: getattr(trade, name)) if trade is not None else (lambda name: None)
    return {
        "signal_id": sig.id,
        "strategy_key": sig.strategy_key,
        "strategy_version": sig.strategy_version,
        "symbol": sig.symbol,
        "side": sig.side,
        "timeframe": sig.timeframe,
        "cross_candle_ms": sig.cross_candle_ms,
        "cross_time": sig.cross_time,
        "detected_at": iso_ms(sig.detected_at) if sig.detected_at else None,
        "source": sig.source,
        "entry_basis": sig.entry_basis,
        "is_eligible": bool(sig.is_eligible),
        "exclusion_reason": reason,
        "counted": counted,
        "headline": (sig.strategy_key, sig.strategy_version) == (STRATEGY_KEY, STRATEGY_VERSION),
        "win_status": sig.win_status,
        "entry_price": sig.entry_price,
        "stop_loss": sig.stop_loss,
        "tp1": sig.tp1,
        "risk_reward_ratio": sig.risk_reward_ratio,
        "exit_price": t("exit_price"),
        "exit_time": t("exit_time"),
        "result": t("result"),
        "booked_by": t("booked_by"),
        "pnl_percent": t("pnl_percent"),
        "r_multiple": t("r_multiple"),
        "gross_r": t("gross_r"),
        "cost_r": t("cost_r"),
        "net_r": t("net_r"),
        "cost_model_version": t("cost_model_version"),
        "slippage_bps": t("slippage_bps"),
        "fee_percent": t("fee_percent"),
        "input_candles_sha256": sig.input_candles_sha256,
        "candles_sha256": candles_sha,
        "event_count": count,
        "last_event_id": last_id,
        "last_event_hash": last_hash,
    }


async def _ledger_rows(session) -> list[dict]:
    sig, pl, sc, ev = MacdSignalModel, ProfitLossHistoryModel, SignalCandlesModel, SignalEventModel
    stats_q = (
        select(ev.signal_id, func.count(ev.id).label("n"), func.max(ev.id).label("last_id"))
        .where(ev.signal_id.is_not(None))
        .group_by(ev.signal_id)
        .subquery()
    )
    stats_rows = (
        await session.execute(
            select(stats_q.c.signal_id, stats_q.c.n, stats_q.c.last_id, ev.hash).join(ev, ev.id == stats_q.c.last_id)
        )
    ).all()
    stats = {r.signal_id: (r.n, r.last_id, r.hash) for r in stats_rows}
    rows = (
        await session.execute(
            select(sig, pl, sc.sha256)
            .outerjoin(pl, pl.signal_id == sig.id)
            .outerjoin(sc, sc.signal_id == sig.id)
            .order_by(sig.id)
        )
    ).all()
    return [_ledger_row(s, trade, sha, stats.get(s.id)) for s, trade, sha in rows]


def filter_rows(rows: list[dict], strategy_key: str | None = None, strategy_version: str | None = None) -> list[dict]:
    """Ledger rows of one strategy (and version), legacy, late and manual rows included."""
    return [
        r
        for r in rows
        if (not strategy_key or r["strategy_key"] == strategy_key)
        and (not strategy_version or r["strategy_version"] == strategy_version)
    ]


# The public exports rebuild the whole ledger, so each is built once per ledger state (and at most
# every EXPORT_CACHE_SECONDS), one build at a time, however many requests arrive
_export_cache: dict[str, tuple[str, float, object]] = {}
_export_lock = asyncio.Lock()


def reset_caches() -> None:
    """Forget cached verifications and exports (tests, or after swapping databases)."""
    _verify_cache.update(state=None, at=0.0, result=None)
    _export_cache.clear()


async def _cached_export(name: str, build):
    maker = _session()
    if maker is None:
        return None
    async with _export_lock, maker as session:
        state = await ledger_state(session)
        hit = _export_cache.get(name)
        if hit is not None and hit[0] == state and time.monotonic() - hit[1] < EXPORT_CACHE_SECONDS:
            return hit[2]
        value = await build(session)
        _export_cache[name] = (state, time.monotonic(), value)
        return value


async def export_rows(strategy_key: str | None = None, strategy_version: str | None = None) -> list[dict] | None:
    rows = await _cached_export("rows", _ledger_rows)
    return None if rows is None else filter_rows(rows, strategy_key, strategy_version)


def _csv_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value) if isinstance(value, float) else str(value)


def rows_to_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(LEDGER_COLUMNS)
    for row in rows:
        writer.writerow([_csv_value(row.get(col)) for col in LEDGER_COLUMNS])
    return out.getvalue()


async def export_jsonl() -> str | None:
    """The whole ledger as JSON lines: a header, every signal row, every event in chain order and
    every published daily head. `python -m src.ledger verify --jsonl` checks a saved copy."""
    return await _cached_export("jsonl", _build_jsonl)


async def _build_jsonl(session) -> str:
    head = await _chain_head(session)
    rows = await _ledger_rows(session)
    events = (await session.execute(select(SignalEventModel).order_by(SignalEventModel.id))).scalars().all()
    heads = (await session.execute(select(LedgerHeadModel).order_by(LedgerHeadModel.day))).scalars().all()
    header = {
        "type": "header",
        "format": "crossline-ledger/1",
        "generated_at": iso_ms_now(),
        "chain_head": head,
        "genesis_hash": GENESIS_HASH,
        "hash_rule": "hash = sha256(prev_hash + canonical_json({id, signal_id, event_type, occurred_at, payload}))",
        "columns": list(LEDGER_COLUMNS),
        "column_notes": COLUMN_NOTES,
        "headline": f"{STRATEGY_KEY}@{STRATEGY_VERSION}",
    }
    lines = [json.dumps(header, sort_keys=True)]
    lines += [canonical_json({"type": "signal", **row}) for row in rows]
    lines += [canonical_json({"type": "event", **e.to_dict()}) for e in events]
    lines += [json.dumps({"type": "head", **normalize(h.to_dict())}, sort_keys=True) for h in heads]
    return "\n".join(lines) + "\n"
