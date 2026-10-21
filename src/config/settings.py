import math
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
POSTGRES_DB = os.getenv("POSTGRES_DB", "simple-trade")
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

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Candle length per Binance interval; 1w/1M are left out since they do not align to the epoch
INTERVAL_MS = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 3_600_000,
    "2h": 2 * 3_600_000,
    "4h": 4 * 3_600_000,
    "6h": 6 * 3_600_000,
    "8h": 8 * 3_600_000,
    "12h": 12 * 3_600_000,
    "1d": 24 * 3_600_000,
}
TIMEFRAME = os.getenv("TIMEFRAME", "1h").strip()
if TIMEFRAME not in INTERVAL_MS:
    raise ValueError(f"TIMEFRAME={TIMEFRAME!r} is not supported, use one of: {', '.join(INTERVAL_MS)}")
TIMEFRAME_MS = INTERVAL_MS[TIMEFRAME]

# RSI strategy: limit entry at the signal candle's low (LONG) or high (SHORT)
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "28"))
RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "72"))
TP_PERCENT = float(os.getenv("TP_PERCENT", "2"))
SL_PERCENT = float(os.getenv("SL_PERCENT", "1"))
# Windows are set in hours so they mean the same time on every TIMEFRAME, then counted in candles
FILL_WINDOW_HOURS = float(os.getenv("FILL_WINDOW_HOURS", "12"))
MAX_HOLD_HOURS = float(os.getenv("MAX_HOLD_HOURS", "24"))
FILL_WINDOW_CANDLES = max(1, math.ceil(FILL_WINDOW_HOURS * 3_600_000 / TIMEFRAME_MS))
MAX_HOLD_CANDLES = max(1, math.ceil(MAX_HOLD_HOURS * 3_600_000 / TIMEFRAME_MS))
# Exchange fees for opening and closing a position, in percent of notional
ROUND_TRIP_FEE_PERCENT = float(os.getenv("ROUND_TRIP_FEE_PERCENT", "0.1"))

# Scanning & Concurrency
# Candles fetched per scan; Wilder RSI needs a long history to match charting platforms
SCAN_CANDLES = int(os.getenv("SCAN_CANDLES", "500"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "50"))
SCAN_CONCURRENCY = int(os.getenv("SCAN_CONCURRENCY", "10"))

# Cron Scheduler Parameters
CRON_ENABLED = os.getenv("CRON_ENABLED", "true").lower() in ("true", "1", "yes")
CRON_SAFETY_DELAY_SECONDS = int(os.getenv("CRON_SAFETY_DELAY_SECONDS", "15"))
# Closed candles the scheduled scan re-checks, so a missed run (restart, deploy) is caught up
CRON_LOOKBACK_CANDLES = int(os.getenv("CRON_LOOKBACK_CANDLES", "3"))

# Telegram message when a tracked trade closes
TELEGRAM_OUTCOME_ALERTS = os.getenv("TELEGRAM_OUTCOME_ALERTS", "true").lower() in ("true", "1", "yes")
