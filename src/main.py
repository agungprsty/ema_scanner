import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

import pandas as pd
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request
from typing import Annotated

from datetime import datetime

from src.config.settings import (
    TOTAL_SIGNALS, RISK_PER_TRADE_PERCENT, DRY_RUN,
    BTC_STRENGTH_MIN, LONG_ENTRY_FEE_PCT, SHORT_ENTRY_FEE_PCT,
    CROSS_LOOKBACK_CANDLES,
)
from src.data_feed.binance_client import create_futures_client
from src.data_feed.ohlcv import fetch_klines
from src.data_feed.macro_filter import compute_btc_bias
from src.strategy.indicators import compute_indicators
from src.strategy.blueprint import macroscan_4h, check_entry, Signal
from src.risk_manager.calculator import calculate_position
from src.execution.order import place_limit_order
from google.cloud.firestore_v1.base_query import FieldFilter
from src.services.firebase import init_firebase, create_trade, update_trade_status, get_db, get_all_trades, get_trades_summary
from src.services.telegram import send_alert
from src.services.redis_client import init_redis, close_redis, is_cross_detected, mark_cross_detected
from src.execution.monitor import monitor_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

_monitor_task = None
_client = None
CONCURRENCY = 10


def _get_client():
    global _client
    if _client is None:
        _client = create_futures_client()
    return _client


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_firebase()
    await init_redis()
    global _monitor_task
    client = _get_client()
    if not DRY_RUN:
        _monitor_task = asyncio.create_task(monitor_loop(client))
    else:
        logger.info("DRY RUN mode — monitor loop disabled")
    yield
    await close_redis()
    if _monitor_task:
        _monitor_task.cancel()


app = FastAPI(title="Crypto Blueprint Bot", lifespan=lifespan)
templates = Jinja2Templates(directory="src/templates")
app.mount("/static", StaticFiles(directory="src/static"), name="static")


def _fmt_trade(t: dict) -> dict:
    prices = t.get("prices") or {}
    metrics = t.get("metrics") or {}
    ts = t.get("timestamps") or {}
    status = (t.get("status") or "UNKNOWN").lower().replace("_", "-")
    side = t.get("side") or "UNKNOWN"
    sig = (ts.get("signal_generated") or "")[:19].replace("T", " ")
    closed = (ts.get("closed_at") or "")[:19].replace("T", " ")
    entry_raw = prices.get("entry_target", 0) or 0
    sl_raw = prices.get("stop_loss", 0) or 0
    tp_raw = prices.get("take_profit", 0) or 0
    return {
        "id": t.get("trade_id", ""),
        "s": t.get("symbol", "—"),
        "side": side,
        "side_lc": side.lower(),
        "status": t.get("status", "—"),
        "status_class": status,
        "entry": f"{entry_raw:.4f}" if prices.get("entry_target") else "—",
        "entry_raw": entry_raw,
        "sl": f"{sl_raw:.4f}" if prices.get("stop_loss") else "—",
        "sl_raw": sl_raw,
        "tp": f"{tp_raw:.4f}" if prices.get("take_profit") and tp_raw > 0 else "—",
        "tp_raw": tp_raw,
        "qty": f"{metrics.get('qty_coins', 0):.4f}" if metrics.get("qty_coins") else "—",
        "qty_raw": metrics.get("qty_coins", 0) or 0,
        "atr": f"{metrics.get('atr_value', 0):.2f}" if metrics.get("atr_value") else "—",
        "atr_raw": metrics.get("atr_value", 0) or 0,
        "tf": t.get("tf", "—"),
        "signal_time": sig if sig else "—",
        "closed_at": closed if closed else "—",
    }


class CloseRequest(BaseModel):
    close_price: float


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    return _render_page(request, "watchlist")

@app.get("/active", response_class=HTMLResponse)
def active_page(request: Request):
    return _render_page(request, "active")

@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    return _render_page(request, "history")


def _render_page(request: Request, page: str):
    mode = "DRY_RUN" if DRY_RUN else "PRODUCTION"
    trades = get_all_trades()
    summary = get_trades_summary()
    watchlist = [_fmt_trade(t) for t in trades if t.get("status") == "PENDING"]
    active = [_fmt_trade(t) for t in trades if t.get("status") in ("LIMIT_PLACED", "FILLED", "TP1_HIT")]
    closed = [_fmt_trade(t) for t in trades if t.get("status") in ("CLOSED_SL", "CLOSED_TP", "CLOSED_BEP", "EXPIRED_CANCELLED")]
    page_size = 10
    return templates.TemplateResponse(request, "index.html", {
        "mode": mode,
        "mode_class": "production" if mode == "PRODUCTION" else "dry-run",
        "version": "8.0.0",
        "strategy": "EMA7/EMA50 Cross LONG/SHORT (1h/4h)",
        "summary": summary,
        "page": page,
        "watchlist_trades": watchlist[:page_size],
        "active_trades": active[:page_size],
        "closed_trades": closed[:page_size],
        "watchlist_json": json.dumps(watchlist),
        "active_json": json.dumps(active),
        "closed_json": json.dumps(closed),
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC"),
    })


