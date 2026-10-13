import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.config.settings import (
    API_SECRET_KEY,
    CORS_ORIGINS,
    DRY_RUN,
    MACD_CROSS_LOOKBACK_CANDLES,
    MACD_FAST,
    MACD_INCLUDE_CURRENT,
    MACD_MAX_SYMBOLS,
    MACD_SIGNAL,
    MACD_SLOW,
    MACD_TIMEFRAME,
    SCAN_CONCURRENCY,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    VOLUME_THRESHOLD_USD,
)
from src.data_feed.binance_client import create_futures_client
from src.data_feed.ohlcv import fetch_candles_payload, fetch_klines
from src.services.cron_scheduler import cron_scheduler
from src.services.db import (
    add_watchlist_symbol,
    close_db,
    delete_watchlist_symbol,
    evaluate_pending_signals,
    get_macd_signals,
    get_macd_signals_summary,
    get_profitloss_history,
    get_signal_by_id,
    get_watchlist,
    init_db,
    is_db_connected,
    record_profit_loss,
    save_macd_signal,
    toggle_watchlist_symbol,
)
from src.services.redis_client import (
    claim_cross,
    close_redis,
    init_redis,
    is_cross_detected,
    is_redis_connected,
    release_cross,
)
from src.services.telegram import send_alert
from src.strategy.macd import compute_macd, detect_macd_cross

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_futures_client()
    return _client


_API_KEY_MIN_LENGTH = 16
_API_KEY_PLACEHOLDERS = frozenset({"your_secret_api_key_here", "changeme", "change_me", "secret", "password"})


def _api_key_configured() -> bool:
    key = API_SECRET_KEY or ""
    return len(key) >= _API_KEY_MIN_LENGTH and key.lower() not in _API_KEY_PLACEHOLDERS


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not _api_key_configured():
        logger.warning(
            "API_SECRET_KEY is unset, a placeholder, or shorter than %d characters; "
            "protected endpoints are disabled and will return 503",
            _API_KEY_MIN_LENGTH,
        )
    await init_db()
    await init_redis()
    cron_scheduler.start(_get_client)
    yield
    cron_scheduler.stop()
    await close_redis()
    await close_db()


app = FastAPI(
    title="Tokyo Drift // Crypto MACD 2H Scanner",
    description="Automated Binance Futures 2H MACD crossover scanner with PostgreSQL, Redis, Cron Scheduler, and Profit/Loss History",
    version="2.2.0",
    lifespan=lifespan,
)

