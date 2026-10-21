import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from src.config.settings import (
    API_SECRET_KEY,
    CORS_ORIGINS,
    DRY_RUN,
    FILL_WINDOW_CANDLES,
    INTERVAL_MS,
    MAX_HOLD_CANDLES,
    MAX_SYMBOLS,
    ROUND_TRIP_FEE_PERCENT,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_PERIOD,
    SL_PERCENT,
    TIMEFRAME,
    TIMEFRAME_MS,
    TP_PERCENT,
)
from src.data_feed.binance_client import create_futures_client
from src.data_feed.ohlcv import fetch_candles_range
from src.services.cron_scheduler import cron_scheduler
from src.services.db import (
    ACTIVE_STATUSES,
    add_watchlist_symbol,
    close_db,
    delete_journal_entry,
    delete_watchlist_symbol,
    get_journal,
    get_signal,
    get_watchlist,
    init_db,
    is_db_connected,
    list_signals,
    signal_stats,
    toggle_watchlist_symbol,
    upsert_journal_entry,
)
from src.services.redis_client import (
    close_redis,
    get_cached_json,
    init_redis,
    is_redis_connected,
    set_cached_json,
)
from src.strategy.rsi import compute_rsi

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


APP_VERSION = "3.0.0"

app = FastAPI(
    title="Crossline",
    description="Binance Futures RSI 28/72 limit-entry scanner with PostgreSQL, Redis and a per-candle scheduler",
    version=APP_VERSION,
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


class SignalsHistoryResponse(BaseModel):
    total: int
    limit: int
    offset: int
    summary: dict = {}
    signals: list[dict]


class SummaryResponse(BaseModel):
    total_signals: int
    long_signals: int
    short_signals: int
    symbols_count: int
    pending_count: int
    open_count: int
    tp_hit_count: int
    sl_hit_count: int
    expired_count: int
    not_filled_count: int
    filled_count: int
    fill_rate: float
    closed_trades: int
    winrate: float
    total_net_pnl: float
    total_r: float
    avg_r: float
    max_drawdown_r: float
    watchlist_count: int
    latest_signal_time: str | None = None


class HealthResponse(BaseModel):
    status: str
    database: bool
    redis: bool
    cron: dict
    timeframe: str = TIMEFRAME
    version: str = APP_VERSION


@app.get("/")
@app.head("/")
async def root():
    return FileResponse("src/static/index.html")


@app.get("/api/info")
async def get_info():
    return {
        "status": "running",
        "app": "Crossline",
        "version": APP_VERSION,
        "mode": "DRY_RUN" if DRY_RUN else "PRODUCTION",
        "timeframe": TIMEFRAME,
        "interval_ms": TIMEFRAME_MS,
        "strategy": {
            "rsi_period": RSI_PERIOD,
            "oversold": RSI_OVERSOLD,
            "overbought": RSI_OVERBOUGHT,
            "tp_percent": TP_PERCENT,
            "sl_percent": SL_PERCENT,
            "fill_window_candles": FILL_WINDOW_CANDLES,
            "max_hold_candles": MAX_HOLD_CANDLES,
            "fee_percent": ROUND_TRIP_FEE_PERCENT,
        },
        "max_symbols": MAX_SYMBOLS,
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


# Chart candles: upstream budget and single-flight shared by every chart request
KLINES_CACHE_TTL = 60
KLINES_MISS_TTL = 120
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


# Single Signal Detail
@app.get("/api/signals/{signal_id}")
async def get_single_signal(signal_id: int):
    sig = await get_signal(signal_id)
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


# Candles framing one signal's trade, with RSI, for its chart
SIGNAL_CANDLES_BEFORE = 30
SIGNAL_CANDLES_AFTER = 6
SIGNAL_RSI_WARMUP = 300  # extra bars fetched before the window so the first charted RSI has settled
# A closed trade's window never changes once all its candles have closed
SIGNAL_CANDLES_FINAL_TTL = 7 * 24 * 3600


def _signal_candle_window(sig: dict, now_ms: int) -> tuple[int, int, bool]:
    """(start_ms, end_ms, final) of the candles to chart for a signal.

    Active trades run from before the signal to now. Closed ones stop a few candles after the
    close, and are final once every candle in that range has closed.
    """
    interval_ms = INTERVAL_MS.get(sig.get("timeframe"))
    if interval_ms is None:
        raise HTTPException(status_code=500, detail="Signal has an unsupported timeframe")
    start_ms = int(sig["signal_candle_ms"]) - SIGNAL_CANDLES_BEFORE * interval_ms
    closed_ms = sig.get("closed_ms")
    if sig.get("status") in ACTIVE_STATUSES or closed_ms is None:
        return start_ms, now_ms, False
    last_open_ms = closed_ms - closed_ms % interval_ms + SIGNAL_CANDLES_AFTER * interval_ms
    final = last_open_ms + interval_ms <= now_ms
    return start_ms, min(last_open_ms, now_ms), final


def _chart_payload(sig: dict, candles: list[dict], start_ms: int) -> dict:
    """Candles from `start_ms` on, with RSI over the whole fetch so warm-up bars are dropped, not zeroed."""
    rsi = compute_rsi(pd.Series([c["close"] for c in candles], dtype=float))
    start_s = start_ms // 1000
    return {
        "symbol": sig["symbol"],
        "timeframe": sig["timeframe"],
        "candles": [c for c in candles if c["time"] >= start_s],
        "rsi": [
            {"time": c["time"], "value": round(float(v), 2)}
            for c, v in zip(candles, rsi, strict=True)
            if c["time"] >= start_s and pd.notna(v)
        ],
    }


async def _load_signal_chart(sig: dict, start_ms: int, end_ms: int, cache_key: str, ttl: int) -> dict:
    miss_key = f"{cache_key}:miss"
    cached = await get_cached_json(cache_key)
    if cached:
        return cached
    if await get_cached_json(miss_key):
        raise HTTPException(status_code=502, detail="Failed to load candle data from upstream")
    if not _consume_klines_budget():
        raise HTTPException(status_code=503, detail="Chart data temporarily unavailable, retry later")

    fetch_from = start_ms - SIGNAL_RSI_WARMUP * INTERVAL_MS[sig["timeframe"]]
    try:
        # shortcut: one request caps at 1500 candles, so 1m/3m trades lose their tail; page if those TFs get used
        candles = await asyncio.to_thread(
            fetch_candles_range, _get_client(), sig["symbol"], sig["timeframe"], fetch_from, end_ms
        )
    except Exception:
        logger.exception("Signal chart fetch failed for signal %s", sig.get("id"))
        candles = None
    if not candles:
        await set_cached_json(miss_key, {"miss": True}, ttl=KLINES_MISS_TTL)
        raise HTTPException(status_code=502, detail="Failed to load candle data from upstream")

    payload = _chart_payload(sig, candles, start_ms)
    await set_cached_json(cache_key, payload, ttl=ttl)
    return payload


@app.get("/api/signals/{signal_id}/chart")
async def get_signal_chart(signal_id: int, response: Response):
    sig = await get_signal(signal_id)
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")

    start_ms, end_ms, final = _signal_candle_window(sig, int(time.time() * 1000))
    ttl = SIGNAL_CANDLES_FINAL_TTL if final else KLINES_CACHE_TTL
    cache_key = f"chart:{signal_id}:{'final' if final else 'live'}"
    payload = await _single_flight(
        ("signal-chart", signal_id, final),
        lambda: _load_signal_chart(sig, start_ms, end_ms, cache_key, ttl),
    )
    if final:
        # Only a closed window is immutable; a live one must refetch to show new candles
        response.headers["Cache-Control"] = f"public, max-age={ttl}"
    return {**payload, "final": final}


# Cron Scheduler Management Endpoints
@app.get("/api/cron/status")
async def get_cron_scheduler_status():
    return cron_scheduler.get_status()


@app.post("/api/cron/trigger", dependencies=[Depends(verify_api_key)])
async def trigger_cron_execution():
    res = await cron_scheduler.trigger_now()
    if res.get("status") in ("skipped", "busy"):
        raise HTTPException(status_code=409, detail=res.get("message", "Cron job is busy"))
    if res.get("status") == "error":
        raise HTTPException(status_code=503, detail=res.get("message", "Job failed"))
    summary = await signal_stats()
    return {"status": "success", "result": res, "summary": summary}


@app.get("/api/signals", response_model=SignalsHistoryResponse)
async def list_signals_endpoint(
    status: Annotated[str | None, Query(max_length=100, description="Comma-separated statuses, e.g. PENDING,OPEN")] = None,
    side: Annotated[str | None, Query(max_length=5, description="Filter by side (LONG/SHORT)")] = None,
    symbol: Annotated[str | None, Query(max_length=30, description="Filter by symbol")] = None,
    timeframe: Annotated[str | None, Query(max_length=4, description="Filter by timeframe")] = None,
    date: Annotated[str | None, Query(description="WIB day (YYYY-MM-DD)")] = None,
    start_date: Annotated[str | None, Query(description="First WIB day (YYYY-MM-DD)")] = None,
    end_date: Annotated[str | None, Query(description="Last WIB day, inclusive (YYYY-MM-DD)")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page")] = 50,
    offset: Annotated[int, Query(ge=0, description="Page offset")] = 0,
):
    try:
        records, total, summary = await list_signals(
            status=status,
            side=side,
            symbol=symbol,
            timeframe=timeframe,
            date=date,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Dates must be YYYY-MM-DD") from exc
    return SignalsHistoryResponse(
        total=total,
        limit=limit,
        offset=offset,
        summary=summary,
        signals=records,
    )


@app.get("/api/summary", response_model=SummaryResponse)
async def signals_summary():
    return SummaryResponse(**await signal_stats())
