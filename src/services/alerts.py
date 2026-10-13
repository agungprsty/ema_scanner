import html
import logging

from src.config.settings import TELEGRAM_OUTCOME_ALERTS
from src.services.telegram import send_alert

logger = logging.getLogger(__name__)

RESULT_LABELS = {"PROFIT": "target hit", "LOSS": "stopped out", "EXPIRED": "expired"}
CANDLE_HOURS = 2


def _price(value: float) -> str:
    return f"{value:,.2f}" if value >= 1000 else f"{value:.6g}"


def format_signal_alert(sig) -> str:
    """One paragraph per new signal; `sig` is a MacdSignal."""
    age = ""
    if sig.bars_ago:
        age = f" (crossed {sig.bars_ago * CANDLE_HOURS}h before the latest close)"
    return (
        f"<b>{sig.side}</b> #${html.escape(sig.symbol)} (2h){age}\n"
        f"Entry: {_price(sig.entry_price)}\n"
        f"Stop loss: {_price(sig.stop_loss)}\n"
        f"TP1: {_price(sig.tp1)} (R:R 1:{sig.risk_reward_ratio:.1f})\n"
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
        lines.append(
            f"<b>{html.escape(t['symbol'])} {side}</b> {label}\n"
            f"Entry {_price(t['entry_price'])} → exit {_price(t['exit_price'])}\n"
            f"{r_text}{t['pnl_percent']:+.2f}% after fees"
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
