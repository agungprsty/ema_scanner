import ccxt
import pandas as pd
from datetime import timedelta
from zoneinfo import ZoneInfo
from app.services.telegram_bot import send_alert

def scan_crossovers():
    print("🚀 Memulai pendeteksian Golden & Death Cross...")
    exchange = ccxt.binanceusdm({
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })

    try:
        markets = exchange.load_markets()
        # Filter: Hanya USDT linear futures yang aktif
        symbols = [
            symbol for symbol, market in markets.items()
            if market.get('linear') and market.get('active') and market.get('quote') == 'USDT'
        ]

        # Ambil volume untuk filter likuiditas
        tickers = exchange.fetch_tickers()
        liquid_symbols = [s for s in symbols if tickers.get(s, {}).get('quoteVolume', 0) > 10_000_000]
    except Exception as e:
        print(f"❌ Gagal memuat market: {e}")
        return

    notifikasi = []
    
    for symbol in liquid_symbols:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df = df.iloc[:-1] # Ambil candle yang sudah CLOSED

            if len(df) < 50: continue

            # Indikator EMA 7 & 25
            df['ema_fast'] = df['close'].ewm(span=7, adjust=False).mean()
            df['ema_slow'] = df['close'].ewm(span=25, adjust=False).mean()

            prev, curr = -2, -1

            # Logika Crossover
            is_golden = (df['ema_fast'].iloc[prev] <= df['ema_slow'].iloc[prev] and 
                        df['ema_fast'].iloc[curr] > df['ema_slow'].iloc[curr])
            
            is_death = (df['ema_fast'].iloc[prev] >= df['ema_slow'].iloc[prev] and 
                        df['ema_fast'].iloc[curr] < df['ema_slow'].iloc[curr])

            waktu_wib = (pd.to_datetime(df['timestamp'].iloc[curr], unit='ms') + 
                        timedelta(hours=7)).strftime('%Y-%m-%d %H:%M WIB')

            if is_golden:
                notifikasi.append(f"<b>🟢 GOLDEN CROSS</b>\nSymbol: <b>{symbol}</b>\nTime: {waktu_wib}")
            elif is_death:
                notifikasi.append(f"<b>🔴 DEATH CROSS</b>\nSymbol: <b>{symbol}</b>\nTime: {waktu_wib}")

        except: continue

    if notifikasi:
        header = "🔔 <b>EMA CROSSOVER 1H (7/25)</b>\n" + ("━"*20) + "\n\n"
        send_alert(header + "\n\n".join(notifikasi))
    else:
        print("✅ Scan selesai: Tidak ada crossover baru.")