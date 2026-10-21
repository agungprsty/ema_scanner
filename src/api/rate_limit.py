import math
import time
from collections import deque

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.responses import error
from src.services.redis_client import sliding_window_hit

WINDOW_MS = 60_000
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_EXEMPT_PATHS = frozenset({"/api/health"})
TRIGGER_PATH = "/api/cron/trigger"
AUTH_PATHS = frozenset({"/api/auth/login", "/api/auth/register"})


class MemoryWindow:
    """Sliding-window log in process memory, used while Redis is unavailable.

    shortcut: counts per worker, so N uvicorn workers allow N x the limit, move to a shared store if Redis outages matter with several workers.
    """

    def __init__(self):
        self._hits: dict[str, deque[int]] = {}
        self._next_prune = 0

    def hit(self, key: str, limit: int, window_ms: int, now_ms: int) -> tuple[bool, int, int]:
        cutoff = now_ms - window_ms
        if now_ms >= self._next_prune:
            # Drop idle clients so memory stays bounded by the clients seen in one window
            self._hits = {k: q for k, q in self._hits.items() if q and q[-1] > cutoff}
            self._next_prune = now_ms + window_ms
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= cutoff:
            hits.popleft()
        allowed = len(hits) < limit
        if allowed:
            hits.append(now_ms)
        return allowed, len(hits), hits[0]


class RateLimitMiddleware:
    """Per client IP limits on /api/*: one bucket for auth, one for cron trigger, one for writes, one for everything else."""

    def __init__(
        self,
        app: ASGIApp,
        per_minute: int,
        write_per_minute: int,
        trigger_per_minute: int,
        auth_per_minute: int = 5,
    ):
        self.app = app
        self.limits = {
            "default": per_minute,
            "write": write_per_minute,
            "trigger": trigger_per_minute,
            "auth": auth_per_minute,
        }
        self.memory = MemoryWindow()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/") or path in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        if path in AUTH_PATHS:
            bucket = "auth"
        elif path == TRIGGER_PATH:
            bucket = "trigger"
        elif scope["method"] in _WRITE_METHODS:
            bucket = "write"
        else:
            bucket = "default"
        limit = self.limits[bucket]
        client_ip = scope["client"][0] if scope.get("client") else "unknown"
        key = f"ratelimit:{bucket}:{client_ip}"
        now_ms = int(time.time() * 1000)
        result = await sliding_window_hit(key, limit, WINDOW_MS, now_ms)
        allowed, count, oldest_ms = result or self.memory.hit(key, limit, WINDOW_MS, now_ms)

        # Seconds until the oldest hit leaves the window and frees a slot
        reset = max(1, math.ceil((oldest_ms + WINDOW_MS - now_ms) / 1000))
        headers = {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(max(0, limit - count)),
            "X-RateLimit-Reset": str(reset),
        }
        if not allowed:
            await error(429, "Too many requests", headers={**headers, "Retry-After": str(reset)})(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                MutableHeaders(scope=message).update(headers)
            await send(message)

        await self.app(scope, receive, send_wrapper)
