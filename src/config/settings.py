import os

from dotenv import load_dotenv

load_dotenv()

# Server & Security
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "7860"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "").strip()
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]

# Database (PostgreSQL)
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
POSTGRES_DB = os.getenv("POSTGRES_DB", "macd_scanner")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))

_raw_db_url = os.getenv("DATABASE_URL")
if _raw_db_url:
    if _raw_db_url.startswith("postgres://"):
        DATABASE_URL = _raw_db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif _raw_db_url.startswith("postgresql://") and "+asyncpg" not in _raw_db_url:
        DATABASE_URL = _raw_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    else:
        DATABASE_URL = _raw_db_url
else:
    DATABASE_URL = f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"

# Redis
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Binance
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_PRIVATE_KEY_PATH = os.getenv("BINANCE_PRIVATE_KEY_PATH")
BINANCE_PRIVATE_KEY = os.getenv("BINANCE_PRIVATE_KEY")
BINANCE_PRIVATE_KEY_PASSPHRASE = os.getenv("BINANCE_PRIVATE_KEY_PASSPHRASE", "")
BINANCE_PROXY = os.getenv("BINANCE_PROXY", "")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# MACD Parameters — Strict 2h timeframe
MACD_FAST = int(os.getenv("MACD_FAST", "12"))
MACD_SLOW = int(os.getenv("MACD_SLOW", "26"))
MACD_SIGNAL = int(os.getenv("MACD_SIGNAL", "9"))
MACD_TIMEFRAME = "2h"
MACD_CROSS_LOOKBACK_CANDLES = int(os.getenv("MACD_CROSS_LOOKBACK_CANDLES", "5"))
MACD_MAX_SYMBOLS = int(os.getenv("MACD_MAX_SYMBOLS", "50"))
MACD_INCLUDE_CURRENT = os.getenv("MACD_INCLUDE_CURRENT", "false").lower() in ("true", "1", "yes")

# Strategy Risk Parameters
RISK_REWARD_RATIO = float(os.getenv("RISK_REWARD_RATIO", "1.5"))
SWING_LOOKBACK_CANDLES = int(os.getenv("SWING_LOOKBACK_CANDLES", "5"))

# Filtering & Concurrency
VOLUME_THRESHOLD_USD = float(os.getenv("VOLUME_THRESHOLD_USD", "50000000"))
SCAN_CONCURRENCY = int(os.getenv("SCAN_CONCURRENCY", "10"))

# Cron Scheduler Parameters
CRON_ENABLED = os.getenv("CRON_ENABLED", "true").lower() in ("true", "1", "yes")
CRON_SAFETY_DELAY_SECONDS = int(os.getenv("CRON_SAFETY_DELAY_SECONDS", "15"))
