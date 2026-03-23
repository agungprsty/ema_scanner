import httpx
from app.core.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

async def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, data=payload)
            return response.json()
    except Exception as e:
        print(f"❌ Gagal mengirim Telegram: {e}")