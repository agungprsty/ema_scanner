import asyncio
import logging
from datetime import datetime, timezone

from binance.um_futures import UMFutures

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
                if trade.get("status") != "LIMIT_PLACED":
                    continue

                order_id = trade.get("binance_order_id")
                symbol = trade.get("symbol")
                if not order_id or not symbol:
                    continue

                order_status = client.get_order(symbol=symbol, orderId=order_id)
                binance_status = order_status.get("status", "")

                if binance_status == "FILLED":
                    update_trade_status(trade["trade_id"], "FILLED")
                    logger.info("Order FILLED: %s %s", symbol, order_id)
                    continue

                placed_at = trade.get("timestamps", {}).get("order_placed")
                if placed_at:
                    placed_dt = datetime.fromisoformat(placed_at.replace("Z", "+00:00"))
                    elapsed = (now - placed_dt).total_seconds()
                    if elapsed > max_candles * candle_seconds:
                        cancel_order(client, symbol, order_id)
                        update_trade_status(trade["trade_id"], "EXPIRED_CANCELLED")
                        logger.info("Order CANCELLED (timeout): %s %s", symbol, order_id)

        except Exception as e:
            logger.error("monitor_loop error: %s", e)

        await asyncio.sleep(check_interval)
