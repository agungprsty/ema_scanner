import time
import logging
import asyncio
from typing import Annotated
import ccxt.async_support as ccxt
from fastapi import FastAPI, Query
from app.services.scanner import TradingBot
from app.services.macd_scanner import MACDScanner
from app.services.ema50_scanner import EMA50Scanner
from app.services.telegram_bot import send_telegram

app = FastAPI(title="Crypto Crossover Bot")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

@app.get("/")
def root():
    return {"status": "running", "version": "v1.0.0"}

@app.get("/api/manual-scan")
async def manual_scan(
    indicator: Annotated[str, Query(description="Select indicator: 'ema' or 'macd'")] = "ema",
    timeframe: Annotated[str, Query(description="Trading timeframe (e.g., 15m, 1h, 4h)")] = "1h",
    limit: Annotated[int, Query(description="Number of candles to fetch")] = 100,
    volume_m: Annotated[int, Query( description="Volume threshold in Millions (1 - 100)", ge=1, le=100)] = 50,
    total_signal: Annotated[int, Query(description="Number of signals to retrieve")] = 5,
    send_to_telegram: Annotated[bool, Query(description="Flag to send results to Telegram")] = True,
):
    """
    Manual scan endpoint supporting two indicators:
    - indicator=ema   → EMA Crossover 7/25 (Asymmetric Bets)
    - indicator=macd  → MACD Crossover 12/26/9 + EMA7 + ADX Filter
    """

    # Indicator validation
    indicator = indicator.lower().strip()
    if indicator not in ["ema", "macd"]:
        return {
            "status": "error",
            "message": "The 'indicator' parameter must be either 'ema' or 'macd'"
        }

    # 1. Init Exchange
    start_time = time.perf_counter()
    exchange = ccxt.binanceusdm({
        'enableRateLimit': True, 
        'options': {'defaultType': 'future'}
    })

    try:
        if indicator == "macd":
            scanner = MACDScanner(exchange, timeframe=timeframe, limit=limit, total_signal=total_signal)
            scanner_type = "MACD Crossover (12/26/9 + EMA7 + ADX Filter)"
        else:
            scanner = TradingBot(exchange, timeframe=timeframe, limit=limit, total_signal=total_signal)
            scanner_type = "EMA Crossover (7/25) - Asymmetric Bets"

        # 2. Filter Market
        markets = await exchange.load_markets()
        all_symbols = [
            s for s, m in markets.items() 
            if m.get('active') and m.get('quote') == 'USDT'
        ]
        
        # Filter liquid symbols (volume > 50 juta)
        tickers = await exchange.fetch_tickers()
        volume_threshold = volume_m * 1_000_000
        liquid_symbols = [
            s for s in all_symbols 
            if tickers.get(s, {}).get('quoteVolume', 0) > volume_threshold
        ]

        # 3. Scanning Paralel
        tasks = [scanner.fetch_and_scan(s) for s in liquid_symbols]
        raw_results = await asyncio.gather(*tasks)

        # 4. Filter sinyal valid
        active_signals = [msg for msg in raw_results if msg is not None]
        
        # 5. Batasi sesuai TOTAL_SIGNALS
        final_signals = active_signals[:scanner.limit_signals]

        if final_signals and send_to_telegram:
            combined_message = scanner.format_combined_message(final_signals)
            await send_telegram(combined_message)

        # 6. Hitung durasi eksekusi
        execution_time = f"{time.perf_counter() - start_time:.2f}s"

        return {
            "status": "success",
            "indicator": indicator.upper(),
            "scanner_type": scanner_type,
            "timeframe": timeframe,
            "limit": limit,
            "execution_time": execution_time,
            "total_scanned": len(liquid_symbols),
            "signals_found": len(active_signals),
            "signals_sent": len(final_signals),
            "data": final_signals
        }

    except Exception as e:
        logging.error(f"Error in manual scan (indicator={indicator}): {str(e)}", exc_info=True)
        return {
            "status": "error",
            "indicator": indicator,
            "message": str(e)
        }
    finally:
        await exchange.close()

@app.get("/api/ema50-scan")
async def ema50_scan(
    timeframe: Annotated[str, Query(description="Trading timeframe (e.g., 15m, 1h, 4h)")] = "15m",
    limit: Annotated[int, Query(description="Number of candles to fetch")] = 100,
    volume_m: Annotated[int, Query(description="Volume threshold in Millions", ge=1, le=100)] = 50,
    total_signal: Annotated[int, Query(description="Number of signals to retrieve")] = 5,
    threshold_pct: Annotated[float, Query(description="Threshold percentage (e.g., 0.95)")] = 0.95,
    send_to_telegram: Annotated[bool, Query(description="Flag to send results to Telegram")] = True,
):
    """
    Endpoint baru untuk mencari token yang harganya menembus/melewati EMA50 
    dengan threshold momentum tertentu (Default 0.95%).
    """
    start_time = time.perf_counter()
    exchange = ccxt.binanceusdm({'enableRateLimit': True, 'options': {'defaultType': 'future'}})

    try:
        # 1. Inisialisasi Scanner Khusus EMA50
        scanner = EMA50Scanner(
            exchange, 
            timeframe=timeframe, 
            limit=limit, 
            total_signal=total_signal,
            threshold=threshold_pct / 100 # Konversi 0.95 ke 0.0095
        )

        # 2. Load Markets & Filter Liquidity (Sesuai logika manual-scan)
        markets = await exchange.load_markets()
        all_symbols = [s for s, m in markets.items() if m.get('active') and m.get('quote') == 'USDT']
        
        tickers = await exchange.fetch_tickers()
        volume_threshold = volume_m * 1_000_000
        liquid_symbols = [
            s for s in all_symbols 
            if tickers.get(s, {}).get('quoteVolume', 0) > volume_threshold
        ]

        # 3. Scanning Paralel
        tasks = [scanner.fetch_and_scan(s) for s in liquid_symbols]
        raw_results = await asyncio.gather(*tasks)

        # 4. Filter & Limit Sinyal
        active_signals = [msg for msg in raw_results if msg is not None]
        final_signals = active_signals[:total_signal]

        if final_signals and send_to_telegram:
            combined_message = scanner.format_combined_message(final_signals)
            await send_telegram(combined_message)

        return {
            "status": "success",
            "scanner_type": f"EMA50 Momentum Breakout ({threshold_pct}%)",
            "execution_time": f"{time.perf_counter() - start_time:.2f}s",
            "signals_found": len(active_signals),
            "data": final_signals
        }

    except Exception as e:
        logging.error(f"Error in ema50 scan: {str(e)}", exc_info=True)
        return {"status": "error", "message": str(e)}
    finally:
        await exchange.close()
