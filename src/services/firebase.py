import time
import uuid
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore

from google.cloud.firestore_v1.base_query import FieldFilter
from src.config.settings import FIREBASE_CRED_PATH, FIREBASE_CRED_JSON

_db = None
_trades_cache = {"data": None, "expires_at": 0}
_TRADES_CACHE_TTL = 30


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


def _invalidate_cache():
    _trades_cache["data"] = None
    _trades_cache["expires_at"] = 0


def create_trade(symbol: str, side: str, tf: str, entry: float, sl: float, tp: float, qty: float, atr: float, bep: float = 0.0) -> str:
    _invalidate_cache()
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
    _invalidate_cache()
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


def get_all_trades() -> list[dict]:
    now = time.time()
    if _trades_cache["data"] is not None and now < _trades_cache["expires_at"]:
        return _trades_cache["data"]
    db = get_db()
    docs = db.collection("active_trades").order_by("timestamps.signal_generated", direction=firestore.Query.DESCENDING).stream()
    data = [doc.to_dict() for doc in docs]
    _trades_cache["data"] = data
    _trades_cache["expires_at"] = now + _TRADES_CACHE_TTL
    return data


def get_trades_summary() -> dict:
    db = get_db()
    docs = db.collection("active_trades").stream()
    trades = [doc.to_dict() for doc in docs]
    total = len(trades)
    active = sum(1 for t in trades if t.get("status") in ("PENDING", "LIMIT_PLACED", "FILLED", "TP1_HIT"))
    closed = sum(1 for t in trades if t.get("status") in ("CLOSED_SL", "CLOSED_TP", "CLOSED_BEP", "EXPIRED_CANCELLED"))
    long_count = sum(1 for t in trades if t.get("side") == "LONG")
    short_count = sum(1 for t in trades if t.get("side") == "SHORT")
    wins = sum(1 for t in trades if t.get("status") in ("CLOSED_TP", "CLOSED_BEP"))
    losses = sum(1 for t in trades if t.get("status") == "CLOSED_SL")
    return {
        "total": total,
        "active": active,
        "closed": closed,
        "long": long_count,
        "short": short_count,
        "wins": wins,
        "losses": losses,
    }
