"""Dashboard session tokens: stateless, HMAC-signed and expiring, keyed off API_SECRET_KEY.

A token is ``v1.<expires_unix>.<nonce>.<signature>``. The signing key is derived from the API key
rather than being the key itself, so a leaked token says nothing about the key. Rotating
API_SECRET_KEY invalidates every outstanding session; there is no server-side revocation list,
so logging out only clears the cookie in the browser that asked.
"""

import base64
import hashlib
import hmac
import secrets
import time

TOKEN_VERSION = "v1"
_KEY_CONTEXT = b"crossline-session-v1"


def _signing_key(secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), _KEY_CONTEXT, hashlib.sha256).digest()


def _sign(secret: str, payload: str) -> str:
    digest = hmac.new(_signing_key(secret), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def issue_session_token(secret: str, ttl_seconds: int, now: float | None = None) -> tuple[str, int]:
    """A signed token valid for `ttl_seconds`, and its expiry as a unix timestamp."""
    expires = int(now if now is not None else time.time()) + int(ttl_seconds)
    payload = f"{TOKEN_VERSION}.{expires}.{secrets.token_urlsafe(12)}"
    return f"{payload}.{_sign(secret, payload)}", expires


def verify_session_token(token: str | None, secret: str, now: float | None = None) -> bool:
    if not token or not secret or len(token) > 256:
        return False
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != TOKEN_VERSION or not parts[1].isdigit():
        return False
    payload, signature = ".".join(parts[:3]), parts[3]
    if not hmac.compare_digest(signature.encode("ascii", "replace"), _sign(secret, payload).encode("ascii")):
        return False
    return int(parts[1]) > (now if now is not None else time.time())
