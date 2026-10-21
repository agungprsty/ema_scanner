import requests
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# -------------------------------------------------------------------------
# 1. PENGAMBILAN DATA DARI BINANCE FUTURES (FAPI)
# -------------------------------------------------------------------------
def fetch_binance_futures_klines(symbol="BTCUSDT", interval="1h", limit=500):
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base_volume", "taker_buy_quote_volume", "ignore"
    ]
    df = pd.DataFrame(data, columns=cols)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
        
    return df[["open_time", "open", "high", "low", "close", "volume"]]

# -------------------------------------------------------------------------
# 2. KALKULASI RSI (14) - METODE WILDER
# -------------------------------------------------------------------------
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    
    # Wilder's Smoothing (alpha = 1 / period)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# -------------------------------------------------------------------------
# 3. IDENTIFIKASI SINYAL ENTRY LANGSUNG
# -------------------------------------------------------------------------
def apply_signals(df):
    df["rsi"] = calculate_rsi(df["close"], period=14)
    df["signal"] = None
    
    rsi = df["rsi"]
    prev_rsi = df["rsi"].shift(1)
    
    # Long: candle sebelumnya masih di atas 28.0, lalu candle ini turun menyentuh/menembus <= 28.0
    long_cond = (prev_rsi > 28.0) & (rsi <= 28.0)
    
    # Short: candle sebelumnya masih di bawah 72.0, lalu candle ini naik menyentuh/menembus >= 72.0
    short_cond = (prev_rsi < 72.0) & (rsi >= 72.0)
    
    df.loc[long_cond, "signal"] = "LONG"
    df.loc[short_cond, "signal"] = "SHORT"
    
    return df

# -------------------------------------------------------------------------
# 4. EXPORT CHART & INDIKATOR KE HTML
# -------------------------------------------------------------------------
def export_interactive_html(df, symbol="BTCUSDT", output_path="crypto_rsi_strategy.html"):
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.72, 0.28],
        subplot_titles=(f"Binance Futures: {symbol} Price Action & Signals", "RSI (14)")
    )

    # Candlestick
    fig.add_trace(
        go.Candlestick(
            x=df["open_time"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="Price"
        ),
        row=1, col=1
    )

    # Marker Sinyal Long
    longs = df[df["signal"] == "LONG"]
    fig.add_trace(
        go.Scatter(
            x=longs["open_time"],
            y=longs["low"] * 0.993,
            mode="markers+text",
            marker=dict(symbol="triangle-up", size=13, color="#00E676"),
            text=["LONG" for _ in range(len(longs))],
            textposition="bottom center",
            name="Long Entry"
        ),
        row=1, col=1
    )

    # Marker Sinyal Short
    shorts = df[df["signal"] == "SHORT"]
    fig.add_trace(
        go.Scatter(
            x=shorts["open_time"],
            y=shorts["high"] * 1.007,
            mode="markers+text",
            marker=dict(symbol="triangle-down", size=13, color="#FF1744"),
            text=["SHORT" for _ in range(len(shorts))],
            textposition="top center",
            name="Short Entry"
        ),
        row=1, col=1
    )

    # Garis RSI
    fig.add_trace(
        go.Scatter(
            x=df["open_time"],
            y=df["rsi"],
            line=dict(color="#29B6F6", width=1.5),
            name="RSI 14"
        ),
        row=2, col=1
    )

    # Level Acuan 75 & 25.5
    fig.add_hline(y=72.0, line_dash="dash", line_color="#FF5252", row=2, col=1, annotation_text="Short Threshold (72)")
    fig.add_hline(y=28.0, line_dash="dash", line_color="#69F0AE", row=2, col=1, annotation_text="Long Threshold (28)")

    # Styling
    fig.update_layout(
        template="plotly_dark",
        xaxis_rangeslider_visible=False,
        height=850,
        margin=dict(l=45, r=45, t=55, b=45)
    )
    
    fig.update_yaxes(title_text="Price (USDT)", row=1, col=1)
    fig.update_yaxes(title_text="RSI", range=[0, 100], row=2, col=1)

    fig.write_html(output_path)
    print(f"Laporan HTML interaktif berhasil dibuat: {output_path}")

# -------------------------------------------------------------------------
# EKSEKUSI
# -------------------------------------------------------------------------
if __name__ == "__main__":
    pair = "BTCUSDT"
    tf = "1h"
    limit_bars = 500

    print(f"Mengambil data {pair} ({tf}) dari fapi.binance.com...")
    df_data = fetch_binance_futures_klines(symbol=pair, interval=tf, limit=limit_bars)
    
    print("Memproses sinyal RSI...")
    df_result = apply_signals(df_data)
    
    total_long = (df_result["signal"] == "LONG").sum()
    total_short = (df_result["signal"] == "SHORT").sum()
    print(f"Sinyal terdeteksi -> Long: {total_long}, Short: {total_short}")

    export_interactive_html(df_result, symbol=pair, output_path="crypto_rsi_strategy.html")