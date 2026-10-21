import hashlib
import secrets
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.api.exceptions import Forbidden, Unauthorized
from src.config.settings import API_SECRET_KEY
from src.services.db import get_session_user

API_KEY_MIN_LENGTH = 16
_API_KEY_PLACEHOLDERS = frozenset({"your_secret_api_key_here", "changeme", "change_me", "secret", "password"})
# auto_error=False so a missing token falls through to the session cookie instead of FastAPI's 403
bearer_scheme = HTTPBearer(auto_error=False, description="API_SECRET_KEY for machine/CI access")


def api_key_configured() -> bool:
    key = API_SECRET_KEY or ""
    return len(key) >= API_KEY_MIN_LENGTH and key.lower() not in _API_KEY_PLACEHOLDERS


@dataclass(frozen=True)
class Principal:
    id: int | None
    username: str
    role: str
    email: str | None = None
    created_at: str | None = None
    via: str = "session"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "username": self.username,
            "role": self.role,
            "created_at": self.created_at,
        }


def check_same_origin(request: Request) -> None:
    """Verify Origin header matches request hostname for CSRF mitigation."""
    origin = request.headers.get("origin")
    if not origin:
        return
    origin_host = urlsplit(origin).hostname
    request_host = request.url.hostname
    if not origin_host or origin_host.lower() != (request_host or "").lower():
        raise Forbidden("Request rejected")


async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> Principal:
    """Authenticate via Bearer API_SECRET_KEY (machine access) or the st_session cookie."""
    if credentials:
        token = credentials.credentials.encode("utf-8")
        if not api_key_configured() or not secrets.compare_digest(token, API_SECRET_KEY.encode("utf-8")):
            raise Unauthorized("Invalid or missing API key")
        return Principal(id=None, username="api-key", role="administrator", via="api_key")

    token = request.cookies.get("st_session")
    if token:
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            check_same_origin(request)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        user = await get_session_user(token_hash)
        if user:
            return Principal(
                id=user["id"],
                username=user["username"],
                role=user["role"],
                email=user.get("email"),
                created_at=user.get("created_at"),
            )

    raise Unauthorized("Authentication required")


async def require_admin(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """Ensure authenticated principal has administrator role."""
    if principal.role != "administrator":
        raise Forbidden("Administrator only")
    return principal
