import logging
import pandas as pd
import pandas_ta as ta
from datetime import timedelta

# Konfigurasi Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MACDScanner:
    def __init__(self, exchange, timeframe: str, limit: int, total_signal: int):
        self.exchange = exchange
        self.exchange_timeframe = timeframe
        self.exchange_limit = limit
        self.limit_signals = total_signal

    async def fetch_and_scan(self, symbol):
        """Fungsi tunggal untuk ambil data + analisa MACD Cross per koin."""
        try:
            ohlcv = await self.exchange.fetch_ohlcv(
                symbol,
                timeframe=self.exchange_timeframe,
                limit=self.exchange_limit
            )

            if not ohlcv or len(ohlcv) < 50:  # Minimal data lebih besar karena butuh indikator
                return None

            df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
            df = df.iloc[:-1]  # Membuang candle yang sedang berjalan

            # === INDIKATOR TAMBAHAN ===
            macd = df.ta.macd(fast=12, slow=26, signal=9)
            df['EMA7'] = df.ta.ema(length=7)
            adx_df = df.ta.adx(length=14)
            df['ATR'] = df.ta.atr(length=14)

            # Gabungkan semua kolom indikator
            df = pd.concat([df, macd, adx_df], axis=1)

            # Bersihkan NaN
            df = df.dropna(subset=['MACD_12_26_9', 'MACDs_12_26_9', 'MACDh_12_26_9', 
                                    'EMA7', 'ADX_14', 'ATR']).reset_index(drop=True)

            if len(df) < 2:
                return None

            # Ambil data candle terakhir dan sebelumnya
            prev = df.iloc[-2]
            curr = df.iloc[-1]

            m_col, s_col = 'MACD_12_26_9', 'MACDs_12_26_9'
            price = curr['close']
            ema_val = curr['EMA7']
            adx_val = curr['ADX_14']

            # Logika Crossover MACD
            is_golden = (curr[m_col] > curr[s_col]) and (prev[m_col] <= prev[s_col])
            is_death = (curr[m_col] < curr[s_col]) and (prev[m_col] >= prev[s_col])

            if not (is_golden or is_death):
                return None

            # Filter Trending Market
            is_trending = adx_val > 15

            if not is_trending:
                return None  # Reject jika sideways

            side = None
            reason = ""

            if is_golden and price > ema_val:
                side = 'LONG'
                reason = "TRENDING + STRONG MOMENTUM"
            elif is_death and price < ema_val:
                side = 'SHORT'
                reason = "TRENDING + STRONG MOMENTUM"
            else:
                return None  # Reject filter EMA7

            return self.create_signal_data(symbol, side, curr, reason)

        except Exception as e:
            logging.error(f"Gagal memproses {symbol} dengan MACD: {str(e)}", exc_info=True)
            return None

    def create_signal_data(self, symbol, side, row, reason=""):
        """
        Membuat signal data dengan format yang konsisten
        """
        # Entry price: bisa disesuaikan (saat ini pakai harga close)
        entry_price = row['close']

        # Parameter Risk/Reward (bisa disesuaikan)
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
            "time": waktu,
            "reason": reason   # Tambahan info kenapa signal muncul
        }

    def format_combined_message(self, signals):
        """Menggabungkan banyak sinyal ke dalam satu bubble chat (mirip scanner.py)."""
        
        header = f"🔔 <b>MACD CROSSOVER {self.exchange_timeframe.upper()} (12/26/9)</b>\n"
        header += "<i>Strategi: Trending + EMA7 Filter (ADX > 15)</i>\n"
        header += "━━━━━━━━━━━━━━━\n\n"
        
        body = ""
        for s in signals:
            icon = "🟢" if s['side'] == "LONG" else "🔴"
            body += f"{icon} {s['side']} <b>${s['symbol']}</b> NOW ⚡️\n"
            body += f"📍 Entry: {s['entry']}\n"
            body += f"💰 TP: {s['tp1']} - {s['tp2']}\n"
            body += f"⛔ SL: {s['sl']}\n"
            if 'reason' in s and s['reason']:
                body += f"ℹ️ {s['reason']}\n"
            body += "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
            
        footer = f"\nTime: {signals[0]['time']}"
        return header + body + footer