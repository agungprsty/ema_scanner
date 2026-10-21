import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from src.config.settings import (
    CRON_ENABLED,
    CRON_LOOKBACK_CANDLES,
    CRON_SAFETY_DELAY_SECONDS,
    MAX_SYMBOLS,
    SCAN_CANDLES,
    SCAN_CONCURRENCY,
    TIMEFRAME,
    TIMEFRAME_MS,
)
from src.data_feed.ohlcv import fetch_klines
from src.services.alerts import notify_run
from src.services.db import (
    claim_unalerted_signals,
    evaluate_active,
    get_watchlist,
    insert_signals,
    is_db_connected,
    release_signal_alerts,
)
from src.services.redis_client import acquire_lock, release_lock
from src.strategy.rsi import detect_signals

logger = logging.getLogger(__name__)


WIB_TZ = timezone(timedelta(hours=7))
MANUAL_LOCK_SECONDS = 300


def now_ms() -> int:
    return int(time.time() * 1000)


def _iso_wib(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, WIB_TZ).isoformat()


def next_run_ms(after_ms: int, interval_ms: int, delay_ms: int) -> int:
    """First candle boundary + delay strictly after `after_ms`; a start inside [B, B+delay) still gets B."""
    return ((after_ms - delay_ms) // interval_ms + 1) * interval_ms + delay_ms


async def _alert(transitions: list[dict], run_ms: int) -> None:
    """Alert every stored signal no run has alerted yet, within the scan lookback; an
    undelivered alert is released so the next run tries again."""
    rows = await claim_unalerted_signals(run_ms - (CRON_LOOKBACK_CANDLES + 1) * TIMEFRAME_MS)
    if not await notify_run(rows, transitions, run_ms):
        await release_signal_alerts([r["id"] for r in rows])


async def run_job(client) -> dict:
    """Scan the watchlist's closed candles for RSI crosses, store them, replay active trades, alert.

    Raises when the database is not ready, so the run is marked failed instead of finding nothing.
    """
    if not await is_db_connected():
        raise RuntimeError("Database not ready")

    start_time = time.perf_counter()
    run_ms = now_ms()
    symbols = [item["symbol"] for item in await get_watchlist(active_only=True)][:MAX_SYMBOLS]
    if not symbols:
        logger.warning("[CRON] Watchlist is empty: no symbols to scan")

    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def _scan(sym: str):
        async with sem:
            df = await asyncio.to_thread(fetch_klines, client, sym, TIMEFRAME, SCAN_CANDLES)
        if df is None:
            return None
        # Closed candles only, by time: the limit entry is the candle's low/high, unknown while it forms
        return detect_signals(df[df["close_time"] < run_ms], sym, CRON_LOOKBACK_CANDLES)

    results = await asyncio.gather(*(_scan(s) for s in symbols))
    failed_symbols = [s for s, r in zip(symbols, results, strict=True) if r is None]
    hits = [sig for r in results if r for sig in r]

    new_rows = await insert_signals(hits)
    try:
        evaluated = await evaluate_active(client)
    except Exception:
        # Stored signals are never found as new again, so alert them before failing the run
        await _alert([], run_ms)
        raise
    await _alert(evaluated["transitions"], run_ms)

    logger.info(
        "[CRON] %s run finished: %d hits, %d new, %d trades evaluated (%d failed), %d symbols failed",
        TIMEFRAME,
        len(hits),
        len(new_rows),
        evaluated["evaluated"],
        evaluated["failed"],
        len(failed_symbols),
    )
    return {
        "new_signals": len(new_rows),
        "evaluated": evaluated["evaluated"],
        "filled": evaluated["filled"],
        "tp": evaluated["tp"],
        "sl": evaluated["sl"],
        "expired": evaluated["expired"],
        "not_filled": evaluated["not_filled"],
        "evaluate_failed": evaluated["failed"],
        "symbols_scanned": len(symbols),
        "failed_symbols": failed_symbols,
        "execution_time": f"{round(time.perf_counter() - start_time, 2)}s",
    }


class Scheduler:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._is_active: bool = False
        self._client_getter: Callable | None = None
        self._last_run: str | None = None
        self._next_run: str | None = None
        self._total_runs: int = 0
        self._last_status: str | None = None
        self._last_result: dict | None = None
        self._last_error: str | None = None
        self._lock = asyncio.Lock()

    def start(self, client_getter: Callable) -> None:
        # Set before the CRON_ENABLED check so "Run now" still works with the schedule off
        self._client_getter = client_getter
        if not CRON_ENABLED:
            logger.info("CRON scheduler is disabled via CRON_ENABLED=false")
            return

        if self._task is not None and not self._task.done():
            return

        self._is_active = True
        self._task = asyncio.create_task(self._cron_loop())
        logger.info("Scheduler started: every %s candle close + %ds", TIMEFRAME, CRON_SAFETY_DELAY_SECONDS)

    def stop(self) -> None:
        self._is_active = False
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("Scheduler stopped")

    def get_status(self) -> dict:
        return {
            "enabled": CRON_ENABLED,
            "is_active": self._is_active,
            "schedule": f"every {TIMEFRAME} candle close + {CRON_SAFETY_DELAY_SECONDS}s",
            "timezone": "WIB (UTC+7)",
            "timeframe": TIMEFRAME,
            "interval_ms": TIMEFRAME_MS,
            "last_run": self._last_run,
            "next_run": self._next_run,
            "total_runs": self._total_runs,
            "last_status": self._last_status,
            "last_result": self._last_result,
            "last_error": self._last_error,
        }

    async def trigger_now(self) -> dict:
        return await self._execute_job(f"job:{TIMEFRAME}:manual", MANUAL_LOCK_SECONDS, is_manual=True)

    async def _cron_loop(self) -> None:
        delay_ms = CRON_SAFETY_DELAY_SECONDS * 1000
        last = 0
        while self._is_active:
            try:
                now = now_ms()
                target = next_run_ms(max(now, last), TIMEFRAME_MS, delay_ms)
                skipped = (target - last) // TIMEFRAME_MS - 1 if last else 0
                if skipped > 0:
                    logger.warning(
                        "[CRON] %d boundaries skipped after a long run; crosses older than %d candles are lost",
                        skipped,
                        CRON_LOOKBACK_CANDLES,
                    )
                self._next_run = _iso_wib(target)
                logger.info("Next %s run at %s WIB", TIMEFRAME, self._next_run)
                await asyncio.sleep((target - now) / 1000)
                # Next target is computed from the boundary just run, so an early wake never runs it twice
                last = target
                boundary = target - delay_ms
                await self._execute_job(f"job:{TIMEFRAME}:{boundary}", TIMEFRAME_MS // 1000, is_manual=False)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Error in cron scheduler loop: %s", e)
                await asyncio.sleep(60)

    async def _execute_job(self, lock_name: str, lock_seconds: int, is_manual: bool) -> dict:
        # Skip, never queue: a manual run gets a 409, a scheduled one waits for its next boundary
        if self._lock.locked():
            logger.info("[CRON] %s run skipped: a job is already running", "Manual" if is_manual else "Scheduled")
            if is_manual:
                return {"status": "busy", "message": "A job is already running, try again later"}
            return {"status": "skipped", "message": "A job is already running"}

        async with self._lock:
            token = await acquire_lock(lock_name, timeout_seconds=lock_seconds)
            if token is None:
                if is_manual:
                    logger.info("[CRON] Manual run rejected: another worker is running it")
                    return {"status": "busy", "message": "A job is already running, try again later"}
                logger.info("[CRON] Scheduled run skipped: %s already taken by another worker", lock_name)
                return {"status": "skipped", "message": "Already executed by another worker"}

            try:
                return await self._run(is_manual)
            finally:
                # A scheduled lock is unique per boundary and expires after one candle, so it is kept
                if is_manual:
                    await release_lock(lock_name, token)

    async def _run(self, is_manual: bool) -> dict:
        self._last_run = _iso_wib(now_ms())
        self._total_runs += 1
        logger.info(">>> [CRON] Starting %s job (Manual: %s) <<<", TIMEFRAME, is_manual)
        try:
            client = self._client_getter() if self._client_getter else None
            if client is None:
                raise RuntimeError("Binance client not initialized")
            result = await run_job(client)
        except Exception as e:
            self._last_status = "failed"
            # Exception text can hold hosts, SQL or URLs and status is public, so only the class name leaves the log
            self._last_error = type(e).__name__
            logger.exception("[CRON] Job failed: %s", e)
            return {"status": "error", "message": "Scan failed, see server logs"}

        self._last_status = "success"
        self._last_error = None
        self._last_result = result
        return {"status": "success", **result}


cron_scheduler = Scheduler()
