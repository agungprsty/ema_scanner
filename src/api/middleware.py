import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.context import request_id_var, request_path_var
from src.api.responses import error

logger = logging.getLogger(__name__)

_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
HSTS = "max-age=31536000; includeSubDomains"
# index.html: inline <style> + style attributes, Google Fonts, lightweight-charts loaded on demand from unpkg
APP_CSP = (
    "default-src 'self'; script-src 'self' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'; object-src 'none'"
)
# Swagger UI / ReDoc pages: inline bootstrap script, assets from jsDelivr, ReDoc web worker from blob:
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data: https:; worker-src blob:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)


class RequestContextMiddleware:
    """Outermost layer: binds request_id, sets security headers on every response and writes the access log."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get("x-request-id", "")
        if not _REQUEST_ID.fullmatch(request_id):
            request_id = uuid.uuid4().hex
        path = scope["path"]
        csp = DOCS_CSP if path.startswith(("/docs", "/redoc")) else APP_CSP
        # uvicorn --proxy-headers rewrites the scheme from X-Forwarded-Proto only for FORWARDED_ALLOW_IPS peers
        https = scope.get("scheme") == "https"
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                out = MutableHeaders(scope=message)
                out["X-Request-ID"] = request_id
                for name, value in SECURITY_HEADERS.items():
                    out.setdefault(name, value)
                out.setdefault("Content-Security-Policy", csp)
                if https:
                    out.setdefault("Strict-Transport-Security", HSTS)
                if out.get("content-type", "").startswith("application/json"):
                    out.setdefault("Cache-Control", "no-store")
            await send(message)

        id_token, path_token = request_id_var.set(request_id), request_path_var.set(path)
        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            logger.info(
                "%s %s %d %.1fms",
                scope["method"],
                path,
                status,
                duration_ms,
                extra={
                    "method": scope["method"],
                    "path": path,
                    "status": status,
                    "duration_ms": duration_ms,
                    "client_ip": scope["client"][0] if scope.get("client") else None,
                    "user_agent": headers.get("user-agent"),
                },
            )
            request_id_var.reset(id_token)
            request_path_var.reset(path_token)


class ServerErrorCatcher:
    """Turns unhandled exceptions into the 500 envelope inside CORS, so the response keeps CORS and security headers.

    Starlette's own Exception handler runs in ServerErrorMiddleware, outside every user middleware.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("Unhandled error on %s %s", scope["method"], scope["path"])
            if started:
                raise
            await error(500, "Internal server error")(scope, receive, send)
