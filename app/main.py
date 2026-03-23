import time
import asyncio
import ccxt.async_support as ccxt
from fastapi import FastAPI
from app.services.scanner import TradingBot
from app.services.telegram_bot import send_telegram

app = FastAPI(title="Crypto Crossover Bot")

@app.get("/")
def root():
    return {"status": "running", "version": "v1.0.0"}

@app.get("/api/manual-scan")
async def manual_scan():
    # 1. Inisialisasi Exchange
    start_time = time.perf_counter()
    exchange = ccxt.binanceusdm({'enableRateLimit': True, 'options': {'defaultType': 'future'}})
    bot = TradingBot(exchange)
    
    try:
        # 2. Filter Market
        markets = await exchange.load_markets()
        all_symbols = [s for s, m in markets.items() if m.get('active') and m.get('quote') == 'USDT']
        
        # Ambil tickers secara paralel untuk filter volume
        tickers = await exchange.fetch_tickers()
        liquid_symbols = [s for s in all_symbols if tickers.get(s, {}).get('quoteVolume', 0) > 10_000_000]
        
        # 3. Scanning Paralel
        tasks = [bot.fetch_and_scan(s) for s in liquid_symbols]
        raw_results = await asyncio.gather(*tasks)

        # 4. Filter hanya yang menghasilkan sinyal (bukan None)
        active_signals = [msg for msg in raw_results if msg is not None]
        
        # 5. Kirim Notifikasi (Sesuai limit TOTAL_SIGNALS)
        final_signals = active_signals[:bot.limit_signals]
        if final_signals:
            combined_message = bot.format_combined_message(final_signals)
            await send_telegram(combined_message)

        # 6. Hitung durasi eksekusi
        execution_time = f"{time.perf_counter() - start_time:.2f}s"

        return {
            "status": "success",
            "execution_time": execution_time,
            "total_scanned": len(liquid_symbols),
            "signals_found": len(active_signals),
            "signals_sent": len(final_signals),
            "data": final_signals
        }
    finally:
        await exchange.close()
