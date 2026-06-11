import asyncio
import logging
import time
from contextlib import asynccontextmanager

import pandas as pd
from fastapi import FastAPI, Query
from typing import Annotated

from src.config.settings import (
    TOTAL_SIGNALS, RISK_PER_TRADE_PERCENT, DRY_RUN,
    BTC_STRENGTH_MIN, GC_ENTRY_FEE_PCT,
)
from src.data_feed.binance_client import create_futures_client
from src.data_feed.ohlcv import fetch_klines
from src.data_feed.macro_filter import compute_btc_bias
from src.strategy.indicators import compute_indicators
from src.strategy.blueprint import macroscan_4h, check_entry, Signal
from src.risk_manager.calculator import calculate_position
from src.execution.order import place_limit_order
from src.services.firebase import init_firebase, create_trade, update_trade_status
from src.services.telegram import send_alert_async
from src.execution.monitor import monitor_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

_monitor_task = None
_client = None

_cache: dict[str, dict] = {}
EXCHANGE_INFO_TTL = 300
TICKER_TTL = 60
CONCURRENCY = 10


def _get_client():
    global _client
    if _client is None:
        _client = create_futures_client()
    return _client


async def _get_cached(client, key: str, fetch_fn, ttl: int):
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached["ts"] < ttl:
        return cached["data"]
    data = await asyncio.to_thread(fetch_fn)
    _cache[key] = {"data": data, "ts": now}
    return data


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_firebase()
    global _monitor_task
    client = _get_client()
    if not DRY_RUN:
        _monitor_task = asyncio.create_task(monitor_loop(client))
    else:
        logger.info("DRY RUN mode — monitor loop disabled")
    yield
    if _monitor_task:
        _monitor_task.cancel()


app = FastAPI(title="Crypto Blueprint Bot", lifespan=lifespan)


@app.get("/")
def root():
    mode = "DRY_RUN" if DRY_RUN else "PRODUCTION"
    return {"status": "running", "version": "v8.0.0", "mode": mode, "strategy": "EMA5/EMA20 Cross (1h/4h)"}


