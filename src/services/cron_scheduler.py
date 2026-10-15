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
    RISK_REWARD_RATIO,
    SCAN_CONCURRENCY,
    SHADOW_ALERTS,
    SIGNAL_EXPIRY_CANDLES,
    STRATEGY_KEY,
    STRATEGY_VERSION,
    SWING_LOOKBACK_CANDLES,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from src.data_feed.ohlcv import MIN_KLINES, fetch_klines
from src.ledger.candles import context_fingerprint
from src.services.alerts import format_outcome_alert, format_signal_alert, notify_closed_trades
from src.services.background import run_in_background
from src.services.db import (
    evaluate_pending_signals,
    get_last_cron_success,
    get_strategy_versions,
    get_watchlist,
    record_alert_events,
    record_cron_run,
    record_daily_ledger_head,
    save_macd_signal,
)
from src.services.redis_client import (
    acquire_lock,
    claim_cross,
    release_cross,
    release_lock,
)
from src.services.telegram import send_alert
from src.strategy import registry
from src.strategy.base import SignalIntent, Strategy
from src.strategy.context import MarketContext, timeframe_ms
from src.strategy.macd import MacdSignal
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


def fetch_plan(strategies: list[Strategy]) -> dict[str, int]:
    """Klines to fetch per timeframe and symbol so each strategy gets what it reads. Each timeframe is
    fetched once per symbol per run and shared by every strategy (the per-run cache)."""
    plan: dict[str, int] = {}
    for strategy in strategies:
        plan[strategy.timeframe] = max(plan.get(strategy.timeframe, 0), strategy.history_bars)
        for tf in strategy.context_timeframes:
            plan[tf] = max(plan.get(tf, 0), context_limit(strategy))
    return plan


def context_limit(strategy: Strategy) -> int:
    # The closed context candles plus the forming one, and at least what fetch_klines accepts
    return max(strategy.context_bars + 1, MIN_KLINES)


def detect_strategy_signals(
    strategy: Strategy, symbol: str, frames: dict, now_ms: int, catchup_bars: int = CRON_LOOKBACK_CANDLES
) -> list[SignalIntent]:
    """Run one strategy on the klines fetched for `symbol`, as of `now_ms`. Each strategy sees exactly
    the window it asked for, whatever another strategy made the run fetch, so its indicators match a
    run where it is alone."""
    if any(frames.get(tf) is None for tf in (strategy.timeframe, *strategy.context_timeframes)):
        return []
    own = {strategy.timeframe: frames[strategy.timeframe].tail(strategy.history_bars)}
    for tf in strategy.context_timeframes:
        own[tf] = frames[tf].tail(context_limit(strategy))
    ctx = MarketContext.from_klines(symbol, now_ms, strategy.timeframe, own, catchup_bars=catchup_bars)
    intents = strategy.detect(ctx)
    if intents:
        # What the strategy read, for the signal's detected event: anyone holding the same klines can
        # recompute the hash (src/ledger/candles.py)
        sha, summary = context_fingerprint(ctx)
        for intent in intents:
            intent.input_candles_sha256 = sha
            intent.input_candles = summary
    return intents


def record_alert_on_delivery(task, signal_ids: list[int], kind: str, message: str) -> None:
    """Once the Telegram task for `message` finishes and reports delivery, append an alert_sent event
    for each signal it announced. A failed or skipped send records nothing."""
    if not signal_ids or not isinstance(task, asyncio.Task):
        return

    def _done(t: asyncio.Task) -> None:
        if t.cancelled() or t.exception() is not None or not t.result():
            return
        run_in_background(record_alert_events(signal_ids, kind, message), name="ledger-alert-sent")

    task.add_done_callback(_done)


def settings_drift() -> list[str]:
    """Configuration that no longer does what it did before strategies were registered: the MACD_*
    and trade settings only drive ad-hoc scans now, and the headline version should be a live one."""
    warnings = []
    headline = registry.lookup(STRATEGY_KEY, STRATEGY_VERSION)
    if headline is None or headline.status != "live":
        warnings.append(
            f"STRATEGY_KEY/STRATEGY_VERSION {STRATEGY_KEY}@{STRATEGY_VERSION} is not a registered live strategy; "
            "the headline statistics report on a version the scheduled scan does not run"
        )
    if headline is not None and headline.key == "macd_cross":
        env = {
            "macd_fast": MACD_FAST,
            "macd_slow": MACD_SLOW,
            "macd_signal": MACD_SIGNAL,
            "macd_candles": MACD_CANDLES,
            "risk_reward_ratio": RISK_REWARD_RATIO,
            "swing_lookback_candles": SWING_LOOKBACK_CANDLES,
            "signal_expiry_candles": SIGNAL_EXPIRY_CANDLES,
        }
        differ = {k: v for k, v in env.items() if headline.params_dict.get(k) != v}
        if differ:
            warnings.append(
                f"settings {differ} differ from the frozen parameters of {headline.ref}; the scheduled scan and the "
                "evaluator use the frozen ones, only ad-hoc scans use the settings"
            )
    return warnings


