"""What the public pages show, computed from stored rows: page titles and Open Graph text, outcome
banners, the facts of a signal, its excursions, and the research documents and changelog the pages
link to. Pure functions except for the file readers, so the wording is tested without a browser.

Numbers follow the page grammar: signed, the minus sign U+2212, two decimals, an R suffix.
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from src.strategy import registry
from src.strategy.outcome import CANDLE_INTERVAL_MS

logger = logging.getLogger(__name__)

MINUS = "\u2212"
# Placeholder for a value that does not exist
DASH = "\u2013"
WIB = timedelta(hours=7)
ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = ROOT / "docs"
RESEARCH_DIR = DOCS_DIR / "research"
CHANGELOG_PATH = DOCS_DIR / "changelog.json"
# Files under docs/research the site serves (as plain text) at /research/{name}
RESEARCH_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*\.(md|json|csv)$")
HOLDOUT_RESULT = re.compile(r"^holdout-result-(?P<key>[a-z0-9_]+)-(?P<version>\d[\w.]*)\.md$")
VERDICT = re.compile(r"\|\s*Verdict\s*\|\s*\*\*(?P<verdict>PASS|FAIL)", re.IGNORECASE)

OUTCOME_LABELS = {
    "PENDING": "Open",
    "TP1_HIT": "Target hit",
    "SL_HIT": "Stopped out",
    "STOPPED": "Stopped out",
    "EXPIRED": "Expired",
    "OPPOSITE_CROSS": "Opposite-cross exit",
    "TIME_EXIT": "Time exit",
}
# Outcomes whose exit_time is the open of the candle that hit a level (that candle is part of the trade)
LEVEL_HITS = ("TP1_HIT", "SL_HIT", "STOPPED")

SOURCE_LABELS = {
    "cron": "Scheduled scan",
    "catchup": "Scheduled scan, late catch-up",
    "manual": "Manual scan",
    "legacy": "Legacy (pre-v2.2 rules)",
}
ENTRY_BASIS_LABELS = {
    "next_open": "Next candle open",
    "forming_close": "Close of the forming candle",
    "cross_interp": "Interpolated cross price (no achievable fill)",
}
EXCLUSION_NOTES = {
    "legacy": "Legacy: booked under pre-v2.2 rules, excluded from stats",
    "catchup": "Late: found after its entry candle had passed, excluded from stats",
    "manual": "Manual scan, excluded from stats",
    "other": "Not entered at the next open, excluded from stats",
}


def signed(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return DASH
    rounded = round(float(value), digits)
    sign = "+" if rounded > 0 else MINUS if rounded < 0 else ""
    return f"{sign}{abs(rounded):.{digits}f}{suffix}"


def signed_r(value: float | None, digits: int = 2) -> str:
    return signed(value, digits, "R")


def to_datetime(value) -> datetime | None:
    """An aware UTC datetime from a datetime, epoch milliseconds or stored text (zone optional)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, UTC)
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def format_wib(value) -> str:
    dt = to_datetime(value)
    if dt is None:
        return DASH
    return (dt.astimezone(UTC) + WIB).strftime("%Y-%m-%d %H:%M") + " WIB"


