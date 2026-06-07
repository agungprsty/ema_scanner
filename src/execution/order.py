import logging
from binance.um_futures import UMFutures
from src.config.settings import LEVERAGE
from src.risk_manager.calculator import OrderSpec

logger = logging.getLogger(__name__)


def set_leverage(client: UMFutures, symbol: str, leverage: int = LEVERAGE) -> None:
    try:
        client.change_leverage(symbol=symbol, leverage=leverage)
    except Exception as e:
        logger.warning("set_leverage %s: %s", symbol, e)


def place_limit_order(
    client: UMFutures,
    spec: OrderSpec,
) -> dict | None:
    try:
        set_leverage(client, spec.symbol)
        resp = client.new_order(
            symbol=spec.symbol,
            side="BUY" if spec.side == "LONG" else "SELL",
            type="LIMIT",
            timeInForce="GTC",
            quantity=str(spec.quantity),
            price=str(spec.entry),
        )
        return resp
    except Exception as e:
        logger.error("Failed to place order for %s: %s", spec.symbol, e)
        return None


def cancel_order(client: UMFutures, symbol: str, order_id: int) -> dict | None:
    try:
        resp = client.cancel_order(symbol=symbol, orderId=order_id)
        return resp
    except Exception as e:
        logger.error("Failed to cancel order %s/%s: %s", symbol, order_id, e)
        return None
