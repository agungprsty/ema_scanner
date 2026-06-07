import asyncio
import logging
import time
from contextlib import asynccontextmanager

import pandas as pd
from fastapi import FastAPI, Query
from typing import Annotated

from src.config.settings import (
    TOTAL_SIGNALS, VOLUME_THRESHOLD_USD, RISK_PER_TRADE_PERCENT, DRY_RUN,
    BTC_STRENGTH_MIN, MAX_TOTAL_RISK_PCT, MAX_DAILY_LOSS_PCT,
)
from src.data_feed.binance_client import create_futures_client
from src.data_feed.ohlcv import fetch_klines
from src.data_feed.macro_filter import compute_btc_bias, BtcBias
from src.strategy.indicators import compute_indicators
from src.strategy.blueprint import hard_filter_checklist, Signal
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
    return {"status": "running", "version": "v7.0.0", "mode": mode, "strategy": "Day Trader MTF (15m/1h/4h)"}


@app.get("/api/scan")
async def scan(
    timeframe: Annotated[str, Query(description="Entry timeframe (LTF)")] = "15m",
    mtf: Annotated[str, Query(description="Middle timeframe (trend)")] = "1h",
    htf: Annotated[str, Query(description="Macro timeframe (filter)")] = "4h",
    limit: Annotated[int, Query(description="Candles to fetch for LTF")] = 500,
    volume_m: Annotated[int, Query(description="Volume threshold (M)")] = 50,
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

        mid_limit = max(300, limit // 4)
        macro_limit = max(300, limit // 16)

        signal_cooldown: dict[str, int] = {}
        signals: list[Signal] = []
        for idx, sym in enumerate(liquid_symbols):
            df_ltf = await asyncio.to_thread(fetch_klines, client, sym, timeframe, limit)
            if df_ltf is None:
                continue
            df_ltf = compute_indicators(df_ltf)
            if df_ltf is None:
                continue

            df_mid = await asyncio.to_thread(fetch_klines, client, sym, mtf, mid_limit)
            if df_mid is not None:
                df_mid = compute_indicators(df_mid)

            df_macro = await asyncio.to_thread(fetch_klines, client, sym, htf, macro_limit)
            if df_macro is not None:
                df_macro = compute_indicators(df_macro)

            df = df_ltf.copy()
            has_mid = df_mid is not None and "EMA20" in df_mid.columns
            has_macro = df_macro is not None and "EMA50" in df_macro.columns and "EMA200" in df_macro.columns

            if has_mid:
                mid_cols = df_mid[["timestamp", "close", "EMA20"]].dropna().rename(
                    columns={"close": "close_1h", "EMA20": "EMA20_1h"})
                df = pd.merge_asof(df, mid_cols, on="timestamp", direction="backward")
            if has_macro:
                macro_cols = df_macro[["timestamp", "close", "EMA50", "EMA200"]].dropna().rename(
                    columns={"close": "close_4h", "EMA50": "EMA50_4h", "EMA200": "EMA200_4h"})
                df = pd.merge_asof(df, macro_cols, on="timestamp", direction="backward")

            if has_mid and has_macro:
                df = df.dropna(subset=["close_1h", "EMA20_1h", "EMA50_4h", "EMA200_4h"])
            elif has_mid:
                df = df.dropna(subset=["close_1h", "EMA20_1h"])

            if len(df) < 50:
                continue

            sig = hard_filter_checklist(
                df=df,
                btc_bias=btc_bias.side,
                btc_strength=btc_bias.strength,
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
                tp_price=float(sig.tp_price),
                balance=balance,
                risk_pct=RISK_PER_TRADE_PERCENT,
                tick_size=tick_size,
                step_size=step_size,
            )

            if is_dry_run:
                status_msg = "DRY RUN"
            else:
                trade_id = create_trade(
                    symbol=spec.symbol,
                    side=spec.side,
                    tf=f"{timeframe}/{mtf}/{htf}",
                    entry=float(spec.entry),
                    sl=float(spec.stop_loss),
                    tp=float(spec.take_profit),
                    qty=float(spec.quantity),
                    atr=sig.atr,
                )

                resp = await asyncio.to_thread(place_limit_order, client, spec)
                if resp and resp.get("orderId"):
                    order_id = resp["orderId"]
                    update_trade_status(trade_id, "LIMIT_PLACED", binance_order_id=order_id)
                    status_msg = "ORDER PLACED"
                else:
                    update_trade_status(trade_id, "PENDING")
                    status_msg = "SETUP CALL"

            msg = (
                f"[{mode_label}] [{status_msg}] {spec.side} {spec.symbol}\n"
                f"Entry: {spec.entry} | SL: {spec.stop_loss} | TP: {spec.take_profit}\n"
                f"Qty: {spec.quantity}"
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
                "reason": sig.reason,
                "status": status_msg,
            })

        return {
            "status": "success",
            "mode": mode_label,
            "btc_bias": btc_bias.side,
            "btc_strength": btc_bias.strength,
            "btc_vol_regime": btc_bias.vol_regime,
            "timeframe_ltf": timeframe,
            "timeframe_mtf": mtf,
            "timeframe_htf": htf,
            "execution_time": f"{time.perf_counter() - start:.2f}s",
            "total_scanned": len(liquid_symbols),
            "signals": results,
        }

    except Exception as e:
        logger.error("Scan error: %s", e, exc_info=True)
        return {"status": "error", "message": str(e)}
