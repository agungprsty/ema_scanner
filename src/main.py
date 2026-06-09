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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_firebase()
    global _monitor_task
    client = create_futures_client()
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
    return {"status": "running", "version": "v8.0.0", "mode": mode, "strategy": "Golden Cross (1h/4h)"}


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
    client = create_futures_client()
    is_dry_run = DRY_RUN if dry_run is None else dry_run
    mode_label = "DRY RUN" if is_dry_run else "PRODUCTION"

    try:
        btc_df = await asyncio.to_thread(fetch_klines, client, "BTCUSDT", htf, limit)
        if btc_df is None:
            return {"status": "error", "message": "Failed to fetch BTC data"}
        btc_bias = compute_btc_bias(btc_df)

        if btc_bias.side == "NEUTRAL":
            return {"status": "skipped", "btc_bias": "NEUTRAL", "message": "No clear BTC bias"}

        if btc_bias.strength < BTC_STRENGTH_MIN:
            return {"status": "skipped", "btc_bias": btc_bias.side, "strength": btc_bias.strength, "message": "BTC bias too weak"}

        exchange_info = await asyncio.to_thread(client.exchange_info)
        symbols_meta = {}
        for s in exchange_info.get("symbols", []):
            if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
                symbols_meta[s["symbol"]] = s

        vol_threshold = volume_m * 1_000_000
        stats_24h = await asyncio.to_thread(client.ticker_24hr_price_change)
        liquid_symbols = [
            s["symbol"] for s in stats_24h
            if float(s.get("quoteVolume", 0)) > vol_threshold
            and s["symbol"] in symbols_meta
        ]

        signal_cooldown: dict[str, int] = {}
        signals: list[Signal] = []
        for idx, sym in enumerate(liquid_symbols):
            # Stage 1: Fetch 4H data for macro filter
            df_macro = await asyncio.to_thread(fetch_klines, client, sym, htf, macro_limit)
            if df_macro is None:
                continue
            df_macro = compute_indicators(df_macro)
            if df_macro is None:
                continue

            if not macroscan_4h(df_macro):
                continue

            # Stage 2: Fetch 1H data for entry trigger
            df_entry = await asyncio.to_thread(fetch_klines, client, sym, timeframe, limit)
            if df_entry is None:
                continue
            df_entry = compute_indicators(df_entry)
            if df_entry is None:
                continue

            # Merge MA50_4h into 1H for TP1 reference
            macro_cols = df_macro[["timestamp", "close", "MA50"]].dropna().rename(
                columns={"close": "close_4h", "MA50": "MA50_4h"})
            df_entry = pd.merge_asof(df_entry, macro_cols, on="timestamp", direction="backward")
            df_entry = df_entry.dropna(subset=["MA50_4h"])

            if len(df_entry) < 50:
                continue

            sig = check_entry(
                df_1h=df_entry,
                btc_bias=btc_bias.side,
                symbol=sym,
                cooldown_map=signal_cooldown,
                current_index=idx,
            )
            if sig:
                signals.append(sig)
                signal_cooldown[sym] = idx
            if len(signals) >= TOTAL_SIGNALS:
                break

        results = []
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

            results.append({
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
            "signals": results,
        }

    except Exception as e:
        logger.error("Scan error: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}
