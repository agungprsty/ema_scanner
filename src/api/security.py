import hashlib
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request

from src.api.exceptions import Forbidden, Unauthorized
from src.services.db import get_session_user


@dataclass(frozen=True)
class Principal:
    id: int
    username: str
    role: str
    via: str = "session"
    email: str | None = None
    created_at: str | None = None

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


async def get_principal(request: Request) -> Principal:
    """Authenticate via st_session cookie."""
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
                via="session",
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
