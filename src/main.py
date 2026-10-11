import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.config.settings import (
    DRY_RUN,
    MACD_CROSS_LOOKBACK_CANDLES,
    MACD_FAST,
    MACD_INCLUDE_CURRENT,
    MACD_MAX_SYMBOLS,
    MACD_SIGNAL,
    MACD_SLOW,
    MACD_TIMEFRAME,
    SCAN_CONCURRENCY,
    VOLUME_THRESHOLD_USD,
)
from src.data_feed.binance_client import create_futures_client
from src.data_feed.ohlcv import fetch_klines
from src.services.db import (
    close_db,
    get_macd_signals,
    get_macd_signals_summary,
    init_db,
    is_db_connected,
    save_macd_signal,
)
from src.services.redis_client import (
    close_redis,
    init_redis,
    is_cross_detected,
    is_redis_connected,
    mark_cross_detected,
)
from src.services.telegram import send_alert
from src.strategy.macd import compute_macd, detect_macd_cross

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_futures_client()
    return _client


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await init_redis()
    yield
    await close_redis()
    await close_db()


app = FastAPI(
    title="Crypto MACD Scanner",
    description="Automated Binance Futures MACD crossover scanner with PostgreSQL and Redis",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="src/static"), name="static")


class SignalItem(BaseModel):
    symbol: str
    side: str
    reason: str
    cross_time: str
    cross_price: float
    close: float
    macd: float
    signal: float
    histogram: float
    bars_ago: int
    confirmed: bool
    is_new: bool = True


class ScanResponse(BaseModel):
    status: str
    timeframe: str
    limit: int
    lookback: int
    include_current: bool
    execution_time: str
    total_scanned: int
    new_signals_count: int
    signals: list[SignalItem]


class SignalsHistoryResponse(BaseModel):
    total: int
    limit: int
    offset: int
    signals: list[dict]


class SummaryResponse(BaseModel):
    total_signals: int
    long_signals: int
    short_signals: int
    symbols_count: int
    latest_signal_time: str | None = None


class HealthResponse(BaseModel):
    status: str
    database: bool
    redis: bool
    version: str = "2.0.0"


@app.get("/")
@app.head("/")
async def root():
    return FileResponse("src/static/index.html")


@app.get("/history")
async def history():
    return FileResponse("src/static/index.html")


@app.get("/api/info")
async def get_info():
    return {
        "status": "running",
        "app": "Crypto MACD Scanner",
        "version": "2.0.0",
        "mode": "DRY_RUN" if DRY_RUN else "PRODUCTION",
        "strategy": f"MACD ({MACD_FAST}, {MACD_SLOW}, {MACD_SIGNAL}) Overcross",
        "default_timeframe": MACD_TIMEFRAME,
        "default_lookback": MACD_CROSS_LOOKBACK_CANDLES,
        "max_symbols": MACD_MAX_SYMBOLS,
    }


@app.get("/api/health", response_model=HealthResponse)
async def health():
    db_ok = await is_db_connected()
    redis_ok = await is_redis_connected()
    status_str = "healthy" if (db_ok and redis_ok) else "degraded"
    return HealthResponse(status=status_str, database=db_ok, redis=redis_ok)


