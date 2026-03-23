import httpx
from app.core.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

def send_alert(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        # Menggunakan synchronous request sederhana untuk scheduler
        with httpx.Client() as client:
            response = client.post(url, data=payload)
            return response.json()
    except Exception as e:
        print(f"❌ Gagal mengirim Telegram: {e}")