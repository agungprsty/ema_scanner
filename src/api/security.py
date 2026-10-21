import secrets
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.api.exceptions import ServiceUnavailable, Unauthorized
from src.config.settings import API_SECRET_KEY

API_KEY_MIN_LENGTH = 16
_API_KEY_PLACEHOLDERS = frozenset({"your_secret_api_key_here", "changeme", "change_me", "secret", "password"})
# auto_error=False so a missing token gets our 401 envelope instead of FastAPI's 403
bearer_scheme = HTTPBearer(auto_error=False, description="API_SECRET_KEY")


def api_key_configured() -> bool:
    key = API_SECRET_KEY or ""
    return len(key) >= API_KEY_MIN_LENGTH and key.lower() not in _API_KEY_PLACEHOLDERS


async def verify_api_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> bool:
    if not api_key_configured():
        raise ServiceUnavailable("API key not configured on server")
    token = credentials.credentials if credentials else ""
    if not token or not secrets.compare_digest(token.encode("utf-8"), API_SECRET_KEY.encode("utf-8")):
        raise Unauthorized("Invalid or missing API key")
    return True
