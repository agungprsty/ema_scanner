import pandas_ta as ta
import pandas as pd

class EMA50Scanner:
    def __init__(self, exchange, timeframe, limit, total_signal, threshold):
        self.exchange = exchange
        self.timeframe = timeframe
        self.limit = limit
        self.limit_signals = total_signal
        self.threshold = threshold

    async def fetch_and_scan(self, symbol):
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe=self.timeframe, limit=self.limit)
            if not ohlcv: return None
            
            df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            ema50 = ta.ema(df['c'], length=50)
            
            if ema50 is None or len(ema50) < 1: return None
            
            current_price = df['c'].iloc[-1]
            current_ema = ema50.iloc[-1]
            deviation = (current_price - current_ema) / current_ema

            # Logika Momentum Searah (Sesuai diskusi terakhir)
            if deviation >= self.threshold:
                return {
                    "symbol": symbol,
                    "signal": "LONG",
                    "price": current_price,
                    "ema50": round(current_ema, 4),
                    "deviation_pct": f"{deviation*100:.2f}%"
                }
            elif deviation <= -self.threshold:
                return {
                    "symbol": symbol,
                    "signal": "SHORT",
                    "price": current_price,
                    "ema50": round(current_ema, 4),
                    "deviation_pct": f"{deviation*100:.2f}%"
                }
            return None
        except:
            return None

    def format_combined_message(self, signals):
        msg = "🚀 EMA50 Momentum Signals Found!\n\n"
        for s in signals:
            icon = "🟢" if s['signal'] == "LONG" else "🔴"
            msg += f"{icon} #{s['symbol']} | {s['signal']}\n"
            msg += f"Price: {s['price']} (Dev: {s['deviation_pct']})\n---\n"
        return msg