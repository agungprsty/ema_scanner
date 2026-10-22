import asyncio
import hashlib
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import pandas as pd
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from src.api import setup_http
from src.api.exceptions import (
    AppException,
    BadGateway,
    BadRequest,
    Conflict,
    NotFound,
    ServiceUnavailable,
    Unauthorized,
    UnprocessableEntity,
)
from src.api.log import configure_logging
from src.api.passwords import DUMMY_HASH, hash_password, verify_password
from src.api.responses import Envelope, PageEnvelope, paginated, success
from src.api.security import (
    API_KEY_MIN_LENGTH,
    Principal,
    api_key_configured,
    check_same_origin,
    get_principal,
    require_admin,
)
from src.config.settings import (
    DRY_RUN,
    FILL_WINDOW_CANDLES,
    INTERVAL_MS,
    MAX_HOLD_CANDLES,
    MAX_SYMBOLS,
    ROUND_TRIP_FEE_PERCENT,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_PERIOD,
    SESSION_COOKIE_SECURE,
    SESSION_TTL_HOURS,
    SL_PERCENT,
    TIMEFRAME,
    TIMEFRAME_MS,
    TP_PERCENT,
)
from src.data_feed.binance_client import create_futures_client
from src.data_feed.ohlcv import fetch_candles_range
from src.services.alerts import notify_signal_edit
from src.services.cron_scheduler import cron_scheduler
from src.services.db import (
    ACTIVE_STATUSES,
    add_watchlist_symbol,
    cleanup_expired_sessions,
    close_db,
    create_session,
    create_user,
    delete_journal_entry,
    delete_session,
    delete_watchlist_symbol,
    get_journal,
    get_journal_summary,
    get_session_user,
    get_signal,
    get_user_by_email,
    get_watchlist,
    init_db,
    is_db_connected,
    list_signals,
    signal_conditions,
    signal_stats,
    toggle_watchlist_symbol,
    update_signal_levels,
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

configure_logging()
logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = create_futures_client()
    return _client


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not api_key_configured():
        logger.warning(
            "API_SECRET_KEY is unset, a placeholder, or shorter than %d characters; "
            "machine access via Bearer is disabled",
            API_KEY_MIN_LENGTH,
        )
    await init_db()
    await init_redis()
    cron_scheduler.start(_get_client)
    yield
    cron_scheduler.stop()
    await close_redis()
    await close_db()


APP_VERSION = "3.1.0"

app = FastAPI(
    title="Simpletrade",
    description="Binance Futures RSI 28/72 limit-entry scanner with PostgreSQL, Redis and a per-candle scheduler",
    version=APP_VERSION,
    lifespan=lifespan,
)

setup_http(app)

app.mount("/static", StaticFiles(directory="src/static"), name="static")


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="st_session",
        value=token,
        max_age=SESSION_TTL_HOURS * 3600,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key="st_session",
        path="/",
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )


EMAIL_REGEX = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
USERNAME_REGEX = r"^[A-Za-z0-9_]{3,32}$"


def _trim_lower(v):
    return v.strip().lower() if isinstance(v, str) else v


class LoginRequest(BaseModel):
    email: str = Field(..., pattern=EMAIL_REGEX, max_length=254)
    password: str = Field(..., min_length=1, max_length=128)

    # mode="before" so the pattern and length checks see the trimmed value
    _clean_email = field_validator("email", mode="before")(_trim_lower)


class RegisterRequest(BaseModel):
    # Field order matters: the password check reads the already validated email and username
    email: str = Field(..., pattern=EMAIL_REGEX, max_length=254)
    username: str = Field(..., pattern=USERNAME_REGEX)
    password: str = Field(..., min_length=10, max_length=128)

    _clean = field_validator("email", "username", mode="before")(_trim_lower)

    @field_validator("password")
    @classmethod
    def _password_not_identity(cls, v: str, info: ValidationInfo) -> str:
        if v.lower() in (info.data.get("email"), info.data.get("username")):
            raise ValueError("Password cannot match email or username")
        return v


