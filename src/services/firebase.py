import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import firebase_admin
from firebase_admin import credentials, firestore

from google.cloud.firestore_v1.base_query import FieldFilter
from src.config.settings import FIREBASE_CRED_PATH, FIREBASE_CRED_JSON
from src.services.redis_client import get_cached, set_cached, invalidate_trades_cache

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


def create_trade(
    symbol: str,
    side: str,
    tf: str,
    entry: float,
    sl: float,
    tp: float,
    qty: float,
    atr: float,
    bep: float = 0.0,
) -> str:
    db = get_db()

    existing = list(
        db.collection("active_trades")
        .where(filter=FieldFilter("symbol", "==", symbol))
        .where(filter=FieldFilter("side", "==", side))
        .where(
            filter=FieldFilter("status", "in", ["PENDING", "LIMIT_PLACED", "FILLED"])
        )
        .stream()
    )

    if existing:
        return existing[0].id

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
        "created_at": datetime.now(timezone.utc).isoformat(),
        "order_place_at": None,
        "filled_at": None,
        "tp1_hit_at": None,
        "closed_at": None,
    }
    db.collection("active_trades").document(trade_id).set(doc)
    invalidate_trades_cache()
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
            update_data["order_place_at"] = datetime.now(timezone.utc).isoformat()
        elif status == "FILLED":
            update_data["filled_at"] = datetime.now(timezone.utc).isoformat()
        elif status == "TP1_HIT":
            update_data["tp1_hit_at"] = datetime.now(timezone.utc).isoformat()
        elif status in ("CLOSED_SL", "CLOSED_TP", "CLOSED_BEP", "EXPIRED"):
            update_data["closed_at"] = datetime.now(timezone.utc).isoformat()
            if "exit_price" in extra:
                pass  # already included in update_data
        transaction.update(ref, update_data)

    _update(tx)
    invalidate_trades_cache()


def get_active_trades() -> list[dict]:
    db = get_db()
    docs = (
        db.collection("active_trades")
        .where(filter=FieldFilter("status", "==", "LIMIT_PLACED"))
        .stream()
    )
    return [doc.to_dict() for doc in docs]


def _compute_pnl_pct(trade: dict) -> float:
    status = trade.get("status", "")
    prices = trade.get("prices", {})
    entry = prices.get("entry_target", 0)
    exit_price = prices.get("exit_price")
    side = trade.get("side", "LONG")

    if not entry:
        return 0.0

    if status == "EXPIRED":
        return 0.0

    elif status == "CLOSED_SL":
        if exit_price is not None and exit_price > 0:
            close_price = exit_price
        else:
            close_price = prices.get("stop_loss", entry)
            sl_pnl = _compute_pnl_from_prices(entry, close_price, side)
            bep_pnl = _compute_pnl_from_prices(entry, prices.get("bep", 0), side)
            if bep_pnl > 0 and sl_pnl < 0:
                return round(bep_pnl - float(os.getenv("LONG_ENTRY_FEE_PCT", "0.05")), 2)

    elif status == "CLOSED_BEP":
        close_price = prices.get("bep", entry)
        if exit_price is not None and exit_price > 0:
            close_price = exit_price

    elif status == "CLOSED_TP":
        close_price = prices.get("take_profit", entry)
        if exit_price is not None and exit_price > 0:
            close_price = exit_price

    else:
        if exit_price is not None and exit_price > 0:
            close_price = exit_price
        else:
            close_price = (
                prices.get("take_profit") or prices.get("bep") or prices.get("stop_loss", entry)
            )

    return round(_compute_pnl_from_prices(entry, close_price, side), 2)


def _compute_pnl_from_prices(entry: float, close_price: float, side: str) -> float:
    if side == "LONG":
        pnl_pct = (close_price - entry) / entry * 100
    else:
        pnl_pct = (entry - close_price) / entry * 100

    fee_pct = (
        float(os.getenv("LONG_ENTRY_FEE_PCT", "0.05"))
        if side == "LONG"
        else float(os.getenv("SHORT_ENTRY_FEE_PCT", "0.05"))
    )
    pnl_pct -= fee_pct
    return pnl_pct


def _compute_duration(trade: dict) -> float:
    filled_at = trade.get("filled_at")
    closed_at = trade.get("closed_at")

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


def _calculate_rr_planned(trade: dict) -> float:
    prices = trade.get("prices", {})
    entry = prices.get("entry_target", 0)
    tp = prices.get("take_profit", 0)
    sl = prices.get("stop_loss", 0)

    if not entry or not tp or not sl:
        return 0.0

    side = trade.get("side", "LONG")
    if side == "LONG":
        risk = entry - sl
        reward = tp - entry
    else:
        risk = sl - entry
        reward = entry - tp

    return round(reward / risk, 2) if risk > 0 else 0.0


