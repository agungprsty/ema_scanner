import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import httpx

from src.config.settings import (
    CRON_ENABLED,
    CRON_LOOKBACK_CANDLES,
    CRON_SAFETY_DELAY_SECONDS,
    CRON_STALE_SECONDS,
    HEALTHCHECK_PING_URL,
    LATE_SIGNAL_GRACE_SECONDS,
    MACD_CANDLES,
    MACD_FAST,
    MACD_MAX_SYMBOLS,
    MACD_SIGNAL,
    MACD_SLOW,
    MACD_TIMEFRAME,
    SCAN_CONCURRENCY,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from src.data_feed.ohlcv import fetch_klines
from src.services.alerts import format_signal_alert, notify_closed_trades
from src.services.background import run_in_background
from src.services.db import (
    evaluate_pending_signals,
    get_last_cron_success,
    get_watchlist,
    record_cron_run,
    save_macd_signal,
)
from src.services.redis_client import (
    acquire_lock,
    claim_cross,
    release_cross,
    release_lock,
)
from src.services.telegram import send_alert
from src.strategy.macd import MacdSignal, compute_macd, detect_macd_cross
from src.strategy.outcome import CANDLE_INTERVAL_MS

logger = logging.getLogger(__name__)


WIB_TZ = timezone(timedelta(hours=7))
ODD_HOURS = [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23]


def calculate_seconds_until_next_cron(safety_delay: int = 15) -> tuple[float, datetime]:
    now_wib = datetime.now(WIB_TZ)
    candidates = []
    for day_offset in (0, 1):
        base_date = (now_wib + timedelta(days=day_offset)).date()
        for h in ODD_HOURS:
            dt = datetime(
                base_date.year,
                base_date.month,
                base_date.day,
                h,
                0,
                safety_delay,
                tzinfo=WIB_TZ,
            )
            if dt > now_wib:
                candidates.append(dt)

    candidates.sort()
    next_dt = candidates[0]
    diff = (next_dt - now_wib).total_seconds()
    return max(diff, 1.0), next_dt


def is_late_signal(sig: MacdSignal, detected_at: datetime, interval_ms: int = CANDLE_INTERVAL_MS["2h"]) -> bool:
    """Whether a signal was found too long after its entry candle opened to be followed at its entry.

    That covers crosses older than the latest closed candle (bars_ago > 0) and a cron run that
    started well after the close, e.g. a manual trigger mid-candle.
    """
    entry_open_ms = sig.cross_candle_ms + interval_ms
    delay_ms = detected_at.timestamp() * 1000 - entry_open_ms
    return sig.bars_ago > 0 or delay_ms > LATE_SIGNAL_GRACE_SECONDS * 1000


def cron_slot_ms(at: datetime, interval_ms: int = CANDLE_INTERVAL_MS["2h"]) -> int:
    """Open time of the candle a run at `at` belongs to: the 2H boundary the scheduled run follows."""
    at_ms = int(at.timestamp() * 1000)
    return at_ms - at_ms % interval_ms


async def ping_dead_man_switch(url: str | None = None) -> bool:
    """Tell an external monitor (Healthchecks.io, Cronitor, ...) a scheduled run succeeded.

    The monitor raises the alarm when pings stop, which also covers the whole process being down.
    Failures are only logged: the monitor will notice the missing ping on its own.
    """
    url = HEALTHCHECK_PING_URL if url is None else url
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
        if resp.status_code >= 400:
            logger.warning("[CRON] Dead-man ping answered HTTP %s", resp.status_code)
            return False
        return True
    except Exception as e:
        logger.warning("[CRON] Dead-man ping failed: %s", type(e).__name__)
        return False


class MacdCronScheduler:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._is_active: bool = False
        self._client_getter: Callable | None = None
        self._last_run: str | None = None
        self._next_run: str | None = None
        self._total_runs: int = 0
        self._last_signals_count: int = 0
        self._last_evaluated_count: int = 0
        self._last_error: str | None = None
        self._last_status: str | None = None
        self._last_success: datetime | None = None
        self._started_at: datetime | None = None
        self._lock = asyncio.Lock()

    def start(self, client_getter: Callable) -> None:
        if not CRON_ENABLED:
            logger.info("CRON scheduler is disabled via CRON_ENABLED=false")
            return

        if self._task is not None and not self._task.done():
            return

        self._client_getter = client_getter
        self._is_active = True
        self._started_at = datetime.now(UTC)
        self._task = asyncio.create_task(self._cron_loop())
        logger.info("2H MACD Cron Scheduler started: strictly at Odd Hours (01, 03, 05, ... WIB)")

    def stop(self) -> None:
        self._is_active = False
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("2H MACD Cron Scheduler stopped")

    def get_status(self, include_error: bool = False) -> dict:
        """Scheduler state. Error text can carry internals, so it is only included when asked for."""
        status = {
            "enabled": CRON_ENABLED,
            "is_active": self._is_active,
            "schedule": "Every 2 Hours at Odd Hours (01, 03, 05, ... WIB)",
            "interval_hours": 2,
            "timezone": "WIB (UTC+7)",
            "timeframe": MACD_TIMEFRAME,
            "last_run": self._last_run,
            "last_status": self._last_status,
            "last_success": self._last_success.isoformat() if self._last_success else None,
            "next_run": self._next_run,
            "total_runs": self._total_runs,
            "last_signals_count": self._last_signals_count,
            "last_evaluated_count": self._last_evaluated_count,
        }
        if include_error:
            status["last_error"] = self._last_error
        return status

    async def is_fresh(self, now: datetime | None = None) -> bool | None:
        """Whether a scheduled run succeeded recently enough; None when the scheduler is disabled.

        The newest success comes from the cron_runs table, so it survives restarts and covers every
        worker, falling back to this process's memory when the database is unavailable. Before any
        run has ever succeeded, the clock starts when this scheduler started, so a fresh deploy is not
        reported stale before its first slot. A scheduler whose loop has died is never fresh.
        """
        if not CRON_ENABLED:
            return None
        if self._task is None or self._task.done():
            return False
        now = now or datetime.now(UTC)
        known = [t for t in (await get_last_cron_success(), self._last_success) if t is not None]
        last = max(known) if known else self._started_at
        if last is None:
            return False
        return (now - last).total_seconds() <= CRON_STALE_SECONDS

    async def trigger_now(self) -> dict:
        return await self._execute_job(is_manual=True)

    async def _cron_loop(self) -> None:
        while self._is_active:
            try:
                seconds_to_wait, next_dt = calculate_seconds_until_next_cron(
                    safety_delay=CRON_SAFETY_DELAY_SECONDS,
                )
                self._next_run = next_dt.isoformat()
                logger.info(
                    "Next 2H Cron run scheduled in %.1f minutes (%s WIB)",
                    seconds_to_wait / 60,
                    next_dt.strftime("%Y-%m-%d %H:%M:%S"),
                )
                await asyncio.sleep(seconds_to_wait)
                await self._execute_job(is_manual=False)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._last_error = str(e)
                logger.exception("Error in cron scheduler loop: %s", e)
                await asyncio.sleep(60)

    async def _execute_job(self, is_manual: bool = False) -> dict:
        lock_name = "cron:2h_execution"
        if is_manual and self._lock.locked():
            logger.info("[CRON] Manual trigger rejected: a job is already running")
            return {"status": "busy", "message": "A cron job is already running, try again later"}

        await self._lock.acquire()
        token = None
        started_at = datetime.now(UTC)
        try:
            token = await acquire_lock(lock_name, timeout_seconds=180)
            if token is None:
                if is_manual:
                    logger.info("[CRON] Manual trigger rejected: lock held by another job")
                    return {"status": "busy", "message": "A cron job is already running or just ran, try again later"}
                logger.info("[CRON] Job skipped: already acquired/executed by another worker")
                await self._record_run(started_at, "skipped")
                return {"status": "skipped", "message": "Already executed by another worker"}

            client = self._client_getter() if self._client_getter else None
            if client is None:
                logger.warning("Cron job execution skipped: Binance client not initialized")
                if not is_manual:
                    await self._record_run(started_at, "error", error="Client not initialized")
                return {"status": "error", "message": "Client not initialized"}

            try:
                result = await self._run_job(client, is_manual)
            except Exception as e:
                if not is_manual:
                    self._last_status = "error"
                    await self._record_run(started_at, "error", error=f"{type(e).__name__}: {e}")
                raise

            # Only scheduled runs prove the schedule is alive; a manual trigger says nothing about it
            if not is_manual:
                self._last_status = "ok"
                self._last_error = None
                self._last_success = datetime.now(UTC)
                await self._record_run(
                    started_at,
                    "ok",
                    signals_count=result["new_signals_count"],
                    evaluated_count=self._last_evaluated_count,
                )
                if HEALTHCHECK_PING_URL:
                    run_in_background(ping_dead_man_switch(), name="cron-dead-man-ping")
            return result
        finally:
            try:
                if is_manual and token is not None:
                    await release_lock(lock_name, token)
            finally:
                self._lock.release()

    async def _record_run(self, started_at: datetime, status: str, **fields) -> None:
        await record_cron_run(
            slot_ms=cron_slot_ms(started_at),
            started_at=started_at,
            finished_at=datetime.now(UTC),
            status=status,
            **fields,
        )

    async def _run_job(self, client, is_manual: bool) -> dict:
        start_time = time.perf_counter()
        exec_iso = datetime.now(UTC).isoformat()
        self._last_run = exec_iso
        self._total_runs += 1

        logger.info(">>> [CRON] Starting 2H Scheduled Job (Manual: %s) <<<", is_manual)

        # Step 1: Evaluate existing pending signals in database
        eval_res = await evaluate_pending_signals(client)
        await notify_closed_trades(eval_res.pop("closed", None))
        self._last_evaluated_count = eval_res.get("evaluated_count", 0)
        logger.info(
            "[CRON] Evaluated pending trades: %d (Wins/Profit: %d, Loss: %d)",
            self._last_evaluated_count,
            eval_res.get("profit_count", 0),
            eval_res.get("loss_count", 0),
        )

        # Step 2: Scan for newly confirmed 2H MACD signals
        watchlist_items = await get_watchlist(active_only=True)
        symbols = [item["symbol"] for item in watchlist_items][:MACD_MAX_SYMBOLS]
        new_signals_found = 0
        telegram_parts = []

        if symbols:
            sem = asyncio.Semaphore(SCAN_CONCURRENCY)

            async def _detect_symbol(sym: str):
                async with sem:
                    df = await asyncio.to_thread(fetch_klines, client, sym, MACD_TIMEFRAME, MACD_CANDLES)
                    if df is None:
                        return None
                    df = compute_macd(df, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
                    if df is None:
                        return None
                    # Closed candles only; re-checking a few catches crosses missed while down
                    return detect_macd_cross(
                        df, sym, lookback_candles=CRON_LOOKBACK_CANDLES, include_current=False
                    )

            tasks = [_detect_symbol(s) for s in symbols]
            results = await asyncio.gather(*tasks)
            detected = [r for r in results if r is not None]
            alert_enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

            for sig in detected:
                claimed = await claim_cross(
                    sig.side, sig.symbol, sig.cross_candle_ms, timeframe=MACD_TIMEFRAME
                )
                if claimed:
                    # A late signal is still recorded and alerted, but as a catch-up that
                    # stays out of the track record: candles after its entry already closed
                    detected_at = datetime.now(UTC)
                    late = is_late_signal(sig, detected_at)
                    saved = await save_macd_signal(
                        sig,
                        timeframe=MACD_TIMEFRAME,
                        source="catchup" if late else "cron",
                        detected_at=detected_at,
                    )
                    if saved and "id" in saved:
                        if saved.get("inserted") is False:
                            continue
                    elif alert_enabled:
                        logger.warning(
                            "[CRON] Failed to save MACD signal %s %s @ %s; alerting without persistence",
                            sig.side,
                            sig.symbol,
                            sig.cross_candle_ms,
                        )
                    else:
                        await release_cross(
                            sig.side, sig.symbol, sig.cross_candle_ms, timeframe=MACD_TIMEFRAME
                        )
                        logger.warning(
                            "[CRON] Failed to save MACD signal %s %s @ %s; dedup claim released",
                            sig.side,
                            sig.symbol,
                            sig.cross_candle_ms,
                        )
                        continue

                    new_signals_found += 1
                    telegram_parts.append(format_signal_alert(sig, late=late))

        self._last_signals_count = new_signals_found
        logger.info("[CRON] 2H Confirmed Scan finished: %d new signals detected", new_signals_found)

        if telegram_parts:
            header = "<b>Crossline: scheduled 2H scan</b>\n\n"
            alert_body = header + "\n\n".join(telegram_parts)
            run_in_background(send_alert(alert_body), name="cron-telegram-alert")

        elapsed = round(time.perf_counter() - start_time, 2)
        return {
            "status": "success",
            "execution_time": f"{elapsed}s",
            "evaluated": eval_res,
            "new_signals_count": new_signals_found,
            "symbols_scanned": len(symbols),
        }


cron_scheduler = MacdCronScheduler()
