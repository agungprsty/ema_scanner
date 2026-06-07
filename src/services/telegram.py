import asyncio
import logging

import httpx

from src.config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


async def send_alert(message: str, parse_mode: str = "HTML") -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not configured")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data=payload)
            if resp.status_code != 200:
                logger.error("Telegram API error %s: %s", resp.status_code, resp.text)
    except Exception as e:
        logger.error("Failed to send Telegram alert: %s", e, exc_info=True)


def send_alert_async(message: str, parse_mode: str = "HTML") -> None:
    asyncio.create_task(send_alert(message, parse_mode))
