import logging
import os

from binance.um_futures import UMFutures

from src.config.settings import (
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    BINANCE_PRIVATE_KEY,
    BINANCE_PRIVATE_KEY_PATH,
    BINANCE_PRIVATE_KEY_PASSPHRASE,
    BINANCE_PROXY,
)

logger = logging.getLogger(__name__)


def _load_private_key() -> str | None:
    if BINANCE_PRIVATE_KEY:
        return BINANCE_PRIVATE_KEY
    if BINANCE_PRIVATE_KEY_PATH and os.path.exists(BINANCE_PRIVATE_KEY_PATH):
        with open(BINANCE_PRIVATE_KEY_PATH) as f:
            return f.read()
    return None


def create_futures_client() -> UMFutures:
    private_key = _load_private_key()
    proxies = {"http": BINANCE_PROXY, "https": BINANCE_PROXY} if BINANCE_PROXY else None

    kwargs = {
        "key": BINANCE_API_KEY or None,
    }
    if proxies:
        kwargs["proxies"] = proxies

    if private_key:
        logger.info("Using RSA authentication for Binance Futures")
        kwargs["private_key"] = private_key
        kwargs["private_key_passphrase"] = BINANCE_PRIVATE_KEY_PASSPHRASE or None
        return UMFutures(**kwargs)

    logger.info("Using HMAC authentication for Binance Futures")
    kwargs["secret"] = BINANCE_API_SECRET or None
    return UMFutures(**kwargs)