# Authentication Endpoints
@app.post(
    "/api/auth/register",
    status_code=201,
    dependencies=[Depends(check_same_origin)],
    response_model=Envelope[dict],
)
async def register_endpoint(body: RegisterRequest):
    password_hash = await hash_password(body.password)
    user = await create_user(
        email=body.email,
        username=body.username,
        password_hash=password_hash,
        role="user",
    )
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(hours=SESSION_TTL_HOURS)
    await create_session(token_hash, user["id"], expires_at)
    res = success(user, "User registered successfully", code=201)
    _set_session_cookie(res, token)
    return res


@app.post(
    "/api/auth/login",
    dependencies=[Depends(check_same_origin)],
    response_model=Envelope[dict],
)
async def login_endpoint(body: LoginRequest):
    user = await get_user_by_email(body.email)
    stored_hash = user["password_hash"] if user else DUMMY_HASH
    is_valid = await verify_password(body.password, stored_hash)
    if not user or not is_valid:
        raise Unauthorized("Invalid email or password")

    await cleanup_expired_sessions()

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(hours=SESSION_TTL_HOURS)
    await create_session(token_hash, user["id"], expires_at)

    user_data = {
        "id": user["id"],
        "email": user["email"],
        "username": user["username"],
        "role": user["role"],
        "created_at": user["created_at"],
    }
    res = success(user_data, "Logged in successfully")
    _set_session_cookie(res, token)
    return res


@app.post(
    "/api/auth/logout",
    dependencies=[Depends(check_same_origin)],
    response_model=Envelope[None],
)
async def logout_endpoint(request: Request):
    token = request.cookies.get("st_session")
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        await delete_session(token_hash)
    res = success(None, "Logged out successfully")
    _clear_session_cookie(res)
    return res


@app.get("/api/auth/me", response_model=Envelope[dict])
async def get_current_user_profile(
    principal: Annotated[Principal, Depends(get_principal)],
):
    return success(principal.to_dict())


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


class SignalLevelsRequest(BaseModel):
    side: Literal["LONG", "SHORT"]
    # Infinity would be stored, then fail every JSON response that holds the row
    entry_price: float = Field(gt=0, allow_inf_nan=False)
    take_profit: float = Field(gt=0, allow_inf_nan=False)
    stop_loss: float = Field(gt=0, allow_inf_nan=False)


def level_errors(body: SignalLevelsRequest, filled: bool) -> list[dict]:
    """Before the fill the target must sit on the profit side of the entry and the stop on the
    loss side. Once filled the stop may move past the entry (break-even, trailing), so it only has
    to stay on the loss side of the target."""
    long = body.side == "LONG"
    above, below = ("above", "below") if long else ("below", "above")
    if filled:
        if (body.stop_loss >= body.take_profit) if long else (body.stop_loss <= body.take_profit):
            return [{"field": "stop_loss", "message": f"must be {below} the target for a {body.side}"}]
        return []
    errors = []
    if (body.take_profit <= body.entry_price) if long else (body.take_profit >= body.entry_price):
        errors.append({"field": "take_profit", "message": f"must be {above} the entry for a {body.side}"})
    if (body.stop_loss >= body.entry_price) if long else (body.stop_loss <= body.entry_price):
        errors.append({"field": "stop_loss", "message": f"must be {below} the entry for a {body.side}"})
    return errors


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


async def _page(
    request: Request,
    filepath: str,
    *,
    require_session: bool = False,
    redirect_if_authenticated: bool = False,
) -> Response:
    user = None
    token = request.cookies.get("st_session")
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        try:
            user = await get_session_user(token_hash)
        except Exception:
            # Database down: public pages still render, /dashboard falls back to /login
            logger.warning("Session lookup failed while serving %s", request.url.path, exc_info=True)
            user = None

    if require_session:
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        return FileResponse(filepath, headers={"Cache-Control": "no-store"})

    if redirect_if_authenticated and user:
        return RedirectResponse(url="/dashboard", status_code=303)

    return FileResponse(filepath)


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def root(request: Request):
    return await _page(request, "src/pages/landing.html")


@app.api_route("/login", methods=["GET", "HEAD"], include_in_schema=False)
async def login_page(request: Request):
    return await _page(request, "src/pages/login.html", redirect_if_authenticated=True)


@app.api_route("/register", methods=["GET", "HEAD"], include_in_schema=False)
async def register_page(request: Request):
    return await _page(request, "src/pages/register.html", redirect_if_authenticated=True)


@app.api_route("/dashboard", methods=["GET", "HEAD"], include_in_schema=False)
async def dashboard_page(request: Request):
    return await _page(request, "src/pages/dashboard.html", require_session=True)