@app.get("/api/scan")
async def scan(
    timeframe: Annotated[str, Query(description="Entry timeframe (golden cross)")] = "1h",
    htf: Annotated[str, Query(description="Macro timeframe (filter)")] = "4h",
    limit: Annotated[int, Query(description="Candles to fetch for LTF")] = 500,
    macro_limit: Annotated[int, Query(description="Candles to fetch for HTF")] = 200,
    volume_m: Annotated[int, Query(description="Volume threshold (M)")] = 100,
    send_telegram: Annotated[bool, Query(description="Send to Telegram")] = True,
    dry_run: Annotated[bool | None, Query(description="Override dry-run mode")] = None,
):
    start = time.perf_counter()
    client = _get_client()
    is_dry_run = DRY_RUN if dry_run is None else dry_run
    mode_label = "DRY RUN" if is_dry_run else "PRODUCTION"

    try:
        btc_df = await asyncio.to_thread(fetch_klines, client, "BTCUSDT", htf, limit)
        if btc_df is None:
            return {"status": "error", "message": "Failed to fetch BTC data"}
        btc_bias = compute_btc_bias(btc_df)

        if btc_bias.side == "NEUTRAL":
            scan_time = f"{time.perf_counter() - start:.2f}s"
            return {"status": "skipped", "btc_bias": "NEUTRAL", "execution_time": scan_time, "message": "No clear BTC bias"}

        if btc_bias.strength < BTC_STRENGTH_MIN:
            scan_time = f"{time.perf_counter() - start:.2f}s"
            return {"status": "skipped", "btc_bias": btc_bias.side, "strength": btc_bias.strength, "execution_time": scan_time, "message": "BTC bias too weak"}

        exchange_info = await _get_cached(client, "exchange_info", client.exchange_info, EXCHANGE_INFO_TTL)
        symbols_meta = {}
        for s in exchange_info.get("symbols", []):
            if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
                symbols_meta[s["symbol"]] = s

        vol_threshold = volume_m * 1_000_000
        stats_24h = await _get_cached(client, "ticker_24h", client.ticker_24hr_price_change, TICKER_TTL)
        liquid_symbols = [
            s["symbol"] for s in stats_24h
            if float(s.get("quoteVolume", 0)) > vol_threshold
            and s["symbol"] in symbols_meta
        ]

        sem = asyncio.Semaphore(CONCURRENCY)

        async def _detect(sym: str) -> Signal | None:
            async with sem:
                df_macro = await asyncio.to_thread(fetch_klines, client, sym, htf, macro_limit)
                if df_macro is None:
                    return None
                df_macro = compute_indicators(df_macro)
                if df_macro is None:
                    return None

                if not macroscan_4h(df_macro):
                    return None

                df_entry = await asyncio.to_thread(fetch_klines, client, sym, timeframe, limit)
                if df_entry is None:
                    return None
                df_entry = compute_indicators(df_entry)
                if df_entry is None:
                    return None

                macro_cols = df_macro[["timestamp", "close", "EMA20"]].dropna().rename(
                    columns={"close": "close_4h", "EMA20": "EMA20_4h"})
                df_entry = pd.merge_asof(df_entry, macro_cols, on="timestamp", direction="backward")
                df_entry = df_entry.dropna(subset=["EMA20_4h"])

                if len(df_entry) < 50:
                    return None

                check_entry_kwargs = {
                    "btc_bias": btc_bias.side,
                    "symbol": sym,
                }
                return check_entry(df_1h=df_entry, **check_entry_kwargs)

        tasks = [_detect(sym) for sym in liquid_symbols]
        results = await asyncio.gather(*tasks)
        signals: list[Signal] = [r for r in results if r is not None][:TOTAL_SIGNALS]

        results_json = []
        for sig in signals:
            meta = symbols_meta.get(sig.symbol, {})
            filters = {f["filterType"]: f for f in meta.get("filters", [])}
            price_filter = filters.get("PRICE_FILTER", {})
            lot_filter = filters.get("LOT_SIZE", {})

            tick_size = float(price_filter.get("tickSize", 0.01))
            step_size = float(lot_filter.get("stepSize", 0.001))

            if not is_dry_run:
                account = await asyncio.to_thread(client.account)
                balance = 0.0
                for asset in account.get("assets", []):
                    if asset["asset"] == "USDT":
                        balance = float(asset["walletBalance"])
                        break
            else:
                balance = 10_000.0

            spec = calculate_position(
                symbol=sig.symbol,
                side=sig.side,
                entry_price=float(sig.entry_price),
                sl_price=float(sig.sl_price),
                tp_price=0.0,
                balance=balance,
                risk_pct=RISK_PER_TRADE_PERCENT,
                tick_size=tick_size,
                step_size=step_size,
                tp1_explicit=float(sig.tp1_price),
            )

            bep = float(spec.entry) * (1 + GC_ENTRY_FEE_PCT / 100)
            trade_id = create_trade(
                symbol=spec.symbol,
                side=spec.side,
                tf=f"{timeframe}/{htf}",
                entry=float(spec.entry),
                sl=float(spec.stop_loss),
                tp=float(spec.take_profit),
                qty=float(spec.quantity),
                atr=sig.atr_1h,
                bep=bep,
            )

            if is_dry_run:
                status_msg = "DRY RUN"
            else:
                resp = await asyncio.to_thread(place_limit_order, client, spec)
                if resp and resp.get("orderId"):
                    order_id = resp["orderId"]
                    update_trade_status(trade_id, "LIMIT_PLACED", binance_order_id=order_id)
                    status_msg = "ORDER PLACED"
                else:
                    update_trade_status(trade_id, "PENDING")
                    status_msg = "SETUP CALL"

            pct_sl = abs(float(spec.entry) - float(spec.stop_loss)) / float(spec.entry) * 100
            pct_tp = abs(float(spec.take_profit) - float(spec.entry)) / float(spec.entry) * 100
            msg = (
                f"{mode_label} | {status_msg}\n"
                f"{spec.side} {spec.symbol}\n"
                f"Entry: {spec.entry}\n"
                f"SL: {spec.stop_loss} ({pct_sl:.2f}%)\n"
                f"TP1 (50%): {spec.take_profit} ({pct_tp:.2f}%)\n"
                f"BEP after TP1: {bep:.4f}\n"
                f"Qty: {spec.quantity} | TP1 Qty: {spec.quantity_tp1}"
            )
            if send_telegram:
                send_alert_async(msg)
            logger.info(msg)

            results_json.append({
                "symbol": spec.symbol,
                "side": spec.side,
                "entry": float(spec.entry),
                "stop_loss": float(spec.stop_loss),
                "take_profit": float(spec.take_profit),
                "quantity": float(spec.quantity),
                "quantity_tp1": float(spec.quantity_tp1),
                "reason": sig.reason,
                "status": status_msg,
            })

        return {
            "status": "success",
            "mode": mode_label,
            "btc_bias": btc_bias.side,
            "btc_strength": btc_bias.strength,
            "btc_vol_regime": btc_bias.vol_regime,
            "timeframe_entry": timeframe,
            "timeframe_macro": htf,
            "execution_time": f"{time.perf_counter() - start:.2f}s",
            "total_scanned": len(liquid_symbols),
            "signals": results_json,
        }

    except Exception as e:
        logger.error("Scan error: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}
