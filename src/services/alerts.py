import html
import logging
from datetime import datetime, timedelta, timezone

from src.config.settings import (
    INTERVAL_MS,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    TELEGRAM_OUTCOME_ALERTS,
)
from src.services.db import window_candles
from src.services.telegram import send_alert

logger = logging.getLogger(__name__)

WIB = timezone(timedelta(hours=7))
RESULT_LABELS = {"TP_HIT": "target hit", "SL_HIT": "stopped out", "EXPIRED": "max hold reached"}


def _price(value: float) -> str:
    return f"{value:,.2f}" if value >= 1000 else f"{value:.6g}"


def _wib(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(WIB).strftime("%H:%M WIB %d %b")


def _duration(ms: int) -> str:
    for unit, size in (("d", 86_400_000), ("h", 3_600_000)):
        if ms % size == 0:
            return f"{ms // size}{unit}"
    return f"{ms // 60_000}m"


def _result(row: dict) -> str:
    """'target hit 60,000.00 → 61,200.00, +1.90R, +1.90% after fees' for a closed trade."""
    r = row.get("r_multiple")
    r_text = f", {r:+.2f}R" if r is not None else ""
    return (
        f"{RESULT_LABELS[row['status']]} {_price(row['fill_price'])} → {_price(row['exit_price'])}"
        f"{r_text}, {row['pnl_percent']:+.2f}% after fees"
    )


def format_signal(row: dict, now_ms: int) -> str:
    """One paragraph per new signal, as stored after this run's evaluation, so a signal found
    late that already filled or closed says so instead of asking for a limit order."""
    long = row["side"] == "LONG"
    interval_ms = INTERVAL_MS[row["timeframe"]]
    entry = row["entry_price"]
    tp_pct = abs(row["take_profit"] - entry) / entry * 100
    sl_pct = abs(entry - row["stop_loss"]) / entry * 100
    cross = f"crossed below {RSI_OVERSOLD:g}" if long else f"crossed above {RSI_OVERBOUGHT:g}"

    # Candles between the signal candle and the latest closed one
    bars_ago = now_ms // interval_ms - 1 - row["signal_candle_ms"] // interval_ms
    age = f" · signal {_duration(bars_ago * interval_ms)} before the latest close" if bars_ago > 0 else ""

    status = row["status"]
    if status == "PENDING":
        window = _duration(window_candles(interval_ms)[0] * interval_ms)  # hours rounded up to whole candles
        state = f"Fill by {_wib(row['fill_deadline'])} ({window} window), else cancelled"
    elif status == "OPEN":
        state = f"Already filled at {_price(row['fill_price'])} ({_wib(row['fill_time'])}), trade open"
    elif status == "NOT_FILLED":
        state = "Not filled within the window, no trade"
    else:
        state = f"Already closed: {_result(row)}"

    return (
        f"<b>{row['side']}</b> #${html.escape(row['symbol'])} ({row['timeframe']}) "
        f"RSI {row['rsi']:.1f}, {cross}{age}\n"
        f"Limit entry: {_price(entry)} (signal candle {'low' if long else 'high'})\n"
        f"TP {_price(row['take_profit'])} (+{tp_pct:g}%) · SL {_price(row['stop_loss'])} (-{sl_pct:g}%)"
        f" · R:R 1:{tp_pct / sl_pct:g}\n"
        f"{state}"
    )


def format_signals(rows: list[dict], now_ms: int) -> str:
    timeframes = ", ".join(sorted({r["timeframe"] for r in rows}))
    return "\n\n".join([f"<b>Crossline: RSI {timeframes} signals</b>", *(format_signal(r, now_ms) for r in rows)])


def format_transitions(rows: list[dict]) -> str:
    """One line per trade that closed or was cancelled; fills are left to the exchange."""
    lines = ["<b>Crossline: trade updates</b>"]
    for row in rows:
        side = "Long" if row["side"] == "LONG" else "Short"
        if row["status"] == "NOT_FILLED":
            text = f"not filled, cancel the limit at {_price(row['entry_price'])}"
        else:
            text = _result(row)
        lines.append(f"{html.escape(row['symbol'])} {side} {text}")
    return "\n".join(lines)


async def notify_run(new_rows: list[dict], transitions: list[dict], now_ms: int) -> bool:
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
            delivered = await send_alert(format_signals([current.get(r["id"], r) for r in new_rows], now_ms))
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