# The page loads the signal itself, so an unknown id shows its message there
@app.api_route("/signals/{signal_id:int}", methods=["GET", "HEAD"], include_in_schema=False)
async def signal_page(request: Request, signal_id: int):
    return await _page(request, "src/pages/signal.html", require_session=True)


@app.get("/api/info", response_model=Envelope[dict])
async def get_info():
    return success({
        "status": "running",
        "app": "Simpletrade",
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
    })


# Always 200, even when degraded: the deploy gate greps the body for "status":"healthy"
@app.get("/api/health", response_model=Envelope[HealthResponse])
async def health():
    db_ok = await is_db_connected()
    redis_ok = await is_redis_connected()
    cron_info = cron_scheduler.get_status()
    status_str = "healthy" if (db_ok and redis_ok) else "degraded"
    return success(HealthResponse(status=status_str, database=db_ok, redis=redis_ok, cron=cron_info))


# Watchlist Endpoints
@app.get("/api/watchlist", dependencies=[Depends(get_principal)], response_model=Envelope[list[dict]])
async def list_watchlist(active_only: bool = False):
    return success(await get_watchlist(active_only=active_only))


@app.post("/api/watchlist", dependencies=[Depends(require_admin)], response_model=Envelope[dict])
async def add_symbol_to_watchlist(body: WatchlistAddRequest):
    item = await add_watchlist_symbol(symbol=body.symbol, notes=body.notes)
    if not item:
        raise BadRequest("Failed to add symbol to watchlist")
    return success(item, "Symbol added to watchlist")


@app.delete("/api/watchlist/{symbol}", dependencies=[Depends(require_admin)], response_model=Envelope[dict])
async def remove_symbol_from_watchlist(symbol: str):
    ok = await delete_watchlist_symbol(symbol=symbol)
    if not ok:
        raise NotFound("Symbol not found in watchlist")
    return success({"symbol": symbol}, "Symbol removed from watchlist")


@app.patch("/api/watchlist/{symbol}/toggle", dependencies=[Depends(require_admin)], response_model=Envelope[dict])
async def toggle_symbol_status(symbol: str, body: WatchlistToggleRequest):
    ok = await toggle_watchlist_symbol(symbol=symbol, is_active=body.is_active)
    if not ok:
        raise NotFound("Symbol not found in watchlist")
    return success({"symbol": symbol, "is_active": body.is_active}, "Symbol updated")


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
@app.get("/api/signals/{signal_id}", response_model=Envelope[dict])
async def get_single_signal(
    signal_id: int,
    principal: Annotated[Principal, Depends(get_principal)],
):
    sig = await get_signal(signal_id)
    if not sig:
        raise NotFound("Signal not found")
    if principal.role != "administrator":
        sig.pop("journal", None)
    return success(sig)


# Upper bound keeps (page - 1) * per_page inside Postgres' bigint OFFSET
Page = Annotated[int, Query(ge=1, le=1_000_000, description="Page number, from 1")]
PerPage = Annotated[int, Query(ge=1, le=200, description="Items per page")]


# Trade Journal Endpoints
@app.get("/api/journal", dependencies=[Depends(require_admin)], response_model=PageEnvelope[dict])
async def list_journal(
    status: Annotated[Literal["TAKEN", "SKIPPED"] | None, Query(description="TAKEN or SKIPPED")] = None,
    symbol: Annotated[str | None, Query(max_length=30, description="Filter by symbol")] = None,
    page: Page = 1,
    per_page: PerPage = 50,
):
    items, total = await get_journal(status=status, symbol=symbol, limit=per_page, offset=(page - 1) * per_page)
    return paginated(items, total, page, per_page)


@app.get("/api/journal/summary", dependencies=[Depends(require_admin)], response_model=Envelope[dict])
async def journal_summary_endpoint():
    """Summary across every journal entry, unfiltered, so the comparison is stable while browsing."""
    return success(await get_journal_summary())


@app.put("/api/signals/{signal_id}/journal", dependencies=[Depends(require_admin)], response_model=Envelope[dict])
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
        raise AppException("Failed to save journal entry", code=500) from exc
    if item is None:
        raise NotFound("Signal not found")
    return success(item, "Journal entry saved")


