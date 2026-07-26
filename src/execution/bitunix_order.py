import logging

from src.config.settings import (
    BITUNIX_AUTO_SL,
    BITUNIX_BLUECHIP_LEVERAGE,
    BITUNIX_BLUECHIP_SYMBOLS,
    BITUNIX_DEFAULT_LEVERAGE,
)
from src.execution.bitunix_client import BitunixClient
from src.risk_manager.calculator import OrderSpec

logger = logging.getLogger(__name__)


def _resolve_leverage(symbol: str) -> int:
    if symbol in BITUNIX_BLUECHIP_SYMBOLS:
        return BITUNIX_BLUECHIP_LEVERAGE
    return BITUNIX_DEFAULT_LEVERAGE


async def set_leverage(client: BitunixClient, symbol: str) -> int:
    leverage = _resolve_leverage(symbol)
    try:
        resp = await client.post(
            "/account/change_leverage",
            {"symbol": symbol, "leverage": str(leverage)},
        )
        if resp.get("code") != 0:
            logger.warning("set_leverage %s failed: %s", symbol, resp.get("msg"))
        else:
            logger.info("Leverage set: %s -> %dx", symbol, leverage)
    except Exception as e:
        logger.warning("set_leverage %s: %s", symbol, e)
    return leverage


async def place_limit_order(
    client: BitunixClient,
    spec: OrderSpec,
    *,
    with_sl: bool = True,
) -> dict | None:
    leverage = await set_leverage(client, spec.symbol)

    body: dict = {
        "symbol": spec.symbol,
        "side": "BUY" if spec.side == "LONG" else "SELL",
        "tradeSide": "OPEN",
        "orderType": "LIMIT",
        "price": str(spec.entry),
        "qty": str(spec.quantity),
        "effect": "GTC",
    }

    if with_sl and BITUNIX_AUTO_SL and spec.stop_loss:
        body["slPrice"] = str(spec.stop_loss)
        body["slStopType"] = "MARK_PRICE"
        body["slOrderType"] = "MARKET"

    try:
        resp = await client.post("/trade/place_order", body)
        if resp.get("code") != 0:
            logger.error("Bitunix place_order error: code=%s msg=%s", resp.get("code"), resp.get("msg"))
            return None

        data = resp.get("data", {})
        order_id = data.get("orderId")
        if order_id:
            logger.info(
                "Bitunix ORDER PLACED: %s %s entry=%s qty=%s SL=%s lev=%dx",
                spec.side,
                spec.symbol,
                spec.entry,
                spec.quantity,
                spec.stop_loss if with_sl and BITUNIX_AUTO_SL else "N/A",
                leverage,
            )
            return {"orderId": order_id, "clientId": data.get("clientId"), "leverage": leverage}

        logger.error("Bitunix place_order no orderId: %s", resp)
        return None
    except Exception as e:
        logger.error("Failed to place Bitunix order for %s: %s", spec.symbol, e)
        return None


async def cancel_order(client: BitunixClient, symbol: str, order_id: str) -> dict | None:
    try:
        resp = await client.post(
            "/trade/cancel_orders",
            {"symbol": symbol, "orderList": [{"orderId": order_id}]},
        )
        return resp
    except Exception as e:
        logger.error("Failed to cancel Bitunix order %s/%s: %s", symbol, order_id, e)
        return None


async def get_order_detail(client: BitunixClient, order_id: str) -> dict | None:
    try:
        resp = await client.get("/trade/get_order_detail", {"orderId": order_id})
        return resp.get("data")
    except Exception as e:
        logger.error("Failed to get Bitunix order detail %s: %s", order_id, e)
        return None
