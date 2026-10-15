import hashlib
import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Server & Security
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "7860"))
# Still parsed so old .env files load, but nothing reads it: there is no order code, only signals
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
APP_MODE = "SIGNALS_ONLY"
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "").strip()
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
# /docs, /redoc and /openapi.json stay off in production unless asked for
ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "false").lower() in ("true", "1", "yes")
# Dashboard sign-in: an HttpOnly cookie holding an HMAC-signed, expiring token derived from API_SECRET_KEY.
# Keep COOKIE_SECURE on behind HTTPS; turn it off only for local plain-http development
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() in ("true", "1", "yes")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(12 * 3600)))

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

# Binance. Every call the app makes is public market data, so credentials are only loaded into the
# web process when BINANCE_AUTH_ENABLED is set (nothing uses them yet)
BINANCE_AUTH_ENABLED = os.getenv("BINANCE_AUTH_ENABLED", "false").lower() in ("true", "1", "yes")

def _binance_secret(name: str, default: str | None = "") -> str | None:
    return os.getenv(name, default) if BINANCE_AUTH_ENABLED else default


BINANCE_API_KEY = _binance_secret("BINANCE_API_KEY")
BINANCE_API_SECRET = _binance_secret("BINANCE_API_SECRET")
BINANCE_PRIVATE_KEY_PATH = _binance_secret("BINANCE_PRIVATE_KEY_PATH", None)
BINANCE_PRIVATE_KEY = _binance_secret("BINANCE_PRIVATE_KEY", None)
BINANCE_PRIVATE_KEY_PASSPHRASE = _binance_secret("BINANCE_PRIVATE_KEY_PASSPHRASE")

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
# Candles fetched per scan; EMA-based MACD needs a long history to match charting platforms
MACD_CANDLES = int(os.getenv("MACD_CANDLES", "500"))

# Strategy Risk Parameters
RISK_REWARD_RATIO = float(os.getenv("RISK_REWARD_RATIO", "1.5"))
SWING_LOOKBACK_CANDLES = int(os.getenv("SWING_LOOKBACK_CANDLES", "5"))
# Close a signal as EXPIRED when neither target nor stop is hit within this many candles
SIGNAL_EXPIRY_CANDLES = int(os.getenv("SIGNAL_EXPIRY_CANDLES", "48"))
# Exchange fees for opening and closing a position, in percent of notional
ROUND_TRIP_FEE_PERCENT = float(os.getenv("ROUND_TRIP_FEE_PERCENT", "0.1"))

# Strategy provenance stamped on every stored signal; bump the version whenever the rules change
STRATEGY_KEY = os.getenv("STRATEGY_KEY", "macd_cross")
STRATEGY_VERSION = os.getenv("STRATEGY_VERSION", "2.2.0")

# Cost model applied to every booked trade: fees plus adverse slippage on market fills.
# "cm1" names exactly these defaults: round-trip fee 0.1%, one-way slippage per symbol from the
# institutional review's P0 table (BTC/ETH 2, SOL 8, XRP 10, ZEC 20, NEAR 25 bps, 10 bps elsewhere)
# and stops at 1.5x. Any override in the environment is published under a derived label instead
CM1_FEE_PERCENT = 0.1
CM1_SLIPPAGE_BPS_DEFAULT = 10.0
CM1_SLIPPAGE_BPS_MAP = {
    "BTCUSDT": 2.0,
    "ETHUSDT": 2.0,
    "SOLUSDT": 8.0,
    "XRPUSDT": 10.0,
    "ZECUSDT": 20.0,
    "NEARUSDT": 25.0,
}
CM1_STOP_SLIPPAGE_MULTIPLIER = 1.5

SLIPPAGE_BPS_DEFAULT = float(os.getenv("SLIPPAGE_BPS_DEFAULT", str(CM1_SLIPPAGE_BPS_DEFAULT)))
# Stops fill worse than entries, since they trigger into the move
STOP_SLIPPAGE_MULTIPLIER = float(os.getenv("STOP_SLIPPAGE_MULTIPLIER", str(CM1_STOP_SLIPPAGE_MULTIPLIER)))


def _load_slippage_map() -> dict[str, float]:
    raw = os.getenv("SLIPPAGE_BPS_MAP", "").strip()
    if not raw:
        return dict(CM1_SLIPPAGE_BPS_MAP)
    try:
        return {str(sym).upper(): float(bps) for sym, bps in json.loads(raw).items()}
    except (ValueError, TypeError, AttributeError):
        logger.warning("SLIPPAGE_BPS_MAP is not a JSON object of symbol to bps; using the default for every symbol")
        return {}