@app.put("/api/signals/{signal_id}/levels", dependencies=[Depends(require_admin)], response_model=Envelope[dict])
async def edit_signal_levels(signal_id: int, body: SignalLevelsRequest):
    """Move the levels of a pending or open signal (only target and stop once filled); Telegram
    gets the new levels."""
    sig = await get_signal(signal_id)
    if not sig:
        raise NotFound("Signal not found")
    errors = level_errors(body, filled=sig["status"] == "OPEN")
    if errors:
        raise UnprocessableEntity("Validation failed", errors=errors)
    try:
        result = await update_signal_levels(
            _get_client(), signal_id, body.side, body.entry_price, body.take_profit, body.stop_loss,
            int(time.time() * 1000),
        )
    except AppException:
        raise
    except Exception as exc:
        logger.exception("Signal %d edit could not check the latest candles", signal_id)
        raise BadGateway("Could not check the order against the latest candles, try again") from exc
    if result is None:
        raise NotFound("Signal not found")
    row, alerted = result
    if alerted:
        await notify_signal_edit(row)
    return success(await get_signal(signal_id) or row, "Signal levels saved")


@app.delete("/api/signals/{signal_id}/journal", dependencies=[Depends(require_admin)], response_model=Envelope[None])
async def remove_journal_entry(signal_id: int):
    if not await delete_journal_entry(signal_id):
        raise NotFound("No journal entry for this signal")
    return success(message="Journal entry deleted")


# Candles framing one signal's trade, with RSI, for its chart
SIGNAL_CANDLES_BEFORE = 30
SIGNAL_CANDLES_AFTER = 6
SIGNAL_RSI_WARMUP = 300  # extra bars fetched before the window so the first charted RSI and EMA have settled
SIGNAL_CHART_EMA = 50
# A closed trade's window never changes once all its candles have closed
SIGNAL_CANDLES_FINAL_TTL = 7 * 24 * 3600


def _signal_candle_window(sig: dict, now_ms: int) -> tuple[int, int, bool]:
    """(start_ms, end_ms, final) of the candles to chart for a signal.

    Active trades run from before the signal to now. Closed ones stop a few candles after the
    close, and are final once every candle in that range has closed.
    """
    interval_ms = INTERVAL_MS.get(sig.get("timeframe"))
    if interval_ms is None:
        raise AppException("Signal has an unsupported timeframe", code=500)
    start_ms = int(sig["signal_candle_ms"]) - SIGNAL_CANDLES_BEFORE * interval_ms
    closed_ms = sig.get("closed_ms")
    if sig.get("status") in ACTIVE_STATUSES or closed_ms is None:
        return start_ms, now_ms, False
    last_open_ms = closed_ms - closed_ms % interval_ms + SIGNAL_CANDLES_AFTER * interval_ms
    final = last_open_ms + interval_ms <= now_ms
    return start_ms, min(last_open_ms, now_ms), final


def _chart_payload(sig: dict, candles: list[dict], start_ms: int) -> dict:
    """Candles from `start_ms` on, with RSI and EMA over the whole fetch so warm-up bars are dropped, not zeroed."""
    close = pd.Series([c["close"] for c in candles], dtype=float)
    rsi = compute_rsi(close)
    ema = close.ewm(span=SIGNAL_CHART_EMA, min_periods=SIGNAL_CHART_EMA, adjust=False).mean()
    start_s = start_ms // 1000
    shown = [i for i, c in enumerate(candles) if c["time"] >= start_s]

    def series(values, digits):
        return [
            {"time": candles[i]["time"], "value": round(float(values[i]), digits)} for i in shown if pd.notna(values[i])
        ]

    return {
        "symbol": sig["symbol"],
        "timeframe": sig["timeframe"],
        "candles": [candles[i] for i in shown],
        "rsi": series(rsi, 2),
        "ema": series(ema, 8),
        # Taker buy minus taker sell volume per candle, in the base asset
        "volume_delta": [
            {"time": candles[i]["time"], "value": round(2 * candles[i]["buy_volume"] - candles[i]["volume"], 8)}
            for i in shown
        ],
    }


