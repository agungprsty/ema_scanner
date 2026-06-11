import os
from dotenv import load_dotenv

load_dotenv()

# Binance
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
BINANCE_PRIVATE_KEY_PATH = os.getenv("BINANCE_PRIVATE_KEY_PATH")
BINANCE_PRIVATE_KEY = os.getenv("BINANCE_PRIVATE_KEY")
BINANCE_PRIVATE_KEY_PASSPHRASE = os.getenv("BINANCE_PRIVATE_KEY_PASSPHRASE", "")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Firebase
FIREBASE_CRED_PATH = os.getenv("FIREBASE_CRED_PATH") or ""
FIREBASE_CRED_JSON = os.getenv("FIREBASE_CRED_JSON")

# Mode
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")

# Trading Parameters
RISK_PER_TRADE_PERCENT = float(os.getenv("RISK_PER_TRADE_PERCENT", "0.02"))
LEVERAGE = int(os.getenv("LEVERAGE", "10"))
TOTAL_SIGNALS = int(os.getenv("TOTAL_SIGNALS", "3"))
VOLUME_THRESHOLD_USD = int(os.getenv("VOLUME_THRESHOLD_USD", "50000000"))
SCORE_THRESHOLD = int(os.getenv("SCORE_THRESHOLD", "80"))
ADX_MIN = int(os.getenv("ADX_MIN", "15"))
VOLUME_RATIO_MIN = float(os.getenv("VOLUME_RATIO_MIN", "1.5"))
TREND_GAP_MIN_PCT = float(os.getenv("TREND_GAP_MIN_PCT", "1.5"))
SIGNAL_COOLDOWN_CANDLES = int(os.getenv("SIGNAL_COOLDOWN_CANDLES", "16"))
MAX_TOTAL_RISK_PCT = float(os.getenv("MAX_TOTAL_RISK_PCT", "0.05"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "5.0"))
MAX_HOLDING_CANDLES = int(os.getenv("MAX_HOLDING_CANDLES", "16"))
BTC_STRENGTH_MIN = float(os.getenv("BTC_STRENGTH_MIN", "20"))

# V7.0 Day Trader — MTF Stack Parameters
ENTRY_TIMEFRAME = os.getenv("ENTRY_TIMEFRAME", "15m")
MIDDLE_TIMEFRAME = os.getenv("MIDDLE_TIMEFRAME", "1h")
MACRO_TIMEFRAME = os.getenv("MACRO_TIMEFRAME", "4h")

PULLBACK_EMA_LENGTH = int(os.getenv("PULLBACK_EMA_LENGTH", "20"))
MIDDLE_EMA_LENGTH = int(os.getenv("MIDDLE_EMA_LENGTH", "20"))
MACRO_EMA_SHORT = int(os.getenv("MACRO_EMA_SHORT", "50"))
MACRO_EMA_LONG = int(os.getenv("MACRO_EMA_LONG", "200"))

ATR_SL_MULTIPLIER = float(os.getenv("ATR_SL_MULTIPLIER", "1.5"))
ATR_TP_MULTIPLIER = float(os.getenv("ATR_TP_MULTIPLIER", "4.0"))
TP1_RISK_MULTIPLIER = float(os.getenv("TP1_RISK_MULTIPLIER", "1.5"))
TP1_EXIT_PCT = float(os.getenv("TP1_EXIT_PCT", "0.5"))
RSI_LONG_MAX = float(os.getenv("RSI_LONG_MAX", "55"))
RSI_LONG_MIN = float(os.getenv("RSI_LONG_MIN", "35"))
RSI_SHORT_MIN = float(os.getenv("RSI_SHORT_MIN", "48"))
RSI_SHORT_MAX = float(os.getenv("RSI_SHORT_MAX", "65"))
PULLBACK_DISTANCE_PCT = float(os.getenv("PULLBACK_DISTANCE_PCT", "5.0"))
VOLUME_RATIO_MAX = float(os.getenv("VOLUME_RATIO_MAX", "2.0"))

# Golden Cross Strategy Parameters (1H Entry / 4H Macro)
GC_ATR_SL_MULTIPLIER = float(os.getenv("GC_ATR_SL_MULTIPLIER", "1.5"))
GC_LOOKBACK_CANDLES = int(os.getenv("GC_LOOKBACK_CANDLES", "10"))
GC_ENTRY_FEE_PCT = float(os.getenv("GC_ENTRY_FEE_PCT", "0.05"))

# Backtest
BACKTEST_INITIAL_BALANCE = float(os.getenv("BACKTEST_INITIAL_BALANCE", "100"))
