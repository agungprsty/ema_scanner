import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import firebase_admin
from firebase_admin import credentials, firestore

from google.cloud.firestore_v1.base_query import FieldFilter
from src.config.settings import FIREBASE_CRED_PATH, FIREBASE_CRED_JSON

_db = None


def _load_cred():
    if FIREBASE_CRED_JSON:
        import json
        return credentials.Certificate(json.loads(FIREBASE_CRED_JSON))
    if FIREBASE_CRED_PATH:
        return credentials.Certificate(FIREBASE_CRED_PATH)
    raise RuntimeError(
        "Firebase credentials not configured. "
        "Set FIREBASE_CRED_JSON or FIREBASE_CRED_PATH in environment."
    )


def init_firebase() -> None:
    global _db
    if _db is not None:
        return
    cred = _load_cred()
    firebase_admin.initialize_app(cred)
    _db = firestore.client()


def get_db():
    if _db is None:
        init_firebase()
    return _db


def create_trade(symbol: str, side: str, tf: str, entry: float, sl: float, tp: float, qty: float, atr: float, bep: float = 0.0) -> str:
    db = get_db()
    trade_id = str(uuid.uuid4())
    doc = {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "tf": tf,
        "prices": {
            "entry_target": entry,
            "stop_loss": sl,
            "take_profit": tp,
            "bep": bep,
        },
        "metrics": {
            "atr_value": atr,
            "qty_coins": qty,
        },
        "status": "PENDING",
        "binance_order_id": None,
        "timestamps": {
            "signal_generated": datetime.now(timezone.utc).isoformat(),
            "order_placed": None,
            "filled_at": None,
            "tp1_hit_at": None,
            "closed_at": None,
        },
    }
    db.collection("active_trades").document(trade_id).set(doc)
    return trade_id


def update_trade_status(trade_id: str, status: str, **extra) -> None:
    db = get_db()
    tx = db.transaction()

    @firestore.transactional
    def _update(transaction):
        ref = db.collection("active_trades").document(trade_id)
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return
        update_data = {"status": status, **extra}
        if status == "LIMIT_PLACED":
            update_data["timestamps.order_placed"] = datetime.now(timezone.utc).isoformat()
        elif status == "FILLED":
            update_data["timestamps.filled_at"] = datetime.now(timezone.utc).isoformat()
        elif status == "TP1_HIT":
            update_data["timestamps.tp1_hit_at"] = datetime.now(timezone.utc).isoformat()
        elif status in ("CLOSED_SL", "CLOSED_TP", "CLOSED_BEP", "EXPIRED_CANCELLED"):
            update_data["timestamps.closed_at"] = datetime.now(timezone.utc).isoformat()
        transaction.update(ref, update_data)

    _update(tx)


def get_active_trades() -> list[dict]:
    db = get_db()
    docs = db.collection("active_trades").where(filter=FieldFilter("status", "==", "LIMIT_PLACED")).stream()
    return [doc.to_dict() for doc in docs]


def _compute_pnl_pct(trade: dict) -> float:
    prices = trade.get("prices", {})
    entry = prices.get("entry_target", 0)
    close_price = prices.get("take_profit") or prices.get("bep") or prices.get("stop_loss", entry)
    side = trade.get("side", "LONG")

    if side == "LONG":
        pnl_pct = (close_price - entry) / entry * 100
    else:
        pnl_pct = (entry - close_price) / entry * 100

    fee_pct = float(os.getenv("LONG_ENTRY_FEE_PCT", "0.05")) if side == "LONG" else float(os.getenv("SHORT_ENTRY_FEE_PCT", "0.05"))
    pnl_pct -= fee_pct
    return round(pnl_pct, 2)


def _compute_duration(trade: dict) -> float:
    ts = trade.get("timestamps", {})
    filled_at = ts.get("filled_at")
    closed_at = ts.get("closed_at")

    if not filled_at or not closed_at:
        return 0.0

    try:
        f_dt = datetime.fromisoformat(filled_at.replace("Z", "+00:00"))
        c_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        return round((c_dt - f_dt).total_seconds() / 3600, 1)
    except Exception:
        return 0.0