def iso_utc(value) -> str | None:
    dt = to_datetime(value)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def duration_text(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return DASH
    hours = int(seconds // 3600)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    minutes = int(seconds % 3600 // 60)
    return f"{hours}h" if hours or not minutes else f"{minutes}m"


def strategy_ref(sig: dict) -> str:
    return f"{sig.get('strategy_key') or 'macd_cross'}@{sig.get('strategy_version') or '?'}"


def strategy_status(sig: dict) -> str | None:
    strategy = registry.lookup(sig.get("strategy_key"), sig.get("strategy_version"))
    return strategy.status if strategy is not None else None


def hold_limit_candles(sig: dict) -> int:
    """Most candles the rules that settle this signal hold a trade (expiry or time exit)."""
    strategy = registry.resolver_for(sig.get("strategy_key"), sig.get("strategy_version"))
    if strategy is None:
        return 0
    params = strategy.params
    return int(getattr(params, "signal_expiry_candles", None) or getattr(params, "max_hold_candles", 0) or 0)


def interval_ms(sig: dict) -> int:
    return CANDLE_INTERVAL_MS.get(sig.get("timeframe") or "2h", CANDLE_INTERVAL_MS["2h"])


def expires_at_ms(sig: dict) -> int | None:
    """When an open signal closes at the latest: the close of its last allowed candle after the cross."""
    cross = sig.get("cross_candle_ms")
    limit = hold_limit_candles(sig)
    if cross is None or not limit:
        return None
    return int(cross) + (limit + 1) * interval_ms(sig)


def has_target(sig: dict) -> bool:
    return bool(sig.get("tp1"))


def achievable(sig: dict) -> bool:
    """Booked at the interpolated cross price, a fill nobody could get, a signal has no achievable result."""
    return sig.get("entry_basis") != "cross_interp"


def net_r(sig: dict) -> float | None:
    if sig.get("net_r") is not None:
        return sig["net_r"]
    return sig.get("r_multiple")


def is_open(sig: dict) -> bool:
    return (sig.get("win_status") or "PENDING") == "PENDING"


def result_text(sig: dict) -> str:
    """The result as a title shows it: OPEN, the signed net R, or CLOSED when there is no achievable R."""
    if is_open(sig):
        return "OPEN"
    r = net_r(sig)
    if r is None or not achievable(sig):
        return "CLOSED"
    return signed_r(r)


def page_title(sig: dict) -> str:
    """`{SYMBOL} {SIDE} · {±R | OPEN} · Crossline {strategy}@{version}`, the same for a win and a loss."""
    return f"{sig.get('symbol', '?')} {sig.get('side', '?')} · {result_text(sig)} · Crossline {strategy_ref(sig)}"


def outcome_label(status: str | None) -> str:
    return OUTCOME_LABELS.get(status or "PENDING", status or "Open")


def banner_text(sig: dict) -> str:
    """Neutral one-line outcome: "Stopped out {minus}1.07R (gross {minus}0.98R, cost {minus}0.09R)"."""
    label = outcome_label(sig.get("win_status"))
    if is_open(sig):
        expires = expires_at_ms(sig)
        exit_word = "expires" if has_target(sig) else "time exit"
        return f"{label}: {exit_word} {format_wib(expires)} at the latest" if expires else label
    if not achievable(sig):
        return f"{label}: entry was the interpolated cross price, so there is no achievable net result"
    if sig.get("net_r") is None:
        r = sig.get("r_multiple")
        return f"{label} {signed_r(r)} (as booked, before the cost model)" if r is not None else label
    parts = []
    if sig.get("gross_r") is not None:
        parts.append(f"gross {signed_r(sig['gross_r'])}")
    if sig.get("cost_r") is not None:
        parts.append(f"cost {signed_r(-sig['cost_r'])}")
    return f"{label} {signed_r(sig['net_r'])}" + (f" ({', '.join(parts)})" if parts else "")


def outcome_class(sig: dict) -> str:
    """Neutral styling hook: open, closed-up, closed-down or closed-flat (never a celebratory style)."""
    if is_open(sig):
        return "open"
    r = net_r(sig) if achievable(sig) else None
    if r is None:
        return "closed"
    return "up" if r > 0 else "down" if r < 0 else "flat"


def og_description(sig: dict) -> str:
    entry = sig.get("entry_price")
    text = f"{banner_text(sig)}. {sig.get('symbol')} {sig.get('side')} 2H"
    if entry:
        text += f", entry {entry:g}, stop {sig.get('stop_loss'):g}"
        if sig.get("tp1"):
            text += f", target {sig['tp1']:g}"
    return text + ". Research signal, no validated edge; not investment advice."


def exclusion_note(sig: dict) -> str | None:
    if sig.get("is_eligible"):
        return None
    source, basis = sig.get("source"), sig.get("entry_basis")
    if source == "cron" and basis == "next_open":
        return None
    if source in EXCLUSION_NOTES:
        return EXCLUSION_NOTES[source]
    return EXCLUSION_NOTES["other"]


def excursions(sig: dict, candles: list[dict] | None) -> dict | None:
    """Maximum adverse and favourable excursion in R over the candles the trade was held, from candle
    extremes (intrabar order unknown). `candles` are chart candles ({time (s), high, low}); None when
    the trade is open, has no achievable entry, or its candle window was not persisted."""
    if not candles or is_open(sig) or not achievable(sig):
        return None
    entry, stop = sig.get("entry_price"), sig.get("stop_loss")
    exit_dt = to_datetime(sig.get("exit_time"))
    if not entry or not stop or entry == stop or exit_dt is None or sig.get("cross_candle_ms") is None:
        return None
    step = interval_ms(sig)
    entry_open = int(sig["cross_candle_ms"]) + step
    exit_ms = int(exit_dt.timestamp() * 1000)
    include_exit = sig.get("win_status") in LEVEL_HITS
    held = [
        c
        for c in candles
        if entry_open <= c["time"] * 1000 < exit_ms or (include_exit and c["time"] * 1000 == exit_ms)
    ]
    if not held:
        return None
    risk = abs(entry - stop)
    direction = 1 if sig.get("side") == "LONG" else -1
    worst = min(direction * ((c["low"] if direction == 1 else c["high"]) - entry) for c in held) / risk
    best = max(direction * ((c["high"] if direction == 1 else c["low"]) - entry) for c in held) / risk
    return {"mae_r": round(min(worst, 0.0), 2), "mfe_r": round(max(best, 0.0), 2), "candles": len(held)}


def hold_seconds(sig: dict) -> float | None:
    if is_open(sig) or sig.get("cross_candle_ms") is None:
        return None
    exit_dt = to_datetime(sig.get("exit_time"))
    if exit_dt is None:
        return None
    entry_ms = int(sig["cross_candle_ms"]) + interval_ms(sig)
    return exit_dt.timestamp() - entry_ms / 1000


def held_text(sig: dict) -> str:
    """How long a closed trade was held: exact for exits at a candle open or close; for a target or
    stop, whose exit_time is the open of the candle that touched it, an upper bound ("≤ 4h"), or
    "within the entry candle"."""
    hold = hold_seconds(sig)
    if hold is None:
        return DASH
    if sig.get("win_status") in LEVEL_HITS:
        return "within the entry candle" if hold <= 0 else f"≤ {duration_text(hold + interval_ms(sig) / 1000)}"
    return duration_text(hold)


def _first_event(events: list[dict], event_type: str) -> dict | None:
    return next((ev for ev in events or [] if ev.get("event_type") == event_type), None)


def provenance(sig: dict, events: list[dict]) -> dict:
    """params_hash, code_hash and git SHA of the rules that produced a signal: from its detected event,
    else from the registered strategy."""
    detected = _first_event(events, "detected")
    payload = (detected or {}).get("payload") or {}
    strategy = registry.lookup(sig.get("strategy_key"), sig.get("strategy_version"))
    return {
        "params_hash": payload.get("params_hash") or (strategy.params_hash if strategy else None),
        "code_hash": payload.get("code_hash") or (strategy.code_hash if strategy else None),
        "git_sha": payload.get("git_sha"),
        "params_source": payload.get("params_source") or ("frozen" if strategy else None),
    }


def _pct_from(entry: float | None, level: float | None) -> str | None:
    if not entry or not level:
        return None
    return signed((level - entry) / entry * 100, 2, "%")


def signal_facts(sig: dict, events: list[dict], candles: list[dict] | None = None) -> list[dict]:
    """The facts grid of /s/{id}: label, value and an optional hint, in display order. Values are
    plain text (the template escapes them); `mono` marks numbers and hashes."""
    prov = provenance(sig, events)
    entry = sig.get("entry_price")
    # The first alert announcing the signal (an outcome alert comes later, at the close)
    alert = next(
        (e for e in events or []
         if e.get("event_type") == "alert_sent" and (e.get("payload") or {}).get("kind") != "outcome"),
        None,
    )
    resolved = _first_event(events, "resolved")
    exc = excursions(sig, candles)
    facts = [
        {"label": "Strategy", "value": strategy_ref(sig), "mono": True,
         "hint": (strategy_status(sig) or "not registered")},
        {"label": "Params hash", "value": (prov["params_hash"] or DASH), "mono": True, "hash": True,
         "hint": prov["params_source"] and f"{prov['params_source']} parameters"},
        {"label": "Source", "value": SOURCE_LABELS.get(sig.get("source"), sig.get("source") or DASH),
         "hint": exclusion_note(sig) or "Counted in its version's track record"},
        {"label": "Entry basis", "value": ENTRY_BASIS_LABELS.get(sig.get("entry_basis"), sig.get("entry_basis") or DASH)},
        {"label": "Cross candle", "value": format_wib(sig.get("cross_candle_ms")), "mono": True,
         "hint": "open time of the 2H candle the cross closed on"},
        {"label": "Detected", "value": format_wib(sig.get("detected_at")), "mono": True},
        {"label": "Alert sent", "value": format_wib(alert["occurred_at"]) if alert else DASH, "mono": True,
         "hint": None if alert else "no alert recorded (shadow, manual, or before the ledger)"},
        {"label": "Entry", "value": f"{entry:g}" if entry else DASH, "mono": True},
        {"label": "Stop", "value": f"{sig['stop_loss']:g}" if sig.get("stop_loss") else DASH, "mono": True,
         "hint": _pct_from(entry, sig.get("stop_loss"))},
        {"label": "Target", "value": f"{sig['tp1']:g}" if sig.get("tp1") else "none (exit rules)", "mono": True,
         "hint": _pct_from(entry, sig.get("tp1"))},
        {"label": "Exit", "value": f"{sig['exit_price']:g}" if sig.get("exit_price") is not None else DASH,
         "mono": True, "hint": format_wib(sig.get("exit_time")) if sig.get("exit_time") else None},
        {"label": "Held", "value": held_text(sig), "mono": True,
         "hint": f"limit {hold_limit_candles(sig)} candles"},
        {"label": "Gross R", "value": signed_r(sig.get("gross_r")) if achievable(sig) else DASH, "mono": True},
        {"label": "Cost R", "value": signed_r(-sig["cost_r"]) if achievable(sig) and sig.get("cost_r") is not None
         else DASH, "mono": True, "hint": (resolved or {}).get("payload", {}).get("cost_model_version")},
        {"label": "Net R", "value": signed_r(sig.get("net_r")) if achievable(sig) else DASH, "mono": True},
        {"label": "MAE / MFE", "value": f"{signed_r(exc['mae_r'])} / {signed_r(exc['mfe_r'])}" if exc else DASH,
         "mono": True, "hint": "from candle extremes while held" if exc else "needs the persisted candle window"},
    ]
    return facts


# Research documents and the changelog -----------------------------------------------------------


def research_file(name: str) -> Path | None:
    """A file of docs/research the site may serve, or None (unknown name, traversal, not a file)."""
    if not RESEARCH_FILE.match(name or ""):
        return None
    path = (RESEARCH_DIR / name).resolve()
    if path.parent != RESEARCH_DIR.resolve() or not path.is_file():
        return None
    return path


RESEARCH_PATH = re.compile(r"/research/([A-Za-z0-9][A-Za-z0-9_.@-]*\.(?:md|json|csv))")


def link_parts(text: str) -> list[dict]:
    """`text` split into plain parts and /research/{file} paths of files the site serves, so a template
    can link those paths while autoescaping everything (no |safe)."""
    parts, pos = [], 0
    text = str(text or "")
    for m in RESEARCH_PATH.finditer(text):
        if research_file(m.group(1)) is None:
            continue
        if m.start() > pos:
            parts.append({"text": text[pos : m.start()], "href": None})
        parts.append({"text": m.group(0), "href": m.group(0)})
        pos = m.end()
    if pos < len(text):
        parts.append({"text": text[pos:], "href": None})
    return parts


def holdout_results() -> dict[str, dict]:
    """{"key@version": {"file", "verdict"}} for every docs/research/holdout-result-*.md."""
    out: dict[str, dict] = {}
    if not RESEARCH_DIR.is_dir():
        return out
    for path in sorted(RESEARCH_DIR.glob("holdout-result-*.md")):
        m = HOLDOUT_RESULT.match(path.name)
        if not m:
            continue
        try:
            verdict = VERDICT.search(path.read_text(encoding="utf-8"))
        except OSError:
            verdict = None
        out[f"{m['key']}@{m['version']}"] = {
            "file": path.name,
            "url": f"/research/{path.name}",
            "verdict": verdict["verdict"].upper() if verdict else None,
        }
    return out


def research_documents() -> list[dict]:
    if not RESEARCH_DIR.is_dir():
        return []
    return [
        {"file": p.name, "url": f"/research/{p.name}"}
        for p in sorted(RESEARCH_DIR.iterdir())
        if research_file(p.name) is not None
    ]


@lru_cache(maxsize=1)
def _changelog_cached(mtime: float) -> dict:
    return json.loads(CHANGELOG_PATH.read_text(encoding="utf-8"))


def load_changelog() -> dict:
    """docs/changelog.json; an empty changelog when it is missing or unreadable."""
    try:
        return _changelog_cached(CHANGELOG_PATH.stat().st_mtime)
    except (OSError, ValueError):
        logger.exception("Could not read %s", CHANGELOG_PATH)
        return {"format": 1, "entries": []}


def strategy_versions(counts: list[dict] | None = None, stored: dict[str, dict] | None = None) -> list[dict]:
    """Every registered strategy version with its frozen hashes and lock notes, plus any version seen
    only in stored signals (older or legacy), each with its signal counts per source and holdout result.

    `stored` maps "key@version" to its strategy_versions row, the record that actually gates emission
    (None without a database: the lock file stands in, as it does for the scan). Each registered
    version says whether the scheduled scan may emit for it (`emitting`) and, when not, why
    (`refusal`): frozen hashes that differ from the running code show here, not only after a run."""
    counts = counts or []
    lock = registry.load_lock()
    holdouts = holdout_results()
    per_ref: dict[str, dict] = {}
    for row in counts:
        ref = f"{row.get('strategy_key') or '?'}@{row.get('strategy_version') or '?'}"
        entry = per_ref.setdefault(ref, {"total": 0, "open": 0, "by_source": {}})
        entry["total"] += row.get("count") or 0
        entry["open"] += row.get("open") or 0
        src = row.get("source") or "unknown"
        entry["by_source"][src] = entry["by_source"].get(src, 0) + (row.get("count") or 0)

    out = []
    for strategy in registry.all_strategies():
        info = strategy.describe()
        locked = lock.get(strategy.ref) or {}
        record = stored.get(strategy.ref) if stored is not None else registry.lock_record(strategy, lock)
        refusal = registry.emission_refusal(strategy, record)
        out.append({
            **info,
            "registered": True,
            "frozen": bool(record and record.get("frozen")),
            "frozen_params_hash": (record or {}).get("params_hash"),
            "frozen_code_hash": (record or {}).get("code_hash"),
            "record_source": "database" if stored is not None else "lock",
            "emitting": refusal is None,
            "refusal": refusal,
            "frozen_at": (record or {}).get("frozen_at") or locked.get("frozen_at"),
            "notes": locked.get("notes", ""),
            "description": strategy.description,
            "hold_limit_candles": hold_limit_candles({"strategy_key": strategy.key,
                                                      "strategy_version": strategy.version}),
            "has_target": "risk_reward_ratio" in info["params"],
            "signals": per_ref.get(strategy.ref, {"total": 0, "open": 0, "by_source": {}}),
            "holdout": holdouts.get(strategy.ref),
        })
    known = {s["ref"] for s in out}
    for ref, signals in sorted(per_ref.items()):
        if ref in known:
            continue
        key, _, version = ref.partition("@")
        out.append({
            "key": key, "version": version, "ref": ref, "name": key, "status": "retired", "registered": False,
            "frozen": False, "params": {}, "params_hash": None, "code_hash": None, "signals": signals,
            "emitting": False, "refusal": "not registered",
            "holdout": holdouts.get(ref), "notes": "", "description": "", "has_target": True,
        })
    order = {"live": 0, "shadow": 1, "research": 2, "retired": 3}
    return sorted(out, key=lambda s: (order.get(s["status"], 9), s["key"], s["version"]))
