import os
from dotenv import load_dotenv

load_dotenv()

# Binance
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
BINANCE_PRIVATE_KEY_PATH = os.getenv("BINANCE_PRIVATE_KEY_PATH")
BINANCE_PRIVATE_KEY_PASSPHRASE = os.getenv("BINANCE_PRIVATE_KEY_PASSPHRASE", "")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Firebase
FIREBASE_CRED_PATH = os.getenv("FIREBASE_CRED_PATH", "firebase-credentials.json")

# Mode
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")

# Trading Parameters
RISK_PER_TRADE_PERCENT = float(os.getenv("RISK_PER_TRADE_PERCENT", "0.01"))
LEVERAGE = int(os.getenv("LEVERAGE", "10"))
TOTAL_SIGNALS = int(os.getenv("TOTAL_SIGNALS", "5"))
VOLUME_THRESHOLD_USD = int(os.getenv("VOLUME_THRESHOLD_USD", "50000000"))

# Backtest
BACKTEST_INITIAL_BALANCE = float(os.getenv("BACKTEST_INITIAL_BALANCE", "100"))
