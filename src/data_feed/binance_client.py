import logging
import os
import time

from binance.um_futures import UMFutures

from src.config.settings import (
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    BINANCE_AUTH_ENABLED,
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


def _client_kwargs() -> dict:
    """Constructor arguments for the futures client.

    Klines, exchange info, 24h tickers and server time are public endpoints, so by default the
    client is built without credentials and no key ever reaches the web process. Signed access
    (RSA private key, else HMAC secret) is only wired up when BINANCE_AUTH_ENABLED is set.
    """
    kwargs: dict = {"timeout": 15}
    if not BINANCE_AUTH_ENABLED:
        logger.info("Binance Futures client in public market-data mode (no credentials loaded)")
        return kwargs

    kwargs["key"] = BINANCE_API_KEY or None
    private_key = _load_private_key()
    if private_key:
        logger.info("Using RSA authentication for Binance Futures")
        kwargs["private_key"] = private_key
        kwargs["private_key_passphrase"] = BINANCE_PRIVATE_KEY_PASSPHRASE or None
    else:
        logger.info("Using HMAC authentication for Binance Futures")
        kwargs["secret"] = BINANCE_API_SECRET or None
    return kwargs


def create_futures_client() -> UMFutures:
    client = UMFutures(**_client_kwargs())

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
