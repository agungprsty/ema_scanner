import html
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.config.settings import ROUND_TRIP_FEE_PERCENT, TELEGRAM_OUTCOME_ALERTS
from src.services.telegram import send_alert

logger = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))
TITLE = "<b>Simple Trade</b>"
SIDE_ICONS = {"LONG": "🟢", "SHORT": "🔴"}
RESULT_ICONS = {"TP_HIT": "🎯", "SL_HIT": "🛑", "EXPIRED": "⏱"}


def _decimals(price: float) -> int:
    """Decimals Binance quoted `price` with; a level rounded to them stays on the symbol's tick."""
    return max(0, -Decimal(repr(price)).as_tuple().exponent)


def _copy(value: float, decimals: int) -> str:
    """Price in <code>, which Telegram copies on tap; plain digits so it pastes into an order."""
    return f"<code>{value:.{decimals}f}</code>"


def _wib(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(WIB).strftime("%H:%M WIB %d %b")


def _pct(value: float) -> str:
    return f"{round(value, 2):+g}%"


def _result(row: dict) -> str:
    """'+2R (+2.00%)' for a closed trade."""
    pnl = f"{row['pnl_percent']:+.2f}%"
    r = row.get("r_multiple")
    return f"{round(r, 2):+g}R ({pnl})" if r is not None else pnl


def format_signal(row: dict) -> str:
    """One block per new signal, as stored after this run's evaluation, so a signal found late
    that already filled or closed says so instead of asking for a limit order."""
    entry = row["entry_price"]
    decimals = _decimals(entry)
    # Net of the round-trip fee, which both exits pay: the gain at TP and the loss at SL
    tp_net = abs(row["take_profit"] - entry) / entry * 100 - ROUND_TRIP_FEE_PERCENT
    sl_net = -abs(entry - row["stop_loss"]) / entry * 100 - ROUND_TRIP_FEE_PERCENT

    status = row["status"]
    if status == "PENDING":
        state = f"⏳ s/d {_wib(row['fill_deadline'])}"
    elif status == "OPEN":
        state = f"✅ Filled at {_copy(row['fill_price'], decimals)}, trade open"
    elif status == "NOT_FILLED":
        state = "⚪ Not filled, no trade"
    else:
        state = f"{RESULT_ICONS[status]} {_result(row)}"

    return (
        f"{SIDE_ICONS[row['side']]} <b>{row['side']} {html.escape(row['symbol'])}</b>\n"
        f"Entry {_copy(entry, decimals)}\n"
        f"TP {_copy(row['take_profit'], decimals)} {_pct(tp_net)}\n"
        f"SL {_copy(row['stop_loss'], decimals)} {_pct(sl_net)}\n"
        f"{state}"
    )


def format_signals(rows: list[dict]) -> str:
    return "\n\n".join(["<b>Simple Trade</b>", *(format_signal(r) for r in rows)])


def format_transitions(rows: list[dict]) -> str:
    """One line per trade that closed or was cancelled; fills are left to the exchange."""
    lines = [TITLE, ""]
    for row in rows:
        name = f"{html.escape(row['symbol'])} {row['side']}"
        if row["status"] == "NOT_FILLED":
            entry = row["entry_price"]
            lines.append(f"⚪ {name} not filled, cancel the limit at {_copy(entry, _decimals(entry))}")
        else:
            lines.append(f"{RESULT_ICONS[row['status']]} {name} {_result(row)}")
    return "\n".join(lines)


async def notify_run(new_rows: list[dict], transitions: list[dict]) -> bool:
    """Send a run's alerts: one message for new signals, then one for trades that closed.

    `new_rows` come from claim_unalerted_signals and `transitions` from evaluate_active; a new
    signal is shown with its post-evaluation state and is not repeated in the trade updates.
    Returns False when the signal message was not delivered, so its rows can be retried.
    """
    current = {t["id"]: t for t in transitions}
    new_ids = {r["id"] for r in new_rows}
    delivered = True
    if new_rows:
        try:
            delivered = await send_alert(format_signals([current.get(r["id"], r) for r in new_rows]))
        except Exception:
            delivered = False
            logger.exception("Failed to send signal alert")
    closed = [t for t in transitions if t["id"] not in new_ids and t["status"] != "OPEN"]
    if closed and TELEGRAM_OUTCOME_ALERTS:
        try:
            await send_alert(format_transitions(closed))
        except Exception:
            logger.exception("Failed to send trade update alert")
    return delivered