def _calculate_rr_actual(trade: dict) -> float:
    prices = trade.get("prices", {})
    entry = prices.get("entry_target", 0)
    exit_price = prices.get("exit_price")
    sl = prices.get("stop_loss", 0)

    if not entry or not sl:
        return 0.0

    actual_exit = exit_price if (exit_price and exit_price > 0) else entry
    side = trade.get("side", "LONG")

    if side == "LONG":
        risk = entry - sl
        reward = actual_exit - entry
    else:
        risk = sl - entry
        reward = entry - actual_exit

    return round(reward / risk, 2) if risk > 0 else 0.0


def get_all_trades(
    symbol: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
    cursor: Optional[str] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    cache_key = f"trades:{symbol or ''}:{status or ''}:{limit}:{cursor or ''}:{sort_by}:{sort_order}:{date_from or ''}:{date_to or ''}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    db = get_db()
    query = db.collection("active_trades")

    if symbol:
        query = query.where(filter=FieldFilter("symbol", "==", symbol))
    if status:
        query = query.where(filter=FieldFilter("status", "==", status))

    order_field = sort_by or "created_at"
    direction = (
        firestore.Query.DESCENDING
        if sort_order == "desc"
        else firestore.Query.ASCENDING
    )
    query = query.order_by(order_field, direction=direction)

    if cursor:
        query = query.start_after({order_field: cursor})

    docs_snapshot = query.limit(limit + 1).stream()
    trades_raw = [doc.to_dict() for doc in docs_snapshot]

    if date_from or date_to:
        filtered_trades = []
        for t in trades_raw:
            created = t.get("created_at", "")
            if not created:
                continue
            try:
                trade_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:
                filtered_trades.append(t)
                continue

            if date_from and trade_dt < datetime.fromisoformat(date_from):
                continue
            if date_to and trade_dt > datetime.fromisoformat(date_to):
                continue
            filtered_trades.append(t)
        trades_raw = filtered_trades

    has_more = len(trades_raw) > limit
    trades = trades_raw[:limit]

    next_cursor = None
    if has_more and trades:
        next_cursor = trades[-1].get(order_field)

    enriched = []
    for t in trades:
        prices = t.get("prices", {})
        entry = prices.get("entry_target")
        exit_price = prices.get("exit_price")
        status = t.get("status", "")

        enriched.append(
            {
                "trade_id": t.get("trade_id"),
                "symbol": t.get("symbol"),
                "side": t.get("side"),
                "timeframe": t.get("tf"),
                "signal_reason": t.get("signal_reason", ""),
                "entry_price": entry,
                "sl_price": prices.get("stop_loss"),
                "tp1_price": prices.get("take_profit"),
                "bep_price": prices.get("bep"),
                "exit_price": exit_price if (exit_price and exit_price > 0) else None,
                "quantity": t.get("metrics", {}).get("qty_coins"),
                "status": status,
                "binance_order_id": t.get("binance_order_id"),
                "duration_hours": _compute_duration(t),
                "duration_str": _compute_duration_str(t),
                "pnl_pct": _compute_pnl_pct(t),
                "rr_planned": _calculate_rr_planned(t),
                "rr_actual": _calculate_rr_actual(t),
                "created_at": t.get("created_at"),
                "order_place_at": t.get("order_place_at"),
                "filled_at": t.get("filled_at"),
                "tp1_hit_at": t.get("tp1_hit_at"),
                "closed_at": t.get("closed_at"),
            }
        )

    result = {"trades": enriched, "next_cursor": next_cursor, "has_more": has_more}
    set_cached(cache_key, result)
    return result


def get_trade_summary(symbol: Optional[str] = None) -> dict:
    cache_key = f"summary:{symbol or ''}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

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

    max_drawdown = 0.0
    if pnl_values:
        cumulative = 0.0
        peak = 0.0
        for pnl in pnl_values:
            cumulative += pnl
            if cumulative > peak:
                peak = cumulative
            drawdown = peak - cumulative
            if drawdown > max_drawdown:
                max_drawdown = drawdown
    max_drawdown = round(max_drawdown, 2)

    by_status = {}
    for t in trades_raw:
        s = t.get("status", "UNKNOWN")
        by_status[s] = by_status.get(s, 0) + 1

    by_side = {}
    for t in trades_raw:
        s = t.get("side", "UNKNOWN")
        by_side[s] = by_side.get(s, 0) + 1

    result = {
        "total_trades": total_trades,
        "closed_trades": closed_count,
        "active_trades": active_count,
        "win_rate_pct": win_rate,
        "avg_pnl_pct": avg_pnl,
        "best_trade_pct": best_pnl,
        "worst_trade_pct": worst_pnl,
        "max_drawdown_pct": max_drawdown,
        "avg_duration_hours": avg_duration,
        "by_status": by_status,
        "by_side": by_side,
    }
    set_cached(cache_key, result)
    return result