SLIPPAGE_BPS_BY_SYMBOL = _load_slippage_map()


def cost_model_version(
    fee_percent: float, slippage_default: float, slippage_map: dict[str, float], stop_multiplier: float
) -> str:
    """"cm1" for the cm1 parameters, otherwise "cmx-" plus a short hash of the parameters in force, so a
    restated net R always says which costs produced it (fits the 10-character column)."""
    params = {
        "fee_percent": float(fee_percent),
        "slippage_bps_default": float(slippage_default),
        "slippage_bps_map": {sym: float(bps) for sym, bps in sorted(slippage_map.items())},
        "stop_slippage_multiplier": float(stop_multiplier),
    }
    cm1 = {
        "fee_percent": CM1_FEE_PERCENT,
        "slippage_bps_default": CM1_SLIPPAGE_BPS_DEFAULT,
        "slippage_bps_map": dict(sorted(CM1_SLIPPAGE_BPS_MAP.items())),
        "stop_slippage_multiplier": CM1_STOP_SLIPPAGE_MULTIPLIER,
    }
    if params == cm1:
        return "cm1"
    digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    return f"cmx-{digest[:6]}"


COST_MODEL_VERSION = cost_model_version(
    ROUND_TRIP_FEE_PERCENT, SLIPPAGE_BPS_DEFAULT, SLIPPAGE_BPS_BY_SYMBOL, STOP_SLIPPAGE_MULTIPLIER
)
if COST_MODEL_VERSION != "cm1":
    logger.warning("Cost parameters differ from cm1; trades are booked under cost model %s", COST_MODEL_VERSION)


def slippage_bps_for(symbol: str) -> float:
    """Modelled one-way slippage for a market fill on `symbol`, in basis points."""
    return SLIPPAGE_BPS_BY_SYMBOL.get(symbol.upper(), SLIPPAGE_BPS_DEFAULT)

# Filtering & Concurrency
VOLUME_THRESHOLD_USD = float(os.getenv("VOLUME_THRESHOLD_USD", "50000000"))
SCAN_CONCURRENCY = int(os.getenv("SCAN_CONCURRENCY", "10"))

# Cron Scheduler Parameters
CRON_ENABLED = os.getenv("CRON_ENABLED", "true").lower() in ("true", "1", "yes")
CRON_SAFETY_DELAY_SECONDS = int(os.getenv("CRON_SAFETY_DELAY_SECONDS", "15"))
# Closed candles the scheduled scan re-checks, so a missed run (restart, deploy) is caught up
CRON_LOOKBACK_CANDLES = int(os.getenv("CRON_LOOKBACK_CANDLES", "3"))
# A scheduled signal detected later than this after its entry candle opened is stored as a LATE
# catch-up and kept out of the track record, since the price it was booked at had already passed
LATE_SIGNAL_GRACE_SECONDS = int(os.getenv("LATE_SIGNAL_GRACE_SECONDS", "600"))
# /api/health reports "degraded" once the last successful scheduled run is older than this (one 2H slot + 5 min)
CRON_STALE_SECONDS = int(os.getenv("CRON_STALE_SECONDS", str(2 * 3600 + 5 * 60)))
# Optional dead-man switch (e.g. Healthchecks.io): GET after every successful scheduled run
HEALTHCHECK_PING_URL = os.getenv("HEALTHCHECK_PING_URL", "").strip()
# Set in production: /api/health then reports "degraded" when CRON_ENABLED is off, so a scheduler
# switched off by mistake does not read as healthy while the ledger stops
REQUIRE_CRON = os.getenv("REQUIRE_CRON", "false").lower() in ("true", "1", "yes")
# Each /api/health check (database, Redis, cron freshness) gets this long, so the endpoint always
# answers inside the Docker HEALTHCHECK timeout (5s) and a hung database reads as down
HEALTH_PROBE_TIMEOUT_SECONDS = float(os.getenv("HEALTH_PROBE_TIMEOUT_SECONDS", "2"))

# Telegram message when a tracked trade closes
TELEGRAM_OUTCOME_ALERTS = os.getenv("TELEGRAM_OUTCOME_ALERTS", "true").lower() in ("true", "1", "yes")