@app.get("/api/trades")
def api_trades():
    trades = get_all_trades()
    summary = get_trades_summary()
    watchlist = [_fmt_trade(t) for t in trades if t.get("status") == "PENDING"]
    active = [_fmt_trade(t) for t in trades if t.get("status") in ("LIMIT_PLACED", "FILLED", "TP1_HIT")]
    closed = [_fmt_trade(t) for t in trades if t.get("status") in ("CLOSED_SL", "CLOSED_TP", "CLOSED_BEP", "EXPIRED_CANCELLED")]
    return {
        "summary": summary,
        "watchlist": watchlist,
        "active": active,
        "closed": closed,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


@app.post("/api/trade/{trade_id}/close")
def api_close_trade(trade_id: str, body: CloseRequest):
    trades = get_all_trades()
    t = next((x for x in trades if x.get("trade_id") == trade_id), None)
    if not t:
        return {"status": "error", "message": "Trade not found"}
    side = t.get("side", "LONG")
    entry = (t.get("prices") or {}).get("entry_target", 0)
    if side == "LONG":
        new_status = "CLOSED_TP" if body.close_price >= entry else "CLOSED_SL"
    else:
        new_status = "CLOSED_TP" if body.close_price <= entry else "CLOSED_SL"
    update_trade_status(trade_id, new_status, **{"prices.close_price": body.close_price})
    return {"status": "ok", "new_status": new_status}


@app.post("/api/trade/{trade_id}/expire")
def api_expire_trade(trade_id: str):
    update_trade_status(trade_id, "EXPIRED_CANCELLED")
    return {"status": "ok"}


@app.post("/api/trade/{trade_id}/skip")
def api_skip_trade(trade_id: str):
    update_trade_status(trade_id, "EXPIRED_CANCELLED")
    return {"status": "ok"}


@app.post("/api/trade/{trade_id}/place-limit")
def api_place_limit(trade_id: str, entry_price: float | None = Query(None)):
    if entry_price:
        update_trade_status(trade_id, "LIMIT_PLACED", **{"prices.entry_target": entry_price})
    else:
        update_trade_status(trade_id, "LIMIT_PLACED")
    return {"status": "ok"}


@app.get("/api/scan")
async def scan(
    timeframe: Annotated[str, Query(description="Entry timeframe (cross detection)")] = "1h",
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

        sem = asyncio.Semaphore(CONCURRENCY)

        async def _detect(sym: str) -> Signal | None:
            async with sem:
                df_macro = await asyncio.to_thread(fetch_klines, client, sym, htf, macro_limit)
                if df_macro is None:
                    return None
                df_macro = compute_indicators(df_macro)
                if df_macro is None:
                    return None

                macro_bias = macroscan_4h(df_macro)
                if macro_bias == "NEUTRAL":
                    return None

                df_entry = await asyncio.to_thread(fetch_klines, client, sym, timeframe, limit)
                if df_entry is None:
                    return None
                df_entry = compute_indicators(df_entry)
                if df_entry is None:
                    return None

                macro_cols = df_macro[["timestamp", "close", "EMA50"]].dropna().rename(
                    columns={"close": "close_4h", "EMA50": "EMA50_4h"})
                df_entry = pd.merge_asof(df_entry, macro_cols, on="timestamp", direction="backward")
                df_entry = df_entry.dropna(subset=["EMA50_4h"])

                if len(df_entry) < 50:
                    return None

                sig = check_entry(
                    df_1h=df_entry,
                    btc_bias=btc_bias.side,
                    symbol=sym,
                    macro_bias=macro_bias,
                    lookback_candles=CROSS_LOOKBACK_CANDLES,
                )

                if sig:
                    if await is_cross_detected(sig.side, sym, sig.cross_candle_ms):
                        logger.debug("Dedup skip: %s %s at %s", sig.side, sym, sig.cross_candle_ms)
                        return None
                    await mark_cross_detected(sig.side, sym, sig.cross_candle_ms)

                return sig

        tasks = [_detect(sym) for sym in liquid_symbols]
        results = await asyncio.gather(*tasks)
        signals: list[Signal] = [r for r in results if r is not None][:TOTAL_SIGNALS]

        results_json = []
        telegram_parts = []
        for sig in signals:
            existing_active = list(get_db().collection("active_trades")
                .where(filter=FieldFilter("symbol", "==", sig.symbol))
                .where(filter=FieldFilter("status", "in", ["PENDING", "LIMIT_PLACED", "FILLED"]))
                .stream())
            if existing_active:
                logger.info(
                    "%s has %d active trade(s) — creating additional entry (trend following)",
                    sig.symbol, len(existing_active),
                )

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
                tp1_explicit=float(sig.tp1_price) if sig.tp1_price > 0 else None,
            )

            if sig.side == "LONG":
                bep = float(spec.entry) * (1 + LONG_ENTRY_FEE_PCT / 100)
            else:
                bep = float(spec.entry) * (1 - SHORT_ENTRY_FEE_PCT / 100)
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
            logger.info(
                "%s | %s\n%s %s\nEntry: %s\nSL: %s (%.2f%%)\nTP1 (50%%): %s (%.2f%%)\nBEP after TP1: %.4f\nQty: %s | TP1 Qty: %s",
                mode_label, status_msg, spec.side, spec.symbol, spec.entry,
                spec.stop_loss, pct_sl, spec.take_profit, pct_tp, bep, spec.quantity, spec.quantity_tp1,
            )
            emoji = "🟢" if spec.side == "LONG" else "🔴"
            telegram_parts.append(
                f"{emoji} {spec.side} ${spec.symbol}\n"
                f"📍 Entry: {spec.entry}\n"
                f"⛔ SL: {spec.stop_loss} ({pct_sl:.2f}%)\n"
                f"💰 TP1 (50%): {spec.take_profit} ({pct_tp:.2f}%)\n"
                f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
            )

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

        if send_telegram and telegram_parts:
            header = (
                "🔔 EMA CROSSOVER\n"
                "Asymmetric Bets (RR 1:1.5)\n"
                "━━━━━━━━━━━━━━━\n"
            )
            await send_alert(header + "\n".join(telegram_parts))

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
