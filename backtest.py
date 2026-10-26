"""Gambar sinyal strategi produksi (Supertrend + EMA + RSI) di candle terbaru Binance Futures.

Jalankan dari root repo: python backtest.py [SYMBOL] [INTERVAL] [LIMIT], misalnya
python backtest.py ETHUSDT 30m 500. Aturan dan indikator diimpor dari src.strategy.signals,
jadi chart ini selalu sama dengan yang dipakai scanner.
"""

import sys

import pandas as pd
import plotly.graph_objects as go
import requests
from plotly.subplots import make_subplots

from src.config.settings import (
    EMA_PERIOD,
    RSI_LONG_MAX,
    RSI_MIDLINE,
    RSI_PERIOD,
    RSI_SHORT_MIN,
    SUPERTREND_ATR_PERIOD,
    SUPERTREND_FACTOR,
)
from src.strategy.signals import compute_ema, compute_rsi, compute_supertrend, detect_signals


# -------------------------------------------------------------------------
# 1. PENGAMBILAN DATA DARI BINANCE FUTURES (FAPI)
# -------------------------------------------------------------------------
def fetch_binance_futures_klines(symbol="BTCUSDT", interval="30m", limit=500):
    url = "https://fapi.binance.com/fapi/v1/klines"
    response = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=20)
    response.raise_for_status()

    cols = [
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base_volume", "taker_buy_quote_volume", "ignore",
    ]  # fmt: skip
    df = pd.DataFrame(response.json(), columns=cols)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    # Candle terakhir masih terbentuk; scanner hanya memakai candle yang sudah close
    return df[["timestamp", "open", "high", "low", "close", "volume"]].iloc[:-1].reset_index(drop=True)


# -------------------------------------------------------------------------
# 2. INDIKATOR DAN SINYAL (SAMA DENGAN PRODUKSI)
# -------------------------------------------------------------------------
def apply_signals(df, symbol="BTCUSDT"):
    df["rsi"] = compute_rsi(df["close"])
    df["ema"] = compute_ema(df["close"])
    df["supertrend"], df["uptrend"] = compute_supertrend(df)
    df["signal"] = None
    for sig in detect_signals(df, symbol, lookback=len(df)):
        df.loc[len(df) - 1 - sig.bars_ago, "signal"] = sig.side
    return df


def rejection_summary(df):
    """Flip Supertrend mentah dan alasan yang menggugurkannya, untuk membandingkan filter."""
    prev = df["uptrend"].shift(1)
    flip_up = (prev == 0) & (df["uptrend"] == 1)
    flip_down = (prev == 1) & (df["uptrend"] == 0)
    rsi = df["rsi"]
    return {
        "flip_up": int(flip_up.sum()),
        "flip_down": int(flip_down.sum()),
        "long": int((df["signal"] == "LONG").sum()),
        "short": int((df["signal"] == "SHORT").sum()),
        "rejected_ema": int((flip_up & ~(df["close"] > df["ema"])).sum() + (flip_down & ~(df["close"] < df["ema"])).sum()),
        "rejected_rsi": int(
            (flip_up & ~((rsi > RSI_MIDLINE) & (rsi < RSI_LONG_MAX))).sum()
            + (flip_down & ~((rsi > RSI_SHORT_MIN) & (rsi < RSI_MIDLINE))).sum()
        ),
    }


# -------------------------------------------------------------------------
# 3. EXPORT CHART & INDIKATOR KE HTML
# -------------------------------------------------------------------------
def export_interactive_html(df, symbol, interval, output_path):
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.72, 0.28],
        subplot_titles=(
            f"Binance Futures: {symbol} {interval}, Supertrend ({SUPERTREND_ATR_PERIOD}, {SUPERTREND_FACTOR:g}) + EMA {EMA_PERIOD}",
            f"RSI ({RSI_PERIOD})",
        ),
    )  # fmt: skip

    fig.add_trace(
        go.Candlestick(x=df["timestamp"], open=df["open"], high=df["high"], low=df["low"], close=df["close"], name="Price"),
        row=1, col=1,
    )  # fmt: skip
    fig.add_trace(
        go.Scatter(x=df["timestamp"], y=df["ema"], line=dict(color="#FFB300", width=1.2), name=f"EMA {EMA_PERIOD}"),
        row=1, col=1,
    )  # fmt: skip

    # Garis Supertrend: hijau saat uptrend, merah saat downtrend
    for value, color, name in ((1.0, "#00E676", "Supertrend up"), (0.0, "#FF1744", "Supertrend down")):
        fig.add_trace(
            go.Scatter(
                x=df["timestamp"],
                y=df["supertrend"].where(df["uptrend"] == value),
                line=dict(color=color, width=1.2),
                name=name,
            ),
            row=1, col=1,
        )  # fmt: skip

    longs = df[df["signal"] == "LONG"]
    fig.add_trace(
        go.Scatter(
            x=longs["timestamp"],
            y=longs["low"] * 0.993,
            mode="markers+text",
            marker=dict(symbol="triangle-up", size=13, color="#00E676"),
            text=["LONG"] * len(longs),
            textposition="bottom center",
            name="Long Entry",
        ),
        row=1, col=1,
    )  # fmt: skip

    shorts = df[df["signal"] == "SHORT"]
    fig.add_trace(
        go.Scatter(
            x=shorts["timestamp"],
            y=shorts["high"] * 1.007,
            mode="markers+text",
            marker=dict(symbol="triangle-down", size=13, color="#FF1744"),
            text=["SHORT"] * len(shorts),
            textposition="top center",
            name="Short Entry",
        ),
        row=1, col=1,
    )  # fmt: skip

    fig.add_trace(
        go.Scatter(x=df["timestamp"], y=df["rsi"], line=dict(color="#29B6F6", width=1.5), name=f"RSI {RSI_PERIOD}"),
        row=2, col=1,
    )  # fmt: skip
    for level in (RSI_SHORT_MIN, RSI_MIDLINE, RSI_LONG_MAX):
        fig.add_hline(y=level, line_dash="dash", line_color="#9E9E9E", row=2, col=1, annotation_text=f"{level:g}")

    fig.update_layout(template="plotly_dark", xaxis_rangeslider_visible=False, height=850, margin=dict(l=45, r=45, t=55, b=45))
    fig.update_yaxes(title_text="Price (USDT)", row=1, col=1)
    fig.update_yaxes(title_text="RSI", range=[0, 100], row=2, col=1)

    fig.write_html(output_path)
    print(f"Laporan HTML interaktif berhasil dibuat: {output_path}")


# -------------------------------------------------------------------------
# EKSEKUSI
# -------------------------------------------------------------------------
if __name__ == "__main__":
    pair = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    tf = sys.argv[2] if len(sys.argv) > 2 else "30m"
    limit_bars = int(sys.argv[3]) if len(sys.argv) > 3 else 500

    print(f"Mengambil data {pair} ({tf}) dari fapi.binance.com...")
    df_data = fetch_binance_futures_klines(symbol=pair, interval=tf, limit=limit_bars)

    print("Memproses sinyal Supertrend + EMA + RSI...")
    df_result = apply_signals(df_data, symbol=pair)

    s = rejection_summary(df_result)
    print(f"Flip Supertrend -> naik: {s['flip_up']}, turun: {s['flip_down']}")
    print(f"Sinyal lolos filter -> Long: {s['long']}, Short: {s['short']}")
    print(f"Ditolak -> EMA: {s['rejected_ema']}, RSI: {s['rejected_rsi']} (satu flip bisa gagal di keduanya)")

    export_interactive_html(df_result, pair, tf, output_path=f"backtest_{pair}_{tf}.html")
