import asyncio
import logging
from datetime import datetime, timezone

from binance.um_futures import UMFutures

from src.config.settings import LONG_ENTRY_FEE_PCT
from src.execution.order import cancel_order
from src.services.firebase import get_active_trades, update_trade_status

logger = logging.getLogger(__name__)


async def monitor_loop(
    client: UMFutures,
    check_interval: int = 60,
    max_candles: int = 5,
    candle_seconds: int = 3600,
) -> None:
    while True:
        try:
            trades = get_active_trades()
            now = datetime.now(timezone.utc)

            for trade in trades:
                status = trade.get("status")

                if status == "LIMIT_PLACED":
                    await _check_order_fill(client, trade, now, max_candles, candle_seconds)

                elif status == "FILLED":
                    await _check_tp1_hit(client, trade)

                elif status == "TP1_HIT":
                    await _check_bep_sl(client, trade)

        except Exception as e:
            logger.error("monitor_loop error: %s", e)

        await asyncio.sleep(check_interval)


async def _check_order_fill(
    client: UMFutures,
    trade: dict,
    now: datetime,
    max_candles: int,
    candle_seconds: int,
) -> None:
    order_id = trade.get("binance_order_id")
    symbol = trade.get("symbol")
    if not order_id or not symbol:
        return

    order_status = client.get_order(symbol=symbol, orderId=order_id)
    binance_status = order_status.get("status", "")

    if binance_status == "FILLED":
        update_trade_status(trade["trade_id"], "FILLED")
        logger.info("Order FILLED: %s %s", symbol, order_id)
        return

    placed_at = trade.get("timestamps", {}).get("order_placed")
    if placed_at:
        placed_dt = datetime.fromisoformat(placed_at.replace("Z", "+00:00"))
        elapsed = (now - placed_dt).total_seconds()
        if elapsed > max_candles * candle_seconds:
            cancel_order(client, symbol, order_id)
            update_trade_status(trade["trade_id"], "EXPIRED_CANCELLED")
            logger.info("Order CANCELLED (timeout): %s %s", symbol, order_id)


async def _check_tp1_hit(
    client: UMFutures,
    trade: dict,
) -> None:
    symbol = trade.get("symbol")
    tp1 = trade.get("prices", {}).get("take_profit")
    if not symbol or not tp1:
        return

    ticker = client.ticker_price(symbol=symbol)
    current_price = float(ticker.get("price", 0))
    if current_price <= 0:
        return

    # TP1 hit if price >= TP1 level (for LONG)
    side = trade.get("side", "LONG")
    tp1_hit = (side == "LONG" and current_price >= tp1) or (side == "SHORT" and current_price <= tp1)

    if tp1_hit:
        entry = trade.get("prices", {}).get("entry_target", 0)
        bep = entry * (1 + LONG_ENTRY_FEE_PCT / 100)
        update_trade_status(
            trade["trade_id"],
            "TP1_HIT",
            **{"prices.bep": bep, "prices.stop_loss": bep},
        )
        logger.info("TP1 HIT: %s at %.2f, SL moved to BEP %.4f", symbol, current_price, bep)


async def _check_bep_sl(
    client: UMFutures,
    trade: dict,
) -> None:
    symbol = trade.get("symbol")
    bep = trade.get("prices", {}).get("bep", 0)
    if not symbol or bep <= 0:
        return

    ticker = client.ticker_price(symbol=symbol)
    current_price = float(ticker.get("price", 0))
    if current_price <= 0:
        return

    side = trade.get("side", "LONG")
    bep_hit = (side == "LONG" and current_price <= bep) or (side == "SHORT" and current_price >= bep)

    if bep_hit:
        update_trade_status(trade["trade_id"], "CLOSED_BEP")
        logger.info("BEP HIT: %s at %.2f, position closed", symbol, current_price)
