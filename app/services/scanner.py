import logging
import pandas as pd
from datetime import timedelta

# Konfigurasi Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class TradingBot:
    def __init__(self, exchange, timeframe: str, limit: int, total_signal: int):
        self.exchange = exchange
        self.exchange_timeframe = timeframe
        self.exchange_limit = limit
        self.limit_signals = total_signal

    async def fetch_and_scan(self, symbol):
        """Fungsi tunggal untuk ambil data + analisa per koin."""
        try:
            ohlcv = await self.exchange.fetch_ohlcv(
                symbol,
                timeframe=self.exchange_timeframe,
                limit=self.exchange_limit
            )

            if not ohlcv or len(ohlcv) < 30: return None

            df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
            df = df.iloc[:-1] # Membuang candle yang sedang berjalan
            df['ema_fast'] = df['close'].ewm(span=7, adjust=False).mean()
            df['ema_slow'] = df['close'].ewm(span=25, adjust=False).mean()

            prev, curr = df.iloc[-2], df.iloc[-1]
            
            # Logika Crossover
            side = None
            if prev['ema_fast'] <= prev['ema_slow'] and curr['ema_fast'] > curr['ema_slow']:
                side = 'LONG'
            elif prev['ema_fast'] >= prev['ema_slow'] and curr['ema_fast'] < curr['ema_slow']:
                side = 'SHORT'

            if side:
                return self.create_signal_data(symbol, side, curr)
        
        except Exception as e:
            logging.error(f"Gagal memproses {symbol}: {str(e)}", exc_info=True)
            return None
        
    def create_signal_data(self, symbol, side, row):
        """
        Menggunakan strategi Asymmetric Bets:
        - Stop Loss Ketat: 1.5% (Proteksi modal)
        - TP 1: 4.5% (RR 1:3) -> Amankan profit sebagian
        - TP 2: 10.0% (RR 1:6.6) -> Menangkap tren besar (Asymmetric Upside)
        """
        # Entry price: lebih menunggu pullback (lebih dekat ke EMA25)
        entry_price = (row['ema_fast'] * 0.4) + (row['ema_slow'] * 0.6)
        
        # Parameter Risk/Reward
        risk_pct = 0.015      # 1.5% Risk
        reward_1_pct = 0.045  # 4.5% Reward (1:3)
        reward_2_pct = 0.10   # 10.0% Reward (1:6.6+)

        if side == 'LONG':
            stop_loss = entry_price * (1 - risk_pct)
            take_profit_1 = entry_price * (1 + reward_1_pct)
            take_profit_2 = entry_price * (1 + reward_2_pct)
        else:  # SHORT
            stop_loss = entry_price * (1 + risk_pct)
            take_profit_1 = entry_price * (1 - reward_1_pct)
            take_profit_2 = entry_price * (1 - reward_2_pct)
        
        waktu = (pd.to_datetime(row['ts'], unit='ms') + timedelta(hours=7)).strftime('%Y-%m-%d %H:%M WIB')
        
        return {
            "symbol": symbol.split(':')[0],
            "side": side,
            "entry": f"{entry_price:,.4f}",
            "tp1": f"{take_profit_1:,.4f}",
            "tp2": f"{take_profit_2:,.4f}",
            "sl": f"{stop_loss:,.4f}",
            "time": waktu
        }
    
    def format_combined_message(self, signals):
        """Menggabungkan banyak sinyal ke dalam satu bubble chat."""
        
        header = f"🔔 <b>EMA CROSSOVER {self.exchange_timeframe.upper()} (7/25)</b>\n"
        header += "<i>Strategi: Asymmetric Bets (RR 1:3)</i>\n"
        header += "━━━━━━━━━━━━━━━\n\n"
        
        body = ""
        for s in signals:
            icon = "🟢" if s['side'] == "LONG" else "🔴"
            body += f"{icon} {s['side']} <b>${s['symbol']}</b> NOW ⚡️\n"
            body += f"📍 Entry: {s['entry']}\n"
            body += f"💰 TP: {s['tp1']} - {s['tp2']}\n"
            body += f"⛔ SL: {s['sl']}\n"
            body += "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
            
        footer = f"\nTime: {signals[0]['time']}"
        return header + body + footer