import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from src.config.settings import (
    API_SECRET_KEY,
    APP_MODE,
    COOKIE_SECURE,
    CORS_ORIGINS,
    COST_MODEL_VERSION,
    ENABLE_API_DOCS,
    HEALTH_PROBE_TIMEOUT_SECONDS,
    MACD_CANDLES,
    MACD_CROSS_LOOKBACK_CANDLES,
    MACD_FAST,
    MACD_INCLUDE_CURRENT,
    MACD_MAX_SYMBOLS,
    MACD_SIGNAL,
    MACD_SLOW,
    MACD_TIMEFRAME,
    REQUIRE_CRON,
    RISK_REWARD_RATIO,
    ROUND_TRIP_FEE_PERCENT,
    SCAN_CONCURRENCY,
    SESSION_TTL_SECONDS,
    SIGNAL_EXPIRY_CANDLES,
    SLIPPAGE_BPS_BY_SYMBOL,
    SLIPPAGE_BPS_DEFAULT,
    STOP_SLIPPAGE_MULTIPLIER,
    STRATEGY_KEY,
    STRATEGY_VERSION,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    VOLUME_THRESHOLD_USD,
)
from src.data_feed.binance_client import create_futures_client
from src.data_feed.ohlcv import fetch_candles_payload, fetch_candles_range, fetch_klines
from src.ledger.candles import context_fingerprint
from src.services.alerts import format_outcome_alert, format_signal_alert, notify_closed_trades
from src.services.auth import issue_session_token, verify_session_token
from src.services.background import run_in_background
from src.services.cron_scheduler import cron_scheduler
from src.services.db import (
    SignalTradeConflict,
    add_watchlist_symbol,
    close_db,
    delete_journal_entry,
    delete_watchlist_symbol,
    evaluate_pending_signals,
    get_cron_runs,
    get_journal,
    get_macd_signals,
    get_macd_signals_summary,
    get_performance,
    get_profitloss_adjustments,
    get_profitloss_history,
    get_signal_by_id,
    get_signals_by_keys,
    get_track_record,
    get_watchlist,
    init_db,
    is_db_connected,
    record_alert_events,
    record_profit_loss,
    save_macd_signal,
    toggle_watchlist_symbol,
    upsert_journal_entry,
)
from src.services.ledger import (
    LEDGER_COLUMNS,
    export_jsonl,
    export_rows,
    filter_rows,
    get_ledger_head,
    get_recent_events,
    get_signal_candle_window,
    get_signal_events,
    rows_to_csv,
    verify_ledger,
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
from src.strategy import registry
from src.strategy.context import MarketContext
from src.strategy.macd import compute_macd, detect_macd_cross
from src.strategy.outcome import CANDLE_INTERVAL_MS

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

APP_VERSION = "2.2.0"

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
    version=APP_VERSION,
    lifespan=lifespan,
    # The API surface is not advertised publicly; ENABLE_API_DOCS=true turns the docs back on
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
)

_is_wildcard = CORS_ORIGINS == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=not _is_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Dashboard sessions. The browser signs in once with the API key and then holds an HttpOnly cookie, so
# the key never sits in page-readable storage. Scripts keep sending X-API-KEY (or a Bearer token)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-Requested-With"
CSRF_HEADER_VALUE = "crossline"


def _session_cookie_name() -> str:
    # The __Host- prefix makes browsers refuse the cookie unless it is Secure, host-only and path=/
    return "__Host-crossline_session" if COOKIE_SECURE else "crossline_session"


def _key_matches(token: str | None) -> bool:
    return bool(token) and secrets.compare_digest(token.encode("utf-8"), API_SECRET_KEY.encode("utf-8"))


def _header_token(x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _has_valid_session(request: Request) -> bool:
    return verify_session_token(request.cookies.get(_session_cookie_name()), API_SECRET_KEY)


def is_authenticated(request: Request) -> bool:
    """Non-raising check, for public endpoints that show more to a signed-in owner."""
    if not _api_key_configured():
        return False
    token = _header_token(request.headers.get("X-API-KEY"), request.headers.get("Authorization"))
    if token is not None:
        return _key_matches(token)
    return _has_valid_session(request)


async def verify_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-KEY")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> bool:
    if not _api_key_configured():
        raise HTTPException(status_code=503, detail="API key not configured on server")

    unauthorized = HTTPException(
        status_code=401,
        detail="Unauthorized: Invalid or missing API Key",
        headers={"WWW-Authenticate": "Bearer"},
    )
    token = _header_token(x_api_key, authorization)
    if token is not None:
        # An explicit credential is judged on its own, never rescued by a cookie
        if not _key_matches(token):
            raise unauthorized
        return True

    if not _has_valid_session(request):
        raise unauthorized
    # Cookies ride along on cross-site requests; a custom header cannot be added without a CORS
    # preflight, so requiring it on writes backs up SameSite=Strict against CSRF
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_HEADER_VALUE:
        raise HTTPException(status_code=403, detail=f"Missing {CSRF_HEADER}: {CSRF_HEADER_VALUE} header")
    return True


app.mount("/static", StaticFiles(directory="src/static"), name="static")


class LoginRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=256)


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


def _upper(value):
    return value.strip().upper() if isinstance(value, str) else value


class ProfitLossRecordRequest(BaseModel):
    """A hand-entered trade: stored as booked_by="manual" and never counted in the track record."""

    symbol: str
    direction: Annotated[Literal["LONG", "SHORT"], BeforeValidator(_upper)]
    entry_price: float
    stop_loss: float
    tp1: float
    exit_price: float
    result: Annotated[Literal["PROFIT", "LOSS", "EXPIRED"], BeforeValidator(_upper)]
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
    entry_basis: str = "next_open"
    # Set once the signal is stored; a manual scan stores signals as "manual", never eligible
    source: str | None = None
    is_eligible: bool = False


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


def _as_booked(description: str):
    return Field(None, description=f"Deprecated: {description}", json_schema_extra={"deprecated": True})


class SignalsSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    open_count: int = 0
    profit_count: int = 0
    loss_count: int = 0
    expired_count: int = 0
    closed_count: int = 0
    total_r: float | None = _as_booked(
        "booked R summed over every closed row, legacy fee-free rows included. Use eligible_net_r."
    )
    total_pnl_percent: float | None = _as_booked("booked percent summed over every closed row.")
    strategy_version: str | None = None
    eligible_closed_count: int = 0
    eligible_net_r: float = 0.0
    excluded_count: int = 0


class SignalsHistoryResponse(BaseModel):
    total: int
    limit: int
    offset: int
    summary: SignalsSummary = SignalsSummary()
    signals: list[dict]


class ProfitLossSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    total_trades: int = 0
    profit_count: int = 0
    loss_count: int = 0
    expired_count: int = 0
    winrate: float | None = _as_booked(
        "percent of every listed row booked as PROFIT, legacy and hand-entered rows included. Use eligible_win_rate."
    )
    total_pnl_percent: float | None = _as_booked("booked percent summed over every listed row.")
    total_r: float | None = _as_booked(
        "booked R summed over every listed row; fees only for rows closed before the cost model. Use eligible_net_r."
    )
    avg_r: float | None = _as_booked("mean booked R over every listed row.")
    strategy_version: str | None = None
    eligible_closed_count: int = 0
    eligible_win_count: int = 0
    eligible_win_rate: float | None = None
    eligible_net_r: float = 0.0
    note: str = ""


class ProfitLossHistoryResponse(BaseModel):
    total: int
    limit: int
    offset: int
    summary: ProfitLossSummary
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
    # The planned reward-to-risk of every signal, not a realised result
    target_rr: float
    watchlist_count: int
    latest_signal_time: str | None = None
    total_net_pnl: float = Field(
        0.0,
        description="Deprecated: booked percent results of the eligible trades, summed across different stop "
        "distances (fees only for trades closed before the cost model). Use total_r.",
        json_schema_extra={"deprecated": True},
    )
    closed_trades: int = 0
    total_r: float = 0.0
    avg_r: float = 0.0
    max_drawdown_r: float = 0.0
    eligible_only: bool = True
    strategy_version: str | None = None
    note: str = ""


class ExcludedCounts(BaseModel):
    legacy: int = 0
    catchup: int = 0
    manual: int = 0
    other_version: int = 0
    # Signals with no ledger events: put in the table outside the app (see /api/ledger/verify)
    outside_ledger: int = 0
    other: int = 0


class PerformanceResponse(BaseModel):
    strategy_key: str
    strategy_version: str
    cost_model_version: str
    filters: dict
    n_closed: int
    wins: int
    losses: int
    expired: int
    win_rate: float | None
    win_rate_ci95: list[float] | None
    mean_net_r: float | None
    mean_net_r_ci95: list[float] | None
    total_net_r: float
    mean_gross_r: float | None
    mean_cost_r: float | None
    profit_factor: float | None
    max_drawdown_r: float
    break_even_win_rate: float | None
    # None for a strategy without a fixed target, whose win rate counts trades closed with a net R above zero
    target_rr: float | None
    win_rule: Literal["target_hit", "net_r_positive"] = "target_hit"
    open_count: int
    excluded: ExcludedCounts
    sample_size_warning: str | None
    min_sample: int
    min_sample_for_totals: int
    first_exit_time: str | None
    last_exit_time: str | None
    definitions: dict[str, str]


class HealthResponse(BaseModel):
    # "healthy", "degraded" (Redis down, the scheduled scan overdue, or switched off while REQUIRE_CRON
    # is set) or "down" (the database is down or does not answer in time)
    status: Literal["healthy", "degraded", "down"]
    db: bool
    redis: bool
    # None when the scheduler is disabled
    cron_fresh: bool | None
    version: str = APP_VERSION


async def _health_checks() -> tuple[bool, bool, bool | None]:
    """Database, Redis and cron freshness, checked at once within HEALTH_PROBE_TIMEOUT_SECONDS.

    A check still running at the deadline counts as failed and is cancelled in the background, so the
    cleanup of a hung connection never delays the answer.
    """
    checks = [(is_db_connected(), False), (is_redis_connected(), False), (cron_scheduler.is_fresh(), False)]
    tasks = [run_in_background(check, name="health-check") for check, _ in checks]
    done, pending = await asyncio.wait(tasks, timeout=HEALTH_PROBE_TIMEOUT_SECONDS)
    for task in pending:
        task.cancel()
    results = []
    for task, (_, failed) in zip(tasks, checks, strict=True):
        ok = task in done and not task.cancelled() and task.exception() is None
        results.append(task.result() if ok else failed)
    return results[0], results[1], results[2]


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
        "version": APP_VERSION,
        # Signals only: there is no order code, and no edge has been validated out of sample
        "mode": APP_MODE,
        "edge_validated": False,
        "strategy": f"MACD ({MACD_FAST}, {MACD_SLOW}, {MACD_SIGNAL}) Overcross with Dynamic Swing SL/TP1",
        "strategy_key": STRATEGY_KEY,
        "strategy_version": STRATEGY_VERSION,
        "enforced_timeframe": "2h",
        "default_lookback": MACD_CROSS_LOOKBACK_CANDLES,
        "risk_reward_ratio": RISK_REWARD_RATIO,
        "signal_expiry_candles": SIGNAL_EXPIRY_CANDLES,
        "round_trip_fee_percent": ROUND_TRIP_FEE_PERCENT,
        "cost_model": {
            "version": COST_MODEL_VERSION,
            "round_trip_fee_percent": ROUND_TRIP_FEE_PERCENT,
            "slippage_bps_default": SLIPPAGE_BPS_DEFAULT,
            "slippage_bps_by_symbol": SLIPPAGE_BPS_BY_SYMBOL,
            "stop_slippage_multiplier": STOP_SLIPPAGE_MULTIPLIER,
        },
        "max_symbols": MACD_MAX_SYMBOLS,
        # Every registered strategy version with its frozen parameters and hashes; the scheduled scan
        # runs the live and shadow ones (shadow signals are stored and evaluated, never alerted)
        "strategies": [s.describe() for s in registry.all_strategies()],
        "cron": cron_scheduler.get_status(),
    }


@app.get("/api/health", response_model=HealthResponse)
async def health(response: Response):
    """Public liveness summary: booleans only, no error text. 503 only when the database is down or
    does not answer in time, so the container healthcheck fails on that and nothing else. Every check
    runs at once with its own short timeout, so the answer comes well inside the healthcheck's 5s."""
    db_ok, redis_ok, cron_fresh = await _health_checks()
    if not db_ok:
        status_str = "down"
        response.status_code = 503
    elif not redis_ok or cron_fresh is False or (REQUIRE_CRON and cron_fresh is None):
        status_str = "degraded"
    else:
        status_str = "healthy"
    return HealthResponse(status=status_str, db=db_ok, redis=redis_ok, cron_fresh=cron_fresh)


# Dashboard sign-in
@app.post("/api/auth/login")
async def login(body: LoginRequest, response: Response):
    if not _api_key_configured():
        raise HTTPException(status_code=503, detail="API key not configured on server")
    if not _key_matches(body.api_key):
        raise HTTPException(status_code=401, detail="Wrong API key")
    token, expires = issue_session_token(API_SECRET_KEY, SESSION_TTL_SECONDS)
    response.set_cookie(
        _session_cookie_name(),
        token,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        secure=COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )
    return {"authenticated": True, "expires_at": datetime.fromtimestamp(expires, UTC).isoformat()}


@app.post("/api/auth/logout")
async def logout(response: Response):
    # Tokens are stateless: this ends the session in this browser; rotate API_SECRET_KEY to end all
    response.delete_cookie(_session_cookie_name(), path="/", secure=COOKIE_SECURE, httponly=True, samesite="strict")
    return {"authenticated": False}


@app.get("/api/auth/session")
async def auth_session(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": is_authenticated(request), "configured": _api_key_configured()}


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
    response: Response,
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
    # Let the browser reuse candles for as long as the server cache does, so reopening a chart is free
    response.headers["Cache-Control"] = f"public, max-age={KLINES_CACHE_TTL}"
    return _slice_klines(payload, limit)


# Single Signal Detail
@app.get("/api/signals/{signal_id}")
async def get_single_signal(signal_id: int, request: Request):
    sig = await get_signal_by_id(signal_id)
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")
    # The ledger is public, the owner's journal is not
    if not is_authenticated(request):
        sig.pop("journal", None)
    return sig


# Trade Journal Endpoints
@app.get("/api/journal", dependencies=[Depends(verify_api_key)])
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


# Candles framing one signal's trade, for its chart
SIGNAL_CANDLES_BEFORE = 30
SIGNAL_CANDLES_AFTER = 6
# A closed trade's window never changes once all its candles have closed
SIGNAL_CANDLES_FINAL_TTL = 7 * 24 * 3600


def _parse_utc_ms(value: str | None) -> int | None:
    """Parse stored UTC times, with or without a zone ("2026-10-11T06:00:00Z", "2026-10-11 14:00:00")."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _signal_candle_window(sig: dict, now_ms: int) -> tuple[int, int, bool]:
    """(start_ms, end_ms, final) of the candles to chart for a signal.

    Open signals run from before the cross to now. Closed ones stop a few candles after the exit,
    and are final once every candle in that range has closed.
    """
    interval_ms = CANDLE_INTERVAL_MS.get(sig.get("timeframe") or "2h", CANDLE_INTERVAL_MS["2h"])
    cross_ms = int(sig["cross_candle_ms"])
    start_ms = cross_ms - SIGNAL_CANDLES_BEFORE * interval_ms
    if sig.get("win_status", "PENDING") == "PENDING":
        return start_ms, now_ms, False
    exit_ms = _parse_utc_ms(sig.get("exit_time")) or cross_ms + (SIGNAL_EXPIRY_CANDLES + 1) * interval_ms
    last_open_ms = exit_ms - exit_ms % interval_ms + SIGNAL_CANDLES_AFTER * interval_ms
    final = last_open_ms + interval_ms <= now_ms
    return start_ms, min(last_open_ms, now_ms), final


async def _load_signal_candles(sig: dict, start_ms: int, end_ms: int, cache_key: str, ttl: int) -> dict:
    miss_key = f"{cache_key}:miss"
    cached = await get_cached_json(cache_key)
    if cached:
        return cached
    if await get_cached_json(miss_key):
        raise HTTPException(status_code=502, detail="Failed to load candle data from upstream")
    if not _consume_klines_budget():
        raise HTTPException(status_code=503, detail="Chart data temporarily unavailable, retry later")

    timeframe = sig.get("timeframe") or "2h"
    try:
        candles = await asyncio.to_thread(
            fetch_candles_range, _get_client(), sig["symbol"], timeframe, start_ms, end_ms
        )
    except Exception:
        logger.exception("Signal candles fetch failed for signal %s", sig.get("id"))
        candles = None
    if not candles:
        await set_cached_json(miss_key, {"miss": True}, ttl=KLINES_MISS_TTL)
        raise HTTPException(status_code=502, detail="Failed to load candle data from upstream")

    payload = {"symbol": sig["symbol"], "timeframe": timeframe, "candles": candles}
    await set_cached_json(cache_key, payload, ttl=ttl)
    return payload


def _publish_candle_window(sig: dict) -> tuple[int, int]:
    """(start_ms, end_ms) of the candles that had closed when a signal was published (detected): the
    cross candle and the ones before it, plus any that closed before a late detection."""
    interval_ms = CANDLE_INTERVAL_MS.get(sig.get("timeframe") or "2h", CANDLE_INTERVAL_MS["2h"])
    cross_ms = int(sig["cross_candle_ms"])
    detected_ms = _parse_utc_ms(sig.get("detected_at")) or cross_ms + interval_ms
    last_open_ms = detected_ms - detected_ms % interval_ms - interval_ms
    last_open_ms = min(max(last_open_ms, cross_ms), cross_ms + SIGNAL_EXPIRY_CANDLES * interval_ms)
    return cross_ms - SIGNAL_CANDLES_BEFORE * interval_ms, last_open_ms


async def _publish_candles(sig: dict, signal_id: int, response: Response) -> dict:
    start_ms, end_ms = _publish_candle_window(sig)
    # Every candle in the window closed before the signal was published, so the window never changes.
    # A persisted window that reaches back far enough (strategies that read history to settle) is reused
    persisted = await get_signal_candle_window(signal_id) if sig.get("win_status", "PENDING") != "PENDING" else None
    if persisted and persisted.get("candles") and persisted["candles"][0]["time"] * 1000 <= start_ms:
        candles = [c for c in persisted["candles"] if start_ms <= c["time"] * 1000 <= end_ms and not c.get("partial")]
        payload = {"symbol": persisted["symbol"], "timeframe": persisted["timeframe"], "candles": candles}
    else:
        payload = await _single_flight(
            ("signal-candles", signal_id, "publish"),
            lambda: _load_signal_candles(
                sig, start_ms, end_ms, f"klines:signal:{signal_id}:publish", SIGNAL_CANDLES_FINAL_TTL
            ),
        )
    response.headers["Cache-Control"] = f"public, max-age={SIGNAL_CANDLES_FINAL_TTL}"
    return {**payload, "final": True, "view": "publish", "as_of": sig.get("detected_at")}


async def _context_before_window(sig: dict, signal_id: int, window: list[dict]) -> list[dict]:
    """Candles before a persisted window, for the chart only: a window that starts at the cross candle
    (rules that read no history to settle) gets the SIGNAL_CANDLES_BEFORE candles before it from the
    at-publish candles, every one closed before the signal was published. They are returned apart
    from `candles`, which stay exactly the hashed window. Empty when not needed or not available."""
    if not window:
        return []
    interval_ms = CANDLE_INTERVAL_MS.get(sig.get("timeframe") or "2h", CANDLE_INTERVAL_MS["2h"])
    first_ms = window[0]["time"] * 1000
    if first_ms <= int(sig["cross_candle_ms"]) - SIGNAL_CANDLES_BEFORE * interval_ms:
        return []
    start_ms, end_ms = _publish_candle_window(sig)
    try:
        payload = await _single_flight(
            ("signal-candles", signal_id, "publish"),
            lambda: _load_signal_candles(
                sig, start_ms, end_ms, f"klines:signal:{signal_id}:publish", SIGNAL_CANDLES_FINAL_TTL
            ),
        )
    except HTTPException:
        return []
    return [c for c in payload.get("candles") or [] if c["time"] * 1000 < first_ms and not c.get("partial")]


@app.get("/api/signals/{signal_id}/candles")
async def get_signal_candles(
    signal_id: int,
    response: Response,
    view: Annotated[
        Literal["after", "publish"],
        Query(description="after: the trade window (default); publish: the candles closed when it was published"),
    ] = "after",
):
    sig = await get_signal_by_id(signal_id)
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")
    if view == "publish":
        return await _publish_candles(sig, signal_id, response)

    # A closed signal is charted from the window the evaluator settled it on, stored at resolution
    # and hashed into its resolved event, so the chart can never change afterwards. Signals closed
    # before the window was stored, and open ones, are fetched as before
    if sig.get("win_status", "PENDING") != "PENDING":
        persisted = await get_signal_candle_window(signal_id)
        if persisted:
            context = await _context_before_window(sig, signal_id, persisted.get("candles") or [])
            response.headers["Cache-Control"] = f"public, max-age={SIGNAL_CANDLES_FINAL_TTL}"
            return {**persisted, "context": context, "final": True}

    start_ms, end_ms, final = _signal_candle_window(sig, int(time.time() * 1000))
    ttl = SIGNAL_CANDLES_FINAL_TTL if final else KLINES_CACHE_TTL
    cache_key = f"klines:signal:{signal_id}:{'final' if final else 'live'}"
    payload = await _single_flight(
        ("signal-candles", signal_id, final),
        lambda: _load_signal_candles(sig, start_ms, end_ms, cache_key, ttl),
    )
    response.headers["Cache-Control"] = f"public, max-age={ttl}"
    return {**payload, "final": final}


# Signal ledger: append-only, hash-chained events (src/ledger), public so anyone can check the record
def _ledger_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="Ledger unavailable: no database")


@app.get("/api/signals/{signal_id}/events")
async def list_signal_events(signal_id: int):
    """Everything the ledger recorded about one signal, oldest first, with each event's chain hashes."""
    try:
        events = await get_signal_events(signal_id)
    except Exception as exc:
        logger.exception("Failed to read events of signal %s", signal_id)
        raise HTTPException(status_code=500, detail="Failed to read the ledger") from exc
    if events is None:
        if not await is_db_connected():
            raise _ledger_unavailable()
        raise HTTPException(status_code=404, detail="Signal not found")
    return {"signal_id": signal_id, "events": events}


@app.get("/api/ledger/head")
async def ledger_head(response: Response):
    """The chain's latest event hash, and the heads published once per UTC day. A head copied today can
    be checked against any later export (python -m src.ledger verify --jsonl FILE --head HASH)."""
    try:
        head = await get_ledger_head()
    except Exception as exc:
        logger.exception("Failed to read the ledger head")
        raise HTTPException(status_code=500, detail="Failed to read the ledger") from exc
    if head is None:
        raise _ledger_unavailable()
    response.headers["Cache-Control"] = "no-store"
    return head


@app.get("/api/ledger/verify")
async def ledger_verify():
    """Recompute the whole chain on the server: ok, and the first event that fails when one does.
    Also checks the published heads, the persisted candle windows and that every signal's live row
    matches its latest event. Reused for a few minutes while the head does not move."""
    try:
        result = await verify_ledger()
    except Exception as exc:
        logger.exception("Ledger verification failed to run")
        raise HTTPException(status_code=500, detail="Failed to verify the ledger") from exc
    if result is None:
        raise _ledger_unavailable()
    return result


StrategyKeyQuery = Annotated[
    str | None, Query(max_length=40, pattern=r"^[a-z0-9_]+$", description="Strategy key, e.g. macd_cross")
]
StrategyVersionQuery = Annotated[
    str | None, Query(max_length=20, pattern=r"^[A-Za-z0-9_.-]+$", description="Strategy version, e.g. 2.2.0")
]


@app.get("/api/ledger.csv")
async def ledger_csv(strategy_key: StrategyKeyQuery = None, strategy_version: StrategyVersionQuery = None):
    """Every stored signal, eligible or not (with the flags saying which), its trade's R columns and
    its latest event hash; optionally only one strategy (and version)."""
    rows = await export_rows()
    if rows is None:
        raise _ledger_unavailable()
    rows = filter_rows(rows, strategy_key, strategy_version)
    name = "-".join(p for p in ("crossline-ledger", strategy_key, strategy_version) if p)
    return PlainTextResponse(
        rows_to_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
    )


@app.get("/api/ledger")
async def ledger_rows(strategy_key: StrategyKeyQuery = None, strategy_version: StrategyVersionQuery = None):
    """The ledger rows of /api/ledger.csv as JSON, for the track-record table."""
    rows = await export_rows()
    if rows is None:
        raise _ledger_unavailable()
    rows = filter_rows(rows, strategy_key, strategy_version)
    return {"total": len(rows), "columns": list(LEDGER_COLUMNS), "rows": rows}


@app.get("/api/ledger/events")
async def ledger_recent_events(limit: Annotated[int, Query(ge=1, le=50)] = 10):
    """The newest events of the chain, newest first, with each one's signal and hashes."""
    try:
        events = await get_recent_events(limit)
    except Exception as exc:
        logger.exception("Failed to read the newest ledger events")
        raise HTTPException(status_code=500, detail="Failed to read the ledger") from exc
    if events is None:
        raise _ledger_unavailable()
    return {"events": events}


@app.get("/api/ledger.jsonl")
async def ledger_jsonl():
    """The ledger rows plus every event and daily head, for offline verification with
    python -m src.ledger verify --jsonl."""
    body = await export_jsonl()
    if body is None:
        raise _ledger_unavailable()
    return PlainTextResponse(
        body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="crossline-ledger.jsonl"'},
    )


# Cron Scheduler Management Endpoints
@app.get("/api/cron/status")
async def get_cron_scheduler_status(request: Request):
    return cron_scheduler.get_status(include_error=is_authenticated(request))


@app.get("/api/cron/runs")
async def list_cron_runs(request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50):
    """The newest scheduled runs from cron_runs, the last success and the on-schedule ratio over the
    last 7 days. Error text is only included for the signed-in owner."""
    try:
        data = await get_cron_runs(limit=limit, include_error=is_authenticated(request))
    except Exception as exc:
        logger.exception("Failed to read cron runs")
        raise HTTPException(status_code=500, detail="Failed to read cron runs") from exc
    if data is None:
        raise HTTPException(status_code=503, detail="Cron runs unavailable: no database")
    status = cron_scheduler.get_status()
    return {**data, "enabled": status["enabled"], "next_run": status["next_run"], "schedule_text": status["schedule"]}


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
    if await notify_closed_trades(closed):
        await record_alert_events([t.get("signal_id") for t in closed], "outcome", format_outcome_alert(closed))
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
    start_date: Annotated[str | None, Query(description="First UTC day, YYYY-MM-DD (inclusive)")] = None,
    end_date: Annotated[str | None, Query(description="Last UTC day, YYYY-MM-DD (inclusive: the range ends at the next midnight UTC)")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page")] = 50,
    offset: Annotated[int, Query(ge=0, description="Page offset")] = 0,
):
    target_dir = direction or side
    try:
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
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return ProfitLossHistoryResponse(
        total=total,
        limit=limit,
        offset=offset,
        summary=summary,
        history=records,
    )


@app.get("/api/profitloss-history/adjustments", dependencies=[Depends(verify_api_key)])
async def list_profit_loss_adjustments(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    items, total = await get_profitloss_adjustments(limit=limit, offset=offset)
    return {"total": total, "limit": limit, "offset": offset, "adjustments": items}


@app.post("/api/profitloss-history", dependencies=[Depends(verify_api_key)])
async def add_profit_loss_record(body: ProfitLossRecordRequest):
    """Record a hand-entered trade, kept for ops in profitloss_adjustments: never in the track record,
    the ledger or GET /api/profitloss-history (list them at /api/profitloss-history/adjustments). It
    cannot point at a stored signal (409), whose outcome only the evaluator books."""
    try:
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
    except SignalTradeConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    if not item:
        raise HTTPException(status_code=400, detail="Failed to record profit/loss item")
    return {"status": "success", "item": item}


def _stamp_input_candles(sig, symbol: str, timeframe: str, klines) -> None:
    """Fingerprint the closed candles an ad-hoc scan detected `sig` on, for its detected event."""
    try:
        ctx = MarketContext.from_klines(symbol, int(time.time() * 1000), timeframe, {timeframe: klines})
        sig.input_candles_sha256, sig.input_candles = context_fingerprint(ctx)
    except Exception:
        logger.warning("Could not fingerprint the candles of the %s %s scan", symbol, timeframe)


# Scanner Endpoints (Strict 2h timeframe). A manual scan is a preview by default: it neither stores
# nor alerts unless asked, and what it stores is labelled "manual" and kept out of the track record
@app.post("/api/scan", response_model=ScanResponse, dependencies=[Depends(verify_api_key)])
@app.post("/api/macd-scan", response_model=ScanResponse, dependencies=[Depends(verify_api_key)])
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
    ] = False,
    save_db: Annotated[
        bool, Query(description="Save to PostgreSQL as manual signals, excluded from the track record")
    ] = False,
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
            klines = await asyncio.to_thread(fetch_klines, client, sym, timeframe, limit)
            if klines is None:
                failed_symbols.append(sym)
                return None
            df = compute_macd(klines, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
            if df is None:
                return None
            sig = detect_macd_cross(
                df, sym, lookback_candles=lookback, include_current=include_current
            )
            if sig is not None and save_db:
                _stamp_input_candles(sig, sym, timeframe, klines)
            return sig

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
            saved = await save_macd_signal(sig, timeframe=timeframe, source="manual")
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
                entry_basis=sig.entry_basis,
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
            item.entry_basis = record.get("entry_basis") or item.entry_basis
            item.source = record.get("source")
            item.is_eligible = bool(record.get("is_eligible"))

    # Only a scan that stored something has anything new to evaluate
    if save_db:
        background_tasks.add_task(_evaluate_and_notify, client)

    if send_telegram and telegram_parts:
        header = "<b>Crossline: manual 2H scan</b>\n\n"
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
    request: Request,
    symbol: Annotated[str | None, Query(description="Filter by symbol")] = None,
    side: Annotated[str | None, Query(description="Filter by side (LONG/SHORT)")] = None,
    direction: Annotated[str | None, Query(description="Alias for side")] = None,
    timeframe: Annotated[str | None, Query(description="Filter by timeframe")] = "2h",
    win_status: Annotated[str | None, Query(description="Filter by win_status (TP1_HIT, SL_HIT, PENDING)")] = None,
    result: Annotated[str | None, Query(description="Filter by result (PROFIT, LOSS, PENDING)")] = None,
    date: Annotated[str | None, Query(description="Filter by date (YYYY-MM-DD)")] = None,
    start_date: Annotated[str | None, Query(description="First UTC day, YYYY-MM-DD (inclusive)")] = None,
    end_date: Annotated[str | None, Query(description="Last UTC day, YYYY-MM-DD (inclusive: the range ends at the next midnight UTC)")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page")] = 50,
    offset: Annotated[int, Query(ge=0, description="Page offset")] = 0,
    tz_offset_minutes: Annotated[
        int,
        Query(
            ge=-720,
            le=840,
            description="Read date, start_date and end_date as local days this many minutes east of UTC "
            "(420 for WIB); 0 means UTC days",
        ),
    ] = 0,
    strategy_key: StrategyKeyQuery = None,
    strategy_version: StrategyVersionQuery = None,
    source: Annotated[
        Literal["cron", "catchup", "manual", "legacy"] | None, Query(description="Filter by where the signal came from")
    ] = None,
):
    target_side = direction or side
    try:
        records, total, summary = await get_macd_signals(
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
            tz_offset_minutes=tz_offset_minutes,
            strategy_key=strategy_key,
            strategy_version=strategy_version,
            source=source,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    if not is_authenticated(request):
        for record in records:
            record.pop("journal_status", None)
    return SignalsHistoryResponse(
        total=total,
        limit=limit,
        offset=offset,
        summary=summary,
        signals=records,
    )


@app.get("/api/summary", response_model=SummaryResponse)
async def signals_summary():
    data = await get_macd_signals_summary()
    return SummaryResponse(**data)


@app.get("/api/performance", response_model=PerformanceResponse)
async def performance(
    strategy_version: Annotated[
        str | None, Query(max_length=20, description="Strategy version; defaults to the version running now")
    ] = None,
    symbol: Annotated[str | None, Query(max_length=20, description="Filter by symbol")] = None,
    side: Annotated[Literal["LONG", "SHORT", "long", "short"] | None, Query(description="Filter by side")] = None,
    strategy_key: StrategyKeyQuery = None,
):
    """Track record of one strategy version: eligible closed trades only, each figure with its sample
    size and a 95% interval where one applies. `definitions` in the payload states every formula."""
    kwargs = {"strategy_key": strategy_key} if strategy_key else {}
    return await get_performance(strategy_version=strategy_version, symbol=symbol, side=side, **kwargs)


@app.get("/api/track-record")
async def track_record(strategy_key: StrategyKeyQuery = None, strategy_version: StrategyVersionQuery = None):
    """Everything the track-record page draws for one strategy version: the KPIs of /api/performance,
    the equity curve and drawdown in R (one point per exit instant), the net R histogram and breakdowns
    per side, symbol and exit month. Breakdown rows under min_breakdown_sample trades are flagged
    meaningful=false."""
    return await get_track_record(strategy_key=strategy_key, strategy_version=strategy_version)
