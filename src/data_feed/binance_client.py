import logging
import os
import time

from binance.um_futures import UMFutures

from src.config.settings import (
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    BINANCE_PRIVATE_KEY,
    BINANCE_PRIVATE_KEY_PASSPHRASE,
    BINANCE_PRIVATE_KEY_PATH,
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

    kwargs = {
        "key": BINANCE_API_KEY or None,
        "timeout": 15,
    }

    if private_key:
        logger.info("Using RSA authentication for Binance Futures")
        kwargs["private_key"] = private_key
        kwargs["private_key_passphrase"] = BINANCE_PRIVATE_KEY_PASSPHRASE or None
        client = UMFutures(**kwargs)
    else:
        logger.info("Using HMAC authentication for Binance Futures")
        kwargs["secret"] = BINANCE_API_SECRET or None
        client = UMFutures(**kwargs)

    try:
        t_data = client.time()
        server_time = t_data.get("serverTime")
        if server_time:
            local_time = int(time.time() * 1000)
            diff = abs(server_time - local_time)
            if diff > 1000:
                logger.warning("Clock drift detected with Binance server: %d ms", diff)
            else:
                logger.info("Binance Futures client connected (time skew: %d ms)", diff)
    except Exception as e:
        logger.warning("Binance server time check warning: %s", e)

    return client
