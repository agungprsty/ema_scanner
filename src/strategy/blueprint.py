import logging

from dataclasses import dataclass
import pandas as pd

logger = logging.getLogger(__name__)

_LOG_NEAR_MISS = True


@dataclass
class Signal:
    symbol: str
    side: str
    current_price: float
    pullback_price: float
    atr: float


def _near_miss_log(
    symbol: str,
    side: str,
    adx: float,
    gc: bool,
    dc: bool,
    vol_ok: bool,
    rsi: float,
    rsi_ok: bool,
    bias: str,
    missing: list[str],
):
    logger.info(
        "NEAR MISS %-5s %s — adx=%.1f gc=%s dc=%s vol=%s "
        "rsi=%.1f(%s) bias=%s | missing: %s",
        side, symbol, adx,
        "✓" if gc else "✗", "✓" if dc else "✗",
        "✓" if vol_ok else "✗",
        rsi, "✓" if rsi_ok else "✗", bias,
        ", ".join(missing),
    )


def evaluate(
    df: pd.DataFrame,
    btc_bias: str,
    symbol: str,
    df_htf: pd.DataFrame | None = None,
) -> Signal | None:
    if df is None or len(df) < 12:
        return None

    curr = df.iloc[-1]

    adx_curr = curr["ADX_14"]
    if adx_curr <= 25:
        return None

    ema15_curr = curr["EMA15"]
    ema100_curr = curr["EMA100"]
    vol_curr = curr["volume"]
    sma_vol20 = curr["SMA_VOL20"]
    rsi = curr["RSI"]
    
    macd_hist = curr.get("MACDh_12_26_9", 0)

    # Check for recent golden cross (within last 5 candles) and ensure EMA15 > EMA100 now
    recent_golden_cross = (ema15_curr > ema100_curr) and any(
        (df["EMA15"].iloc[-i-1] <= df["EMA100"].iloc[-i-1]) and (df["EMA15"].iloc[-i] > df["EMA100"].iloc[-i])
        for i in range(1, min(6, len(df)))
    )

    # Check for recent death cross (within last 5 candles) and ensure EMA15 < EMA100 now
    recent_death_cross = (ema15_curr < ema100_curr) and any(
        (df["EMA15"].iloc[-i-1] >= df["EMA100"].iloc[-i-1]) and (df["EMA15"].iloc[-i] < df["EMA100"].iloc[-i])
        for i in range(1, min(6, len(df)))
    )

    volume_surge = vol_curr > sma_vol20

    # HTF directional filter
    htf_bullish = True
    htf_bearish = True
    if df_htf is not None and len(df_htf) >= 2:
        htf_close = df_htf["close"].iloc[-1]
        htf_ema100 = df_htf["EMA100"].iloc[-1] if "EMA100" in df_htf.columns else None
        if htf_ema100 is not None and not pd.isna(htf_ema100):
            htf_bullish = htf_close > htf_ema100
            htf_bearish = htf_close < htf_ema100

    # --- LONG signal ---
    if recent_golden_cross and volume_surge and rsi < 65 and btc_bias == "BULLISH" and htf_bullish and macd_hist > 0:
        return Signal(
            symbol=symbol,
            side="LONG",
            current_price=curr["close"],
            pullback_price=curr["EMA15"],
            atr=curr["ATR"],
        )

    # --- SHORT signal ---
    if recent_death_cross and volume_surge and rsi > 35 and btc_bias == "BEARISH" and htf_bearish and macd_hist < 0:
        return Signal(
            symbol=symbol,
            side="SHORT",
            current_price=curr["close"],
            pullback_price=curr["EMA15"],
            atr=curr["ATR"],
        )

    if not _LOG_NEAR_MISS:
        return None

    # LONG near-miss
    if btc_bias == "BULLISH" and recent_golden_cross:
        missing = []
        if not htf_bullish:
            missing.append("htf_bearish")
        if not volume_surge:
            missing.append("vol_surge")
        if not (rsi < 65):
            missing.append(f"rsi({rsi:.1f}>=65)")
        if missing:
            _near_miss_log(symbol, "LONG", adx_curr, recent_golden_cross, recent_death_cross,
                           volume_surge, rsi, rsi < 65, btc_bias, missing)
        return None

    # SHORT near-miss
    if btc_bias == "BEARISH" and recent_death_cross:
        missing = []
        if not htf_bearish:
            missing.append("htf_bullish")
        if not volume_surge:
            missing.append("vol_surge")
        if not (rsi > 35):
            missing.append(f"rsi({rsi:.1f}<=35)")
        if missing:
            _near_miss_log(symbol, "SHORT", adx_curr, recent_golden_cross, recent_death_cross,
                           volume_surge, rsi, rsi > 35, btc_bias, missing)
        return None

    ema15_pct_from_100 = abs(ema15_curr - ema100_curr) / ema100_curr * 100
    if ema15_pct_from_100 < 0.5:
        direction = "gc-pending" if ema15_curr > ema100_curr else "dc-pending"
        logger.info(
            "NEAR MISS %-5s %s — ema15/100 gap=%.3f%% adx_curr=%.1f vol=%s rsi=%.1f",
            direction, symbol, ema15_pct_from_100,
            adx_curr, "✓" if volume_surge else "✗", rsi,
        )

    return None