_is_wildcard = CORS_ORIGINS == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=not _is_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def verify_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-KEY")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> bool:
    if not _api_key_configured():
        raise HTTPException(status_code=503, detail="API key not configured on server")

    token = None
    if x_api_key:
        token = x_api_key
    elif authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    if not token or not secrets.compare_digest(token.encode("utf-8"), API_SECRET_KEY.encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Invalid or missing API Key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True

app.mount("/static", StaticFiles(directory="src/static"), name="static")


class WatchlistAddRequest(BaseModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{2,20}$")
    notes: str = Field(default="", max_length=100)


class WatchlistToggleRequest(BaseModel):
    is_active: bool


class ProfitLossRecordRequest(BaseModel):
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    tp1: float
    exit_price: float
    result: str
    timeframe: str = "2h"
    signal_id: int | None = None
    pnl_percent: float = 0.0
    risk_reward_ratio: float = 1.5
    cross_time: str = ""
    exit_time: str = ""


class SignalItem(BaseModel):
    id: int | None = None
    symbol: str
    side: str
    reason: str
    cross_time: str
    cross_price: float
    close: float
    entry_price: float
    stop_loss: float
    tp1: float
    risk_reward_ratio: float
    win_status: str = "PENDING"
    macd: float
    signal: float
    histogram: float
    bars_ago: int
    confirmed: bool
    is_new: bool = True


class ScanResponse(BaseModel):
    status: str
    timeframe: str = "2h"
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


class ProfitLossHistoryResponse(BaseModel):
    total: int
    limit: int
    offset: int
    summary: dict
    history: list[dict]


class SummaryResponse(BaseModel):
    total_signals: int
    long_signals: int
    short_signals: int
    symbols_count: int
    tp1_hit_count: int
    sl_hit_count: int
    pending_count: int
    winrate: float
    avg_rr: float
    watchlist_count: int
    latest_signal_time: str | None = None
    total_net_pnl: float = 0.0
    closed_trades: int = 0


class HealthResponse(BaseModel):
    status: str
    database: bool
    redis: bool
    cron: dict
    timeframe: str = "2h"
    version: str = "2.2.0"


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
        "app": "Tokyo Drift // Crypto MACD 2H Scanner",
        "version": "2.2.0",
        "mode": "DRY_RUN" if DRY_RUN else "PRODUCTION",
        "strategy": f"MACD ({MACD_FAST}, {MACD_SLOW}, {MACD_SIGNAL}) Overcross with Dynamic Swing SL/TP1",
        "enforced_timeframe": "2h",
        "default_lookback": MACD_CROSS_LOOKBACK_CANDLES,
        "max_symbols": MACD_MAX_SYMBOLS,
        "cron": cron_scheduler.get_status(),
    }


@app.get("/api/health", response_model=HealthResponse)
async def health():
    db_ok = await is_db_connected()
    redis_ok = await is_redis_connected()
    cron_info = cron_scheduler.get_status()
    status_str = "healthy" if (db_ok and redis_ok) else "degraded"
    return HealthResponse(
        status=status_str,
        database=db_ok,
        redis=redis_ok,
        cron=cron_info,
        timeframe="2h",
    )


# Watchlist Endpoints
@app.get("/api/watchlist")
async def list_watchlist(active_only: bool = False):
    items = await get_watchlist(active_only=active_only)
    return {"watchlist": items, "count": len(items)}


@app.post("/api/watchlist", dependencies=[Depends(verify_api_key)])
async def add_symbol_to_watchlist(body: WatchlistAddRequest):
    item = await add_watchlist_symbol(symbol=body.symbol, notes=body.notes)
    if not item:
        raise HTTPException(status_code=400, detail="Failed to add symbol to watchlist")
    return {"status": "success", "item": item}


@app.delete("/api/watchlist/{symbol}", dependencies=[Depends(verify_api_key)])
async def remove_symbol_from_watchlist(symbol: str):
    ok = await delete_watchlist_symbol(symbol=symbol)
    if not ok:
        raise HTTPException(status_code=404, detail="Symbol not found in watchlist")
    return {"status": "success", "deleted": symbol}


@app.patch("/api/watchlist/{symbol}/toggle", dependencies=[Depends(verify_api_key)])
async def toggle_symbol_status(symbol: str, body: WatchlistToggleRequest):
    ok = await toggle_watchlist_symbol(symbol=symbol, is_active=body.is_active)
    if not ok:
        raise HTTPException(status_code=404, detail="Symbol not found in watchlist")
    return {"status": "success", "symbol": symbol, "is_active": body.is_active}


# Chart Klines Endpoint
@app.get("/api/klines")
async def get_klines_chart(
    symbol: Annotated[str, Query(description="Symbol name e.g. BTCUSDT")],
    limit: Annotated[int, Query(ge=35, le=500, description="Candles count")] = 100,
):
    client = _get_client()
    payload = await asyncio.to_thread(
        fetch_candles_payload,
        client=client,
        symbol=symbol.upper().strip(),
        timeframe="2h",
        limit=limit,
    )
    if not payload:
        raise HTTPException(status_code=404, detail="Failed to load candle data from Binance")
    return payload


# Single Signal Detail
@app.get("/api/signals/{signal_id}")
async def get_single_signal(signal_id: int):
    sig = await get_signal_by_id(signal_id)
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")
    return sig


# Cron Scheduler Management Endpoints
@app.get("/api/cron/status")
async def get_cron_scheduler_status():
    return cron_scheduler.get_status()


@app.post("/api/cron/trigger", dependencies=[Depends(verify_api_key)])
async def trigger_cron_execution():
    res = await cron_scheduler.trigger_now()
    if res.get("status") in ("skipped", "busy"):
        raise HTTPException(status_code=409, detail=res.get("message", "Cron job is busy"))
    summary = await get_macd_signals_summary()
    return {"status": "success", "result": res, "summary": summary}


# Evaluate Outcome Endpoints
@app.post("/api/evaluate", dependencies=[Depends(verify_api_key)])
@app.post("/api/profitloss-history/evaluate", dependencies=[Depends(verify_api_key)])
async def evaluate_trades():
    client = _get_client()
    eval_res = await evaluate_pending_signals(client)
    summary = await get_macd_signals_summary()
    return {"status": "success", "evaluated": eval_res, "summary": summary}


# Profit/Loss History Endpoints
@app.get("/api/profitloss-history", response_model=ProfitLossHistoryResponse)
async def list_profit_loss_history(
    symbol: Annotated[str | None, Query(description="Filter by symbol")] = None,
    direction: Annotated[str | None, Query(description="Filter by direction (LONG/SHORT)")] = None,
    side: Annotated[str | None, Query(description="Alias for direction")] = None,
    timeframe: Annotated[str | None, Query(description="Filter by timeframe (e.g. 2h)")] = None,
    result: Annotated[str | None, Query(description="Filter by result (PROFIT/LOSS)")] = None,
    date: Annotated[str | None, Query(description="Filter by date (YYYY-MM-DD)")] = None,
    start_date: Annotated[str | None, Query(description="Start date")] = None,
    end_date: Annotated[str | None, Query(description="End date")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page")] = 50,
    offset: Annotated[int, Query(ge=0, description="Page offset")] = 0,
):
    target_dir = direction or side
    records, total, summary = await get_profitloss_history(
        symbol=symbol,
        direction=target_dir,
        timeframe=timeframe,
        result=result,
        date=date,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )
    return ProfitLossHistoryResponse(
        total=total,
        limit=limit,
        offset=offset,
        summary=summary,
        history=records,
    )


@app.post("/api/profitloss-history", dependencies=[Depends(verify_api_key)])
async def add_profit_loss_record(body: ProfitLossRecordRequest):
    item = await record_profit_loss(
        symbol=body.symbol,
        direction=body.direction,
        entry_price=body.entry_price,
        stop_loss=body.stop_loss,
        tp1=body.tp1,
        exit_price=body.exit_price,
        result=body.result,
        timeframe=body.timeframe,
        signal_id=body.signal_id,
        pnl_percent=body.pnl_percent,
        risk_reward_ratio=body.risk_reward_ratio,
        cross_time=body.cross_time,
        exit_time=body.exit_time,
    )
    if not item:
        raise HTTPException(status_code=400, detail="Failed to record profit/loss item")
    return {"status": "success", "item": item}


# Scanner Endpoints (Strict 2h timeframe)
@app.get("/api/macd-scan", response_model=ScanResponse, dependencies=[Depends(verify_api_key)])
@app.get("/api/scan", response_model=ScanResponse, dependencies=[Depends(verify_api_key)])
async def run_macd_scan(
    background_tasks: BackgroundTasks,
    symbols: Annotated[
        str | None,
        Query(description="Comma-separated whitelist. If omitted, uses Database Watchlist."),
    ] = None,
    limit: Annotated[
        int, Query(ge=35, le=1000, description="Candles to fetch")
    ] = 100,
    lookback: Annotated[
        int, Query(ge=1, le=20, description="Candles lookback for crossover")
    ] = MACD_CROSS_LOOKBACK_CANDLES,
    include_current: Annotated[
        bool, Query(description="Include forming candle")
    ] = MACD_INCLUDE_CURRENT,
    volume_min: Annotated[
        float | None, Query(description="Minimum 24h USD volume fallback")
    ] = None,
    send_telegram: Annotated[
        bool, Query(description="Send Telegram alerts")
    ] = True,
    save_db: Annotated[
        bool, Query(description="Save to PostgreSQL")
    ] = True,
):
    start = time.perf_counter()
    client = _get_client()
    timeframe = MACD_TIMEFRAME

    if symbols:
        symbol_list = list(
            dict.fromkeys(s.upper().strip() for s in symbols.split(",") if s.strip())
        )[:MACD_MAX_SYMBOLS]
    else:
        watchlist_items = await get_watchlist(active_only=True)
        if watchlist_items:
            symbol_list = [item["symbol"] for item in watchlist_items][:MACD_MAX_SYMBOLS]
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

    preview = not save_db and not send_telegram
    alert_enabled = send_telegram and bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

    for sig in detected:
        if preview:
            is_new = not await is_cross_detected(
                sig.side, sig.symbol, sig.cross_candle_ms, timeframe=timeframe
            )
        else:
            is_new = await claim_cross(
                sig.side, sig.symbol, sig.cross_candle_ms, timeframe=timeframe
            )
        db_id = None

        if is_new and save_db:
            saved = await save_macd_signal(sig, timeframe=timeframe)
            if saved and "id" in saved:
                db_id = saved["id"]
                if saved.get("inserted") is False:
                    is_new = False
            elif alert_enabled:
                logger.warning(
                    "Failed to save MACD signal %s %s @ %s; alerting without persistence",
                    sig.side,
                    sig.symbol,
                    sig.cross_candle_ms,
                )
            else:
                is_new = False
                await release_cross(
                    sig.side, sig.symbol, sig.cross_candle_ms, timeframe=timeframe
                )
                logger.warning(
                    "Failed to save MACD signal %s %s @ %s; dedup claim released",
                    sig.side,
                    sig.symbol,
                    sig.cross_candle_ms,
                )

        if is_new:
            new_signals_count += 1
            emoji = "🟢" if sig.side == "LONG" else "🔴"
            status_tag = "✅ Confirmed" if sig.is_confirmed else "⏳ In Progress"
            telegram_parts.append(
                f"{emoji} <b>{sig.side}</b> #${sig.symbol} (2h)\n"
                f"Status: {status_tag} | Reason: {sig.reason}\n"
                f"📍 Entry: {sig.entry_price:.4f}\n"
                f"⛔ Stop Loss: {sig.stop_loss:.4f}\n"
                f"🎯 TP1: {sig.tp1:.4f} (R:R 1:{sig.risk_reward_ratio:.1f})\n"
                f"📊 MACD: {sig.macd:.4f} | Signal: {sig.signal:.4f} | Hist: {sig.histogram:.4f}\n"
                f"Time: {sig.cross_time}"
            )

        signals_output.append(
            SignalItem(
                id=db_id,
                symbol=sig.symbol,
                side=sig.side,
                reason=sig.reason,
                cross_time=sig.cross_time,
                cross_price=round(sig.cross_price, 6),
                close=round(sig.close, 6),
                entry_price=round(sig.entry_price, 6),
                stop_loss=round(sig.stop_loss, 6),
                tp1=round(sig.tp1, 6),
                risk_reward_ratio=round(sig.risk_reward_ratio, 2),
                win_status=sig.win_status,
                macd=round(sig.macd, 6),
                signal=round(sig.signal, 6),
                histogram=round(sig.histogram, 6),
                bars_ago=sig.bars_ago,
                confirmed=sig.is_confirmed,
                is_new=is_new,
            )
        )

    # Trigger outcome evaluation asynchronously
    background_tasks.add_task(evaluate_pending_signals, client)

    if send_telegram and telegram_parts:
        header = "🏎️💨 <b>TOKYO DRIFT // MACD 2H SIGNALS</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
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
    direction: Annotated[str | None, Query(description="Alias for side")] = None,
    timeframe: Annotated[str | None, Query(description="Filter by timeframe")] = "2h",
    win_status: Annotated[str | None, Query(description="Filter by win_status (TP1_HIT, SL_HIT, PENDING)")] = None,
    result: Annotated[str | None, Query(description="Filter by result (PROFIT, LOSS, PENDING)")] = None,
    date: Annotated[str | None, Query(description="Filter by date (YYYY-MM-DD)")] = None,
    start_date: Annotated[str | None, Query(description="Start date")] = None,
    end_date: Annotated[str | None, Query(description="End date")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page")] = 50,
    offset: Annotated[int, Query(ge=0, description="Page offset")] = 0,
):
    target_side = direction or side
    records, total = await get_macd_signals(
        symbol=symbol,
        side=target_side,
        timeframe=timeframe,
        win_status=win_status,
        result=result,
        date=date,
        start_date=start_date,
        end_date=end_date,
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
