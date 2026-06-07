import logging
import os

from binance.um_futures import UMFutures

from src.config.settings import (
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    BINANCE_PRIVATE_KEY_PATH,
    BINANCE_PRIVATE_KEY_PASSPHRASE,
)

logger = logging.getLogger(__name__)


def _load_private_key(path: str) -> str | None:
    """Load private key from file, returning None if file not found."""
    if not os.path.exists(path):
        logger.warning("Private key file not found: %s", path)
        return None
    with open(path) as f:
        return f.read()


def create_futures_client() -> UMFutures:
    private_key = _load_private_key(BINANCE_PRIVATE_KEY_PATH)

    if private_key:
        logger.info(
            "Using RSA authentication with private key: %s",
            BINANCE_PRIVATE_KEY_PATH,
        )
        return UMFutures(
            key=BINANCE_API_KEY,
            private_key=private_key,
            private_key_passphrase=BINANCE_PRIVATE_KEY_PASSPHRASE or None,
        )

    logger.info("Using HMAC authentication")
    return UMFutures(
        key=BINANCE_API_KEY,
        secret=BINANCE_API_SECRET,
    )
