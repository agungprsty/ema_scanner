import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from src.config.settings import (
    CRON_ENABLED,
    CRON_SAFETY_DELAY_SECONDS,
    MACD_FAST,
    MACD_MAX_SYMBOLS,
    MACD_SIGNAL,
    MACD_SLOW,
    MACD_TIMEFRAME,
    SCAN_CONCURRENCY,
)
from src.data_feed.ohlcv import fetch_klines
from src.services.db import (
    evaluate_pending_signals,
    get_watchlist,
    save_macd_signal,
)
from src.services.redis_client import (
    acquire_lock,
    is_cross_detected,
    mark_cross_detected,
    release_lock,
)
from src.services.telegram import send_alert
from src.strategy.macd import compute_macd, detect_macd_cross

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
        self._lock = asyncio.Lock()

    def start(self, client_getter: Callable) -> None:
        if not CRON_ENABLED:
            logger.info("CRON scheduler is disabled via CRON_ENABLED=false")
            return

        if self._task is not None and not self._task.done():
            return

        self._client_getter = client_getter
        self._is_active = True
        self._task = asyncio.create_task(self._cron_loop())
        logger.info("2H MACD Cron Scheduler started: strictly at Odd Hours (01, 03, 05, ... WIB)")

    def stop(self) -> None:
        self._is_active = False
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("2H MACD Cron Scheduler stopped")

    def get_status(self) -> dict:
        return {
            "enabled": CRON_ENABLED,
            "is_active": self._is_active,
            "schedule": "Every 2 Hours at Odd Hours (01, 03, 05, ... WIB)",
            "interval_hours": 2,
            "timezone": "WIB (UTC+7)",
            "timeframe": MACD_TIMEFRAME,
            "last_run": self._last_run,
            "next_run": self._next_run,
            "total_runs": self._total_runs,
            "last_signals_count": self._last_signals_count,
            "last_evaluated_count": self._last_evaluated_count,
            "last_error": self._last_error,
        }

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
                logger.error("Error in cron scheduler loop: %s", e, exc_info=True)
                await asyncio.sleep(60)

    async def _execute_job(self, is_manual: bool = False) -> dict:
        lock_name = "cron:2h_execution"
        acquired = await acquire_lock(lock_name, timeout_seconds=180)
        if not acquired and not is_manual:
            logger.info("[CRON] Job skipped: already acquired/executed by another worker")
            return {"status": "skipped", "message": "Already executed by another worker"}

        try:
            async with self._lock:
                client = self._client_getter() if self._client_getter else None
                if client is None:
                    logger.warning("Cron job execution skipped: Binance client not initialized")
                    return {"status": "error", "message": "Client not initialized"}

                start_time = time.perf_counter()
                exec_iso = datetime.now(timezone.utc).isoformat()
                self._last_run = exec_iso
                self._total_runs += 1

                logger.info(">>> [CRON] Starting 2H Scheduled Job (Manual: %s) <<<", is_manual)

            # Step 1: Evaluate existing pending signals in database
            eval_res = await evaluate_pending_signals(client)
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
                        df = await asyncio.to_thread(fetch_klines, client, sym, MACD_TIMEFRAME, 100)
                        if df is None:
                            return None
                        # Strictly confirmed candle only
                        df = df.iloc[:-1].copy()
                        df = compute_macd(df, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
                        if df is None:
                            return None
                        return detect_macd_cross(
                            df, sym, lookback_candles=1, include_current=False
                        )

                tasks = [_detect_symbol(s) for s in symbols]
                results = await asyncio.gather(*tasks)
                detected = [r for r in results if r is not None]

                for sig in detected:
                    is_dup = await is_cross_detected(
                        sig.side, sig.symbol, sig.cross_candle_ms, timeframe=MACD_TIMEFRAME
                    )
                    if not is_dup:
                        new_signals_found += 1
                        await mark_cross_detected(
                            sig.side, sig.symbol, sig.cross_candle_ms, timeframe=MACD_TIMEFRAME
                        )
                        await save_macd_signal(sig, timeframe=MACD_TIMEFRAME)

                        emoji = "🟢" if sig.side == "LONG" else "🔴"
                        telegram_parts.append(
                            f"{emoji} <b>[CRON 2H] {sig.side}</b> #${sig.symbol}\n"
                            f"📍 Entry: {sig.entry_price:.4f}\n"
                            f"⛔ Stop Loss: {sig.stop_loss:.4f}\n"
                            f"🎯 TP1: {sig.tp1:.4f} (R:R 1:{sig.risk_reward_ratio:.1f})\n"
                            f"Time: {sig.cross_time} (Confirmed)"
                        )

            self._last_signals_count = new_signals_found
            logger.info("[CRON] 2H Confirmed Scan finished: %d new signals detected", new_signals_found)

            if telegram_parts:
                header = "🏎️💨 <b>TOKYO DRIFT // 2H CRON NOTIFICATION</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                alert_body = header + "\n\n".join(telegram_parts)
                asyncio.create_task(send_alert(alert_body))

            elapsed = round(time.perf_counter() - start_time, 2)
            return {
                "status": "success",
                "execution_time": f"{elapsed}s",
                "evaluated": eval_res,
                "new_signals_count": new_signals_found,
                "symbols_scanned": len(symbols),
            }
        finally:
            if is_manual:
                await release_lock(lock_name)


cron_scheduler = MacdCronScheduler()