def _compute_duration_str(trade: dict) -> str:
    hours = _compute_duration(trade)
    if hours < 1:
        return f"{hours * 60:.0f}m"
    elif hours < 24:
        return f"{hours:.1f}h"
    else:
        return f"{hours / 24:.1f}d"


def get_all_trades(
    symbol: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    sort_by: str = "timestamps.closed_at",
    sort_order: str = "desc",
) -> dict:
    db = get_db()
    query = db.collection("active_trades")

    if symbol:
        query = query.where(filter=FieldFilter("symbol", "==", symbol))
    if status:
        query = query.where(filter=FieldFilter("status", "==", status))

    order_field = sort_by or "timestamps.closed_at"
    direction = firestore.Query.DESCENDING if sort_order == "desc" else firestore.Query.ASCENDING
    query = query.order_by(order_field, direction=direction)

    docs_snapshot = query.limit(limit + 1).offset(offset).stream()
    trades_raw = [doc.to_dict() for doc in docs_snapshot]

    total = len(trades_raw) - 1 if len(trades_raw) > limit else len(trades_raw)
    trades = trades_raw[:limit]

    enriched = []
    for t in trades:
        enriched.append({
            "trade_id": t.get("trade_id"),
            "symbol": t.get("symbol"),
            "side": t.get("side"),
            "timeframe": t.get("tf"),
            "signal_reason": t.get("signal_reason", ""),
            "entry_price": t.get("prices", {}).get("entry_target"),
            "sl_price": t.get("prices", {}).get("stop_loss"),
            "tp1_price": t.get("prices", {}).get("take_profit"),
            "bep_price": t.get("prices", {}).get("bep"),
            "quantity": t.get("metrics", {}).get("qty_coins"),
            "status": t.get("status"),
            "binance_order_id": t.get("binance_order_id"),
            "duration_hours": _compute_duration(t),
            "duration_str": _compute_duration_str(t),
            "pnl_pct": _compute_pnl_pct(t),
            "timestamps": t.get("timestamps", {}),
        })

    return {"total": total, "trades": enriched}


def get_trade_summary(symbol: Optional[str] = None) -> dict:
    db = get_db()
    query = db.collection("active_trades")

    if symbol:
        query = query.where(filter=FieldFilter("symbol", "==", symbol))

    docs = list(query.stream())
    trades_raw = [doc.to_dict() for doc in docs]

    closed = [t for t in trades_raw if t.get("status", "").startswith("CLOSED")]
    active = [t for t in trades_raw if not t.get("status", "").startswith("CLOSED")]

    total_trades = len(trades_raw)
    closed_count = len(closed)
    active_count = len(active)

    wins = [t for t in closed if t.get("status") in ("CLOSED_TP", "CLOSED_BEP")]
    losses = [t for t in closed if t.get("status") == "CLOSED_SL"]
    beps = [t for t in closed if t.get("status") == "CLOSED_BEP"]

    win_rate = round(len(wins) / closed_count * 100, 1) if closed_count > 0 else 0.0

    pnl_values = [_compute_pnl_pct(t) for t in closed]
    avg_pnl = round(sum(pnl_values) / len(pnl_values), 2) if pnl_values else 0.0
    best_pnl = max(pnl_values) if pnl_values else 0.0
    worst_pnl = min(pnl_values) if pnl_values else 0.0

    durations = [_compute_duration(t) for t in closed]
    avg_duration = round(sum(durations) / len(durations), 1) if durations else 0.0

    by_status = {}
    for t in trades_raw:
        s = t.get("status", "UNKNOWN")
        by_status[s] = by_status.get(s, 0) + 1

    by_side = {}
    for t in trades_raw:
        s = t.get("side", "UNKNOWN")
        by_side[s] = by_side.get(s, 0) + 1

    return {
        "total_trades": total_trades,
        "closed_trades": closed_count,
        "active_trades": active_count,
        "win_rate_pct": win_rate,
        "avg_pnl_pct": avg_pnl,
        "best_trade_pct": best_pnl,
        "worst_trade_pct": worst_pnl,
        "avg_duration_hours": avg_duration,
        "by_status": by_status,
        "by_side": by_side,
    }
