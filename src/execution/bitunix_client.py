import hashlib
import json
import logging
import time
import uuid
from urllib.parse import urlencode

import httpx

from src.config.settings import BITUNIX_API_KEY, BITUNIX_API_SECRET, BITUNIX_BASE_URL

logger = logging.getLogger(__name__)


class BitunixClient:
    PREFIX = "/api/v1/futures"

    def __init__(self, api_key: str, api_secret: str, base_url: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.client = httpx.AsyncClient(base_url=base_url, timeout=10.0)

    def _sign(self, body: str, query_params: str = "") -> dict:
        nonce = uuid.uuid4().hex[:32]
        timestamp = str(int(time.time() * 1000))
        digest_input = nonce + timestamp + self.api_key + query_params + body
        digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
        sign_input = digest + self.api_secret
        sign = hashlib.sha256(sign_input.encode("utf-8")).hexdigest()
        return {
            "api-key": self.api_key,
            "nonce": nonce,
            "timestamp": timestamp,
            "sign": sign,
        }

    async def post(self, endpoint: str, body: dict) -> dict:
        body_str = json.dumps(body, separators=(",", ":"))
        headers = self._sign(body_str)
        headers["Content-Type"] = "application/json"
        resp = await self.client.post(
            f"{self.PREFIX}{endpoint}", content=body_str, headers=headers
        )
        resp.raise_for_status()
        return resp.json()

    async def get(self, endpoint: str, params: dict | None = None) -> dict:
        params = params or {}
        query = urlencode(sorted(params.items()))
        headers = self._sign("", query)
        resp = await self.client.get(
            f"{self.PREFIX}{endpoint}", params=params, headers=headers
        )
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        await self.client.aclose()


def create_bitunix_client() -> BitunixClient | None:
    if not BITUNIX_API_KEY or not BITUNIX_API_SECRET:
        logger.warning("Bitunix credentials not configured")
        return None
    return BitunixClient(
        api_key=BITUNIX_API_KEY,
        api_secret=BITUNIX_API_SECRET,
        base_url=BITUNIX_BASE_URL,
    )
