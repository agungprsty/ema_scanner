import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator, BaseModel, Field, model_validator

from src.config.settings import (
    API_SECRET_KEY,
    CORS_ORIGINS,
    DRY_RUN,
    MACD_CANDLES,
    MACD_CROSS_LOOKBACK_CANDLES,
    MACD_FAST,
    MACD_INCLUDE_CURRENT,
    MACD_MAX_SYMBOLS,
    MACD_SIGNAL,
    MACD_SLOW,
    MACD_TIMEFRAME,
    RISK_REWARD_RATIO,
    ROUND_TRIP_FEE_PERCENT,
    SCAN_CONCURRENCY,
    SIGNAL_EXPIRY_CANDLES,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    VOLUME_THRESHOLD_USD,
)
from src.data_feed.binance_client import create_futures_client
from src.data_feed.ohlcv import fetch_candles_payload, fetch_klines
from src.services.alerts import format_signal_alert, notify_closed_trades
from src.services.cron_scheduler import cron_scheduler
from src.services.db import (
    add_watchlist_symbol,
    close_db,
    delete_journal_entry,
    delete_watchlist_symbol,
    evaluate_pending_signals,
    get_journal,
    get_macd_signals,
    get_macd_signals_summary,
    get_profitloss_history,
    get_signal_by_id,
    get_signals_by_keys,
    get_watchlist,
    init_db,
    is_db_connected,
    record_profit_loss,
    save_macd_signal,
    toggle_watchlist_symbol,
    upsert_journal_entry,
)
from src.services.redis_client import (
    claim_cross,
    close_redis,
    get_cached_json,
    init_redis,
    is_cross_detected,
    is_redis_connected,
    release_cross,
    set_cached_json,
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
    title="Crossline",
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


class JournalRequest(BaseModel):
    status: Literal["TAKEN", "SKIPPED"]
    entry_fill: float | None = Field(default=None, gt=0)
    exit_fill: float | None = Field(default=None, gt=0)
    notes: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def exit_needs_entry(self):
        if self.exit_fill is not None and self.entry_fill is None:
            raise ValueError("exit_fill requires entry_fill")
        return self


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
    failed_symbols: list[str] = []
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
    expired_count: int = 0
    winrate: float
    avg_rr: float
    watchlist_count: int
    latest_signal_time: str | None = None
    total_net_pnl: float = 0.0
    closed_trades: int = 0
    total_r: float = 0.0
    avg_r: float = 0.0
    max_drawdown_r: float = 0.0


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
        "app": "Crossline",
        "version": "2.2.0",
        "mode": "DRY_RUN" if DRY_RUN else "PRODUCTION",
        "strategy": f"MACD ({MACD_FAST}, {MACD_SLOW}, {MACD_SIGNAL}) Overcross with Dynamic Swing SL/TP1",
        "enforced_timeframe": "2h",
        "default_lookback": MACD_CROSS_LOOKBACK_CANDLES,
        "risk_reward_ratio": RISK_REWARD_RATIO,
        "signal_expiry_candles": SIGNAL_EXPIRY_CANDLES,
        "round_trip_fee_percent": ROUND_TRIP_FEE_PERCENT,
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
KLINES_CACHE_TTL = 60
KLINES_MISS_TTL = 120
KLINES_FETCH_LIMIT = 499
KLINES_UPSTREAM_BUDGET = 60
KLINES_BUDGET_WINDOW = 60
_klines_budget = {"window": 0, "count": 0}


def _consume_klines_budget() -> bool:
    window = int(time.time() // KLINES_BUDGET_WINDOW)
    if _klines_budget["window"] != window:
        _klines_budget["window"] = window
        _klines_budget["count"] = 0
    if _klines_budget["count"] >= KLINES_UPSTREAM_BUDGET:
        return False
    _klines_budget["count"] += 1
    return True


def _slice_klines(payload: dict, limit: int) -> dict:
    return {**payload, "candles": payload["candles"][-limit:], "macd": payload["macd"][-limit:]}


KLINES_SYMBOLS_KEY = "klines:symbols"
KLINES_SYMBOLS_TTL = 3600
KLINES_SYMBOLS_RETRY = 60
_klines_symbols: dict = {"symbols": None, "expires": 0.0, "retry_at": 0.0}
_klines_inflight: dict[tuple, asyncio.Future] = {}


async def _single_flight(key: tuple, factory):
    fut = _klines_inflight.get(key)
    if fut is None:
        fut = asyncio.ensure_future(factory())
        _klines_inflight[key] = fut

        def _cleanup(done: asyncio.Future) -> None:
            if _klines_inflight.get(key) is done:
                del _klines_inflight[key]

        fut.add_done_callback(_cleanup)
    return await asyncio.shield(fut)


def _normalize_symbol(value: str) -> str:
    sym = value.strip().upper()
    if not (2 <= len(sym) <= 30 and sym.isalnum()):
        raise ValueError("symbol must be 2-30 letters or digits")
    return sym


async def _refresh_valid_symbols() -> dict[str, str] | None:
    now = time.time()
    cached = await get_cached_json(KLINES_SYMBOLS_KEY)
    symbols = cached.get("symbols") if cached else None
    fetched_at = cached.get("fetched_at", now) if cached else now
    if not symbols:
        try:
            info = await asyncio.to_thread(_get_client().exchange_info)
            symbols = [s["symbol"] for s in info.get("symbols", []) if s.get("symbol")]
        except Exception:
            logger.exception("Failed to load exchange info for symbol validation")
            symbols = None
        if symbols:
            fetched_at = now
            await set_cached_json(
                KLINES_SYMBOLS_KEY, {"symbols": symbols, "fetched_at": now}, ttl=KLINES_SYMBOLS_TTL
            )
    if symbols:
        _klines_symbols["symbols"] = {s.upper(): s for s in symbols}
        _klines_symbols["expires"] = max(fetched_at + KLINES_SYMBOLS_TTL, now + KLINES_SYMBOLS_RETRY)
    else:
        _klines_symbols["retry_at"] = now + KLINES_SYMBOLS_RETRY
    return _klines_symbols["symbols"]


async def _get_valid_symbols() -> dict[str, str] | None:
    now = time.time()
    if now < _klines_symbols["expires"] or now < _klines_symbols["retry_at"]:
        return _klines_symbols["symbols"]
    try:
        return await _single_flight(("symbols",), _refresh_valid_symbols)
    except Exception:
        logger.exception("Symbol list refresh failed")
        return _klines_symbols["symbols"]


async def _load_klines(sym: str, interval: str) -> dict:
    cache_key = f"klines:{interval}:{sym}"
    miss_key = f"{cache_key}:miss"
    cached = await get_cached_json(cache_key)
    if cached:
        return cached

    if await get_cached_json(miss_key):
        raise HTTPException(status_code=502, detail="Failed to load candle data from upstream")

    if not _consume_klines_budget():
        raise HTTPException(status_code=503, detail="Chart data temporarily unavailable, retry later")

    try:
        client = _get_client()
        payload = await asyncio.to_thread(
            fetch_candles_payload,
            client=client,
            symbol=sym,
            timeframe=interval,
            limit=KLINES_FETCH_LIMIT,
        )
    except Exception:
        logger.exception("Klines fetch failed for %s %s", sym, interval)
        payload = None

    if not payload:
        await set_cached_json(miss_key, {"miss": True}, ttl=KLINES_MISS_TTL)
        raise HTTPException(status_code=502, detail="Failed to load candle data from upstream")

    await set_cached_json(cache_key, payload, ttl=KLINES_CACHE_TTL)
    return payload


@app.get("/api/klines")
async def get_klines_chart(
    symbol: Annotated[
        str, Query(max_length=64, description="Symbol name e.g. BTCUSDT"), AfterValidator(_normalize_symbol)
    ],
    interval: Annotated[Literal["2h"], Query(description="Candle interval")] = "2h",
    limit: Annotated[int, Query(ge=50, le=500, description="Candles count")] = 100,
):
    valid = await _get_valid_symbols()
    if valid is not None:
        if symbol not in valid:
            raise HTTPException(status_code=404, detail="Unknown symbol")
        symbol = valid[symbol]
    payload = await _single_flight(("klines", interval, symbol), lambda: _load_klines(symbol, interval))
    return _slice_klines(payload, limit)


# Single Signal Detail
@app.get("/api/signals/{signal_id}")
async def get_single_signal(signal_id: int):
    sig = await get_signal_by_id(signal_id)
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")
    return sig


# Trade Journal Endpoints
@app.get("/api/journal")
async def list_journal(
    status: Annotated[Literal["TAKEN", "SKIPPED"] | None, Query(description="TAKEN or SKIPPED")] = None,
    symbol: Annotated[str | None, Query(max_length=30, description="Filter by symbol")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    items, total, summary = await get_journal(status=status, symbol=symbol, limit=limit, offset=offset)
    return {"total": total, "limit": limit, "offset": offset, "summary": summary, "entries": items}


@app.put("/api/signals/{signal_id}/journal", dependencies=[Depends(verify_api_key)])
async def save_journal_entry(signal_id: int, body: JournalRequest):
    try:
        item = await upsert_journal_entry(
            signal_id,
            status=body.status,
            entry_fill=body.entry_fill,
            exit_fill=body.exit_fill,
            notes=body.notes.strip(),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to save journal entry") from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return item


@app.delete("/api/signals/{signal_id}/journal", dependencies=[Depends(verify_api_key)])
async def remove_journal_entry(signal_id: int):
    if not await delete_journal_entry(signal_id):
        raise HTTPException(status_code=404, detail="No journal entry for this signal")
    return {"status": "success"}


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


async def _evaluate_and_notify(client) -> dict:
    res = await evaluate_pending_signals(client)
    closed = res.pop("closed", None) if isinstance(res, dict) else None
    await notify_closed_trades(closed)
    return res


# Evaluate Outcome Endpoints
@app.post("/api/evaluate", dependencies=[Depends(verify_api_key)])
@app.post("/api/profitloss-history/evaluate", dependencies=[Depends(verify_api_key)])
async def evaluate_trades():
    eval_res = await _evaluate_and_notify(_get_client())
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
        int, Query(ge=35, le=1500, description="Candles to fetch")
    ] = MACD_CANDLES,
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

    failed_symbols: list[str] = []

    async def _detect(sym: str):
        async with sem:
            df = await asyncio.to_thread(fetch_klines, client, sym, timeframe, limit)
            if df is None:
                failed_symbols.append(sym)
                return None
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
        if not sig.is_confirmed:
            # A cross on the forming candle can still disappear before the close, so it is shown
            # but never claimed, stored or alerted; the confirmed cross is picked up after the close
            is_new = False
        elif preview:
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
            telegram_parts.append(format_signal_alert(sig))

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

    stored = await get_signals_by_keys(
        [(sig.symbol, sig.side, sig.cross_candle_ms) for sig in detected], timeframe=timeframe
    )
    for sig, item in zip(detected, signals_output, strict=True):
        record = stored.get((sig.symbol, sig.side, sig.cross_candle_ms))
        if record:
            item.id = record["id"]
            item.win_status = record["win_status"]
            item.cross_time = record["cross_time"]
            item.cross_price = record["cross_price"]
            item.close = record["close_price"]
            item.entry_price = record["entry_price"]
            item.stop_loss = record["stop_loss"]
            item.tp1 = record["tp1"]
            item.risk_reward_ratio = record["risk_reward_ratio"]
            item.confirmed = record["is_confirmed"]

    # Trigger outcome evaluation asynchronously
    background_tasks.add_task(_evaluate_and_notify, client)

    if send_telegram and telegram_parts:
        header = "<b>Crossline: MACD 2H signals</b>\n\n"
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
        failed_symbols=sorted(failed_symbols),
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
