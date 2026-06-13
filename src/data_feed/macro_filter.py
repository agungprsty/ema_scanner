from dataclasses import dataclass

import pandas as pd


@dataclass
class BtcBias:
    side: str  # "BULLISH" | "BEARISH" | "NEUTRAL"
    strength: float  # 0-100
    vol_regime: str  # "LOW" | "NORMAL" | "HIGH"


def _vwap(df: pd.DataFrame, period: int = 168) -> pd.Series:
    return (df["close"] * df["volume"]).rolling(period).sum() / df["volume"].rolling(period).sum()


def _compute_strength(df: pd.DataFrame, side: str) -> float:
    last = df.iloc[-1]
    close = last["close"]

    # 1. VWAP distance (0-40 pts)
    vwap_val = _vwap(df).iloc[-1]
    vwap_dist = abs(close - vwap_val) / vwap_val * 100
    vwap_score = min(40, vwap_dist * 10)

    # 2. EMA slope gradient (0-30 pts)
    ema_fast = df["close"].ewm(span=7, adjust=False).mean()
    ema_slope = (ema_fast.iloc[-1] - ema_fast.iloc[-7]) / ema_fast.iloc[-7] * 100 if len(df) >= 7 else 0
    slope_score = min(30, abs(ema_slope) * 30)

    # 3. Volume momentum (0-30 pts)
    vol_ratio = last["volume"] / df["volume"].rolling(20).mean().iloc[-1]
    vol_score = min(30, vol_ratio * 15) if side == "BULLISH" else min(30, max(0, (2 - vol_ratio) * 15))

    return min(100, vwap_score + slope_score + vol_score)


def _compute_vol_regime(df: pd.DataFrame) -> str:
    if "ATR_PERCENT" not in df.columns:
        atr_series = (df["high"] - df["low"]).rolling(14).mean()
        atr_pct = atr_series / df["close"] * 100
    else:
        atr_pct = df["ATR_PERCENT"]

    curr_vol = atr_pct.iloc[-1]
    vol_rank = (atr_pct.rank(pct=True).iloc[-1]) * 100

    if vol_rank > 80:
        return "HIGH"
    elif vol_rank < 20:
        return "LOW"
    return "NORMAL"


def compute_btc_bias(df: pd.DataFrame) -> BtcBias:
    if df is None or len(df) < 30:
        return BtcBias(side="NEUTRAL", strength=0.0, vol_regime="NORMAL")

    close = df["close"]
    vwap = _vwap(df)
    ema_fast = close.ewm(span=7, adjust=False).mean()
    ema_slow = close.ewm(span=50, adjust=False).mean()

    last = df.iloc[-1]
    ema_aligned_bullish = ema_fast.iloc[-1] > ema_slow.iloc[-1]
    ema_aligned_bearish = ema_fast.iloc[-1] < ema_slow.iloc[-1]

    # Direction from EMA alignment; VWAP distance modulates strength
    if ema_aligned_bullish:
        side = "BULLISH"
    elif ema_aligned_bearish:
        side = "BEARISH"
    else:
        return BtcBias(side="NEUTRAL", strength=0.0, vol_regime=_compute_vol_regime(df))

    strength = _compute_strength(df, side)
    vol_regime = _compute_vol_regime(df)

    return BtcBias(side=side, strength=strength, vol_regime=vol_regime)
