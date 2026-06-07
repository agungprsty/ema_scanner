import logging
import os

from binance.um_futures import UMFutures

from src.config.settings import (
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    BINANCE_PRIVATE_KEY,
    BINANCE_PRIVATE_KEY_PATH,
    BINANCE_PRIVATE_KEY_PASSPHRASE,
)

logger = logging.getLogger(__name__)


def _load_private_key() -> str | None:
    if BINANCE_PRIVATE_KEY:
        return BINANCE_PRIVATE_KEY
    if BINANCE_PRIVATE_KEY_PATH and os.path.exists(BINANCE_PRIVATE_KEY_PATH):
        with open(BINANCE_PRIVATE_KEY_PATH) as f:
            return f.read()
    logger.warning("No private key found (BINANCE_PRIVATE_KEY or BINANCE_PRIVATE_KEY_PATH)")
    return None


def create_futures_client() -> UMFutures:
    private_key = _load_private_key()

    kwargs = dict(enable_server_time=True)
    if private_key:
        logger.info("Using RSA authentication")
        return UMFutures(
            key=BINANCE_API_KEY,
            private_key=private_key,
            private_key_passphrase=BINANCE_PRIVATE_KEY_PASSPHRASE or None,
            **kwargs,
        )

    logger.info("Using HMAC authentication")
    return UMFutures(
        key=BINANCE_API_KEY,
        secret=BINANCE_API_SECRET,
        **kwargs,
    )