def dedup_scope(strategy: Strategy) -> str | None:
    """Redis claim scope: None (the original keys, shared with ad-hoc scans) for the headline version."""
    return None if (strategy.key, strategy.version) == (STRATEGY_KEY, STRATEGY_VERSION) else strategy.ref


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
        self._emitting: list[str] = []
        self._refused: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def start(self, client_getter: Callable) -> None:
        if not CRON_ENABLED:
            logger.info("CRON scheduler is disabled via CRON_ENABLED=false")
            return

        if self._task is not None and not self._task.done():
            return

        for warning in settings_drift():
            logger.warning("[CRON] %s", warning)
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
            # Strategy versions the last run emitted for, and those it refused (not frozen, or code
            # or parameters that differ from the frozen version)
            "strategies": {"emitting": list(self._emitting), "refused": dict(self._refused)},
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
        closed = eval_res.pop("closed", None)
        if await notify_closed_trades(closed):
            await record_alert_events([t.get("signal_id") for t in closed], "outcome", format_outcome_alert(closed))
        self._last_evaluated_count = eval_res.get("evaluated_count", 0)
        logger.info(
            "[CRON] Evaluated pending trades: %d (Wins/Profit: %d, Loss: %d)",
            self._last_evaluated_count,
            eval_res.get("profit_count", 0),
            eval_res.get("loss_count", 0),
        )

        # Step 2: Run every live and shadow strategy whose frozen version matches the running code
        strategies, refused = registry.emitting_strategies(await get_strategy_versions())
        self._emitting = [s.ref for s in strategies]
        self._refused = refused
        watchlist_items = await get_watchlist(active_only=True)
        symbols = [item["symbol"] for item in watchlist_items][:MACD_MAX_SYMBOLS]
        new_signals_found = 0
        telegram_parts = []
        shadow_parts: dict[str, list[str]] = {}
        # Stored signals each message announces, for their alert_sent events
        telegram_ids: list[int] = []
        shadow_ids: dict[str, list[int]] = {}

        if symbols and strategies:
            sem = asyncio.Semaphore(SCAN_CONCURRENCY)
            plan = fetch_plan(strategies)
            now_ms = int(datetime.now(UTC).timestamp() * 1000)

            async def _scan_symbol(sym: str) -> list[tuple[Strategy, MacdSignal]]:
                async with sem:
                    frames = {}
                    for tf, limit in plan.items():
                        frames[tf] = await asyncio.to_thread(fetch_klines, client, sym, tf, limit)
                found = []
                for strategy in strategies:
                    try:
                        # Closed candles only; re-checking a few catches crosses missed while down
                        found.extend((strategy, sig) for sig in detect_strategy_signals(strategy, sym, frames, now_ms))
                    except Exception:
                        logger.exception("[CRON] %s detection failed on %s", strategy.ref, sym)
                return found

            results = await asyncio.gather(*[_scan_symbol(s) for s in symbols])
            detected = [pair for found in results for pair in found]
            alert_enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

            for strategy, sig in detected:
                scope = dedup_scope(strategy)
                # Shadow strategies are stored and evaluated like the live one but stay off Telegram
                alerts_on = alert_enabled and (strategy.status == "live" or SHADOW_ALERTS)
                claimed = await claim_cross(
                    sig.side, sig.symbol, sig.cross_candle_ms, timeframe=strategy.timeframe, strategy=scope
                )
                if claimed:
                    # A late signal is still recorded and alerted, but as a catch-up that
                    # stays out of the track record: candles after its entry already closed
                    detected_at = datetime.now(UTC)
                    late = is_late_signal(sig, detected_at, timeframe_ms(strategy.timeframe))
                    saved = await save_macd_signal(
                        sig,
                        timeframe=strategy.timeframe,
                        source="catchup" if late else "cron",
                        detected_at=detected_at,
                    )
                    if saved and "id" in saved:
                        if saved.get("inserted") is False:
                            continue
                    elif alerts_on:
                        logger.warning(
                            "[CRON] Failed to save %s signal %s %s @ %s; alerting without persistence",
                            strategy.ref,
                            sig.side,
                            sig.symbol,
                            sig.cross_candle_ms,
                        )
                    else:
                        await release_cross(
                            sig.side, sig.symbol, sig.cross_candle_ms, timeframe=strategy.timeframe, strategy=scope
                        )
                        logger.warning(
                            "[CRON] Failed to save %s signal %s %s @ %s; dedup claim released",
                            strategy.ref,
                            sig.side,
                            sig.symbol,
                            sig.cross_candle_ms,
                        )
                        continue

                    new_signals_found += 1
                    saved_id = saved.get("id") if saved else None
                    if strategy.status == "live":
                        telegram_parts.append(format_signal_alert(sig, late=late))
                        if saved_id is not None:
                            telegram_ids.append(saved_id)
                    elif SHADOW_ALERTS:
                        shadow_parts.setdefault(strategy.ref, []).append(format_signal_alert(sig, late=late))
                        if saved_id is not None:
                            shadow_ids.setdefault(strategy.ref, []).append(saved_id)

        self._last_signals_count = new_signals_found
        logger.info("[CRON] 2H Confirmed Scan finished: %d new signals detected", new_signals_found)

        if telegram_parts:
            header = "<b>Crossline: scheduled 2H scan</b>\n\n"
            alert_body = header + "\n\n".join(telegram_parts)
            task = run_in_background(send_alert(alert_body), name="cron-telegram-alert")
            record_alert_on_delivery(task, telegram_ids, "signal", alert_body)
        for ref, parts in shadow_parts.items():
            header = f"<b>Crossline shadow {ref}</b> (paper only, not the live strategy)\n\n"
            body = header + "\n\n".join(parts)
            task = run_in_background(send_alert(body), name="cron-telegram-shadow-alert")
            record_alert_on_delivery(task, shadow_ids.get(ref, []), "shadow_signal", body)

        # The first run of each UTC day publishes the chain head (ledger_heads, /api/ledger/head)
        await record_daily_ledger_head()

        elapsed = round(time.perf_counter() - start_time, 2)
        return {
            "status": "success",
            "execution_time": f"{elapsed}s",
            "evaluated": eval_res,
            "new_signals_count": new_signals_found,
            "symbols_scanned": len(symbols),
            "strategies": self._emitting,
            "refused_strategies": self._refused,
        }


cron_scheduler = MacdCronScheduler()
