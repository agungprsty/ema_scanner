import asyncio
import logging

import httpx

from src.config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


def _chunk_message(text: str, max_length: int = 3800) -> list[str]:
    if len(text) <= max_length:
        return [text]

    chunks = []
    paragraphs = text.split("\n\n")
    current_chunk: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para) + 2
        if current_len + para_len > max_length:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = [para]
                current_len = para_len
            else:
                for i in range(0, len(para), max_length):
                    chunks.append(para[i : i + max_length])
                current_chunk = []
                current_len = 0
        else:
            current_chunk.append(para)
            current_len += para_len

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


async def send_alert(message: str, parse_mode: str = "HTML") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = _chunk_message(message)
    all_ok = True

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for idx, chunk in enumerate(chunks):
                payload = {
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                }
                resp = await client.post(url, data=payload)
                if resp.status_code != 200:
                    logger.error("Telegram API error %s: %s", resp.status_code, resp.text)
                    all_ok = False
                if len(chunks) > 1 and idx < len(chunks) - 1:
                    await asyncio.sleep(0.3)
        return all_ok
    except Exception as e:
        logger.error("Failed to send Telegram alert: %s", e)
        return False