async def _load_signal_chart(sig: dict, start_ms: int, end_ms: int, cache_key: str, ttl: int) -> dict:
    miss_key = f"{cache_key}:miss"
    cached = await get_cached_json(cache_key)
    if cached:
        return cached
    if await get_cached_json(miss_key):
        raise BadGateway("Failed to load candle data from upstream")
    if not _consume_klines_budget():
        raise ServiceUnavailable("Chart data temporarily unavailable, retry later", headers={"Retry-After": str(KLINES_BUDGET_WINDOW)})

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
        raise BadGateway("Failed to load candle data from upstream")

    payload = _chart_payload(sig, candles, start_ms)
    await set_cached_json(cache_key, payload, ttl=ttl)
    return payload


@app.get("/api/signals/{signal_id}/chart", dependencies=[Depends(get_principal)], response_model=Envelope[dict])
async def get_signal_chart(signal_id: int):
    sig = await get_signal(signal_id)
    if not sig:
        raise NotFound("Signal not found")

    start_ms, end_ms, final = _signal_candle_window(sig, int(time.time() * 1000))
    ttl = SIGNAL_CANDLES_FINAL_TTL if final else KLINES_CACHE_TTL
    # v2 added ema and volume_delta; the version keeps week-old final payloads without them from being served
    cache_key = f"chart:v2:{signal_id}:{'final' if final else 'live'}"
    payload = await _single_flight(
        ("signal-chart", signal_id, final),
        lambda: _load_signal_chart(sig, start_ms, end_ms, cache_key, ttl),
    )
    # Only a closed window is immutable; a live one must refetch to show new candles
    headers = {"Cache-Control": f"public, max-age={ttl}"} if final else None
    return success({**payload, "final": final}, headers=headers)


# Cron Scheduler Management Endpoints
@app.get("/api/cron/status", dependencies=[Depends(get_principal)], response_model=Envelope[dict])
async def get_cron_scheduler_status():
    return success(cron_scheduler.get_status())


@app.post("/api/cron/trigger", dependencies=[Depends(require_admin)], response_model=Envelope[dict])
async def trigger_cron_execution():
    res = await cron_scheduler.trigger_now()
    if res.get("status") in ("skipped", "busy"):
        raise Conflict(res.get("message", "Cron job is busy"))
    if res.get("status") == "error":
        raise ServiceUnavailable("Scan failed, see server logs")
    summary = await signal_stats()
    return success({"result": res, "summary": summary}, "Scan finished")


def signal_filters(
    status: Annotated[str | None, Query(max_length=100, description="Comma-separated statuses, e.g. PENDING,OPEN")] = None,
    side: Annotated[str | None, Query(max_length=5, description="Filter by side (LONG/SHORT)")] = None,
    symbol: Annotated[str | None, Query(max_length=30, description="Filter by symbol")] = None,
    timeframe: Annotated[str | None, Query(max_length=4, description="Filter by timeframe")] = None,
    date: Annotated[str | None, Query(description="WIB day (YYYY-MM-DD)")] = None,
    start_date: Annotated[str | None, Query(description="First WIB day (YYYY-MM-DD)")] = None,
    end_date: Annotated[str | None, Query(description="Last WIB day, inclusive (YYYY-MM-DD)")] = None,
) -> dict:
    """Signal filters shared by the list and its summary, so both describe the same signals."""
    filters = {
        "status": status,
        "side": side,
        "symbol": symbol,
        "timeframe": timeframe,
        "date": date,
        "start_date": start_date,
        "end_date": end_date,
    }
    try:
        signal_conditions(**filters)
    except ValueError as exc:
        raise UnprocessableEntity("Dates must be YYYY-MM-DD") from exc
    return filters


SignalFilters = Annotated[dict, Depends(signal_filters)]


@app.get("/api/signals", response_model=PageEnvelope[dict])
async def list_signals_endpoint(
    filters: SignalFilters,
    principal: Annotated[Principal, Depends(get_principal)],
    page: Page = 1,
    per_page: PerPage = 50,
):
    records, total, _ = await list_signals(**filters, limit=per_page, offset=(page - 1) * per_page)
    if principal.role != "administrator":
        for r in records:
            r.pop("journal_status", None)
    return paginated(records, total, page, per_page)


@app.get("/api/summary", dependencies=[Depends(get_principal)], response_model=Envelope[SummaryResponse])
async def signals_summary(filters: SignalFilters):
    return success(SummaryResponse(**await signal_stats(signal_conditions(**filters))))
