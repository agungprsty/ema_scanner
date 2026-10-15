import html
import logging

from src.config.settings import TELEGRAM_OUTCOME_ALERTS
from src.services.telegram import send_alert

logger = logging.getLogger(__name__)

RESULT_LABELS = {
    "PROFIT": "target hit",
    "LOSS": "stopped out",
    "EXPIRED": "expired",
    "STOPPED": "stopped out",
    "OPPOSITE_CROSS": "closed on the opposite cross",
    "TIME_EXIT": "closed at the time stop",
}
CANDLE_HOURS = 2


def _price(value: float) -> str:
    return f"{value:,.2f}" if value >= 1000 else f"{value:.6g}"


def format_signal_alert(sig, late: bool | None = None) -> str:
    """One paragraph per new signal; `sig` is a MacdSignal.

    A late signal (by default, one whose cross is older than the latest closed candle) leads with
    a warning: its entry is a past price, so following it now means chasing.
    """
    if late is None:
        late = sig.bars_ago > 0
    warning = ""
    if late:
        if sig.bars_ago:
            age = f"crossed {sig.bars_ago * CANDLE_HOURS}h before the latest close"
        else:
            age = "found well after its entry candle opened"
        warning = (
            f"<b>LATE signal</b>: {age}. The entry below is a past price, so do not chase it. "
            "Not counted in the track record.\n"
        )
    # A strategy without a fixed target stores tp1 = 0 and exits by its own rule
    target = (
        f"TP1: {_price(sig.tp1)} (R:R 1:{sig.risk_reward_ratio:.1f})" if sig.tp1 else "Target: none (rule-based exit)"
    )
    return (
        f"{warning}<b>{sig.side}</b> #${html.escape(sig.symbol)} (2h)\n"
        f"Entry: {_price(sig.entry_price)}\n"
        f"Stop loss: {_price(sig.stop_loss)}\n"
        f"{target}\n"
        f"MACD: {sig.macd:.4f} | Signal: {sig.signal:.4f} | Hist: {sig.histogram:.4f}\n"
        f"Cross candle: {sig.cross_time}"
    )


def format_outcome_alert(closed: list[dict]) -> str:
    lines = ["<b>Crossline: trades closed</b>"]
    for t in closed:
        side = "Long" if t["direction"] == "LONG" else "Short"
        label = RESULT_LABELS.get(t["result"], t["result"].lower())
        r = t.get("r_multiple")
        r_text = f"{r:+.2f}R, " if r is not None else ""
        shadow = f"[shadow {html.escape(t['shadow_strategy'])}] " if t.get("shadow_strategy") else ""
        lines.append(
            f"{shadow}<b>{html.escape(t['symbol'])} {side}</b> {label}\n"
            f"Entry {_price(t['entry_price'])} → exit {_price(t['exit_price'])}\n"
            f"{r_text}{t['pnl_percent']:+.2f}% after fees and modelled slippage"
        )
    return "\n\n".join(lines)


async def notify_closed_trades(closed: list[dict] | None) -> bool:
    """Send one Telegram message for the trades an evaluation run just closed."""
    if not closed or not TELEGRAM_OUTCOME_ALERTS:
        return False
    try:
        return await send_alert(format_outcome_alert(closed))
    except Exception:
        logger.exception("Failed to send outcome alert")
        return False