@app.get("/api/macd-scan", response_model=ScanResponse)
@app.get("/api/scan", response_model=ScanResponse)
async def run_macd_scan(
    background_tasks: BackgroundTasks,
    symbols: Annotated[
        str | None,
        Query(
            description="Comma-separated whitelist symbols (e.g. BTCUSDT,ETHUSDT). Omit to scan top liquid pairs."
        ),
    ] = None,
    timeframe: Annotated[
        str, Query(description="MACD timeframe (e.g. 15m, 1h, 4h, 1d)")
    ] = MACD_TIMEFRAME,
    limit: Annotated[
        int, Query(ge=35, le=1000, description="Number of candles to fetch")
    ] = 100,
    lookback: Annotated[
        int, Query(ge=1, le=20, description="Candles lookback for crossover")
    ] = MACD_CROSS_LOOKBACK_CANDLES,
    include_current: Annotated[
        bool, Query(description="Include forming candle")
    ] = MACD_INCLUDE_CURRENT,
    volume_min: Annotated[
        float | None, Query(description="Minimum 24h USD volume")
    ] = None,
    send_telegram: Annotated[
        bool, Query(description="Send Telegram alerts for new signals")
    ] = True,
    save_db: Annotated[
        bool, Query(description="Persist detected signals to PostgreSQL")
    ] = True,
):
    start = time.perf_counter()
    client = _get_client()

    if symbols:
        symbol_list = list(
            dict.fromkeys(
                s.upper().strip() for s in symbols.split(",") if s.strip()
            )
        )[:MACD_MAX_SYMBOLS]
    else:
        threshold = volume_min if volume_min is not None else VOLUME_THRESHOLD_USD
        stats_24h = await asyncio.to_thread(client.ticker_24hr_price_change)
        if isinstance(stats_24h, dict) and "data" in stats_24h:
            stats_24h = stats_24h["data"]
        pairs = [
            s
            for s in stats_24h
            if s.get("symbol", "").endswith("USDT")
            and float(s.get("quoteVolume", 0)) >= threshold
        ]
        pairs.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
        symbol_list = [p["symbol"] for p in pairs[:MACD_MAX_SYMBOLS]]

    if not symbol_list:
        return ScanResponse(
            status="error",
            timeframe=timeframe,
            limit=limit,
            lookback=lookback,
            include_current=include_current,
            execution_time=f"{time.perf_counter() - start:.2f}s",
            total_scanned=0,
            new_signals_count=0,
            signals=[],
        )

    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def _detect(sym: str):
        async with sem:
            df = await asyncio.to_thread(fetch_klines, client, sym, timeframe, limit)
            if df is None:
                return None
            if not include_current:
                df = df.iloc[:-1].copy()
            df = compute_macd(df, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
            if df is None:
                return None
            return detect_macd_cross(
                df, sym, lookback_candles=lookback, include_current=include_current
            )

    tasks = [_detect(sym) for sym in symbol_list]
    results = await asyncio.gather(*tasks)
    detected = [r for r in results if r is not None]

    signals_output: list[SignalItem] = []
    telegram_parts = []
    new_signals_count = 0

    for sig in detected:
        is_dup = await is_cross_detected(
            sig.side, sig.symbol, sig.cross_candle_ms, timeframe=timeframe
        )
        is_new = not is_dup

        if is_new:
            new_signals_count += 1
            await mark_cross_detected(
                sig.side, sig.symbol, sig.cross_candle_ms, timeframe=timeframe
            )
            if save_db:
                await save_macd_signal(sig, timeframe=timeframe)

            emoji = "🟢" if sig.side == "LONG" else "🔴"
            status_tag = "✅ Confirmed" if sig.is_confirmed else "⏳ In Progress"
            telegram_parts.append(
                f"{emoji} <b>{sig.side}</b> #${sig.symbol} ({timeframe})\n"
                f"Status: {status_tag} | Reason: {sig.reason}\n"
                f"Cross Price: {sig.cross_price:.6f} (Now: {sig.close:.6f})\n"
                f"MACD: {sig.macd:.4f} | Signal: {sig.signal:.4f} | Hist: {sig.histogram:.4f}\n"
                f"Bars Ago: {sig.bars_ago} candle(s)\n"
                f"Time: {sig.cross_time}"
            )

        signals_output.append(
            SignalItem(
                symbol=sig.symbol,
                side=sig.side,
                reason=sig.reason,
                cross_time=sig.cross_time,
                cross_price=round(sig.cross_price, 6),
                close=round(sig.close, 6),
                macd=round(sig.macd, 6),
                signal=round(sig.signal, 6),
                histogram=round(sig.histogram, 6),
                bars_ago=sig.bars_ago,
                confirmed=sig.is_confirmed,
                is_new=is_new,
            )
        )

    if send_telegram and telegram_parts:
        header = f"🔔 <b>MACD OVERCROSS SIGNALS ({timeframe})</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
        alert_body = header + "\n\n".join(telegram_parts)
        background_tasks.add_task(send_alert, alert_body)

    elapsed = f"{time.perf_counter() - start:.2f}s"
    return ScanResponse(
        status="success",
        timeframe=timeframe,
        limit=limit,
        lookback=lookback,
        include_current=include_current,
        execution_time=elapsed,
        total_scanned=len(symbol_list),
        new_signals_count=new_signals_count,
        signals=signals_output,
    )


@app.get("/api/signals", response_model=SignalsHistoryResponse)
async def list_signals(
    symbol: Annotated[str | None, Query(description="Filter by symbol")] = None,
    side: Annotated[str | None, Query(description="Filter by side (LONG/SHORT)")] = None,
    timeframe: Annotated[str | None, Query(description="Filter by timeframe")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page")] = 50,
    offset: Annotated[int, Query(ge=0, description="Page offset")] = 0,
):
    records, total = await get_macd_signals(
        symbol=symbol,
        side=side,
        timeframe=timeframe,
        limit=limit,
        offset=offset,
    )
    return SignalsHistoryResponse(
        total=total,
        limit=limit,
        offset=offset,
        signals=records,
    )


@app.get("/api/summary", response_model=SummaryResponse)
async def signals_summary():
    data = await get_macd_signals_summary()
    return SummaryResponse(**data)
