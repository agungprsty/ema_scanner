from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers
from starlette.responses import Response

from src.api.exceptions import register_exception_handlers
from src.api.middleware import RequestContextMiddleware, ServerErrorCatcher
from src.api.rate_limit import RateLimitMiddleware
from src.api.responses import ErrorEnvelope, error
from src.config import settings

_ERROR_RESPONSE = {"model": ErrorEnvelope, "description": "Error envelope"}


class EnvelopeCORSMiddleware(CORSMiddleware):
    """Rejected preflights get the error envelope instead of Starlette's plain-text 400."""

    def preflight_response(self, request_headers: Headers) -> Response:
        response = super().preflight_response(request_headers)
        if response.status_code == 200:
            return response
        headers = {k: v for k, v in response.headers.items() if k not in ("content-length", "content-type")}
        return error(response.status_code, response.body.decode(), headers=headers)


def setup_http(app: FastAPI) -> None:
    """Install envelopes, exception handlers and middleware. Call right after FastAPI(), before declaring routes."""
    # Router-level responses only reach routes declared afterwards; "4XX" also replaces FastAPI's default 422 schema
    app.router.responses.update({"4XX": _ERROR_RESPONSE, "5XX": _ERROR_RESPONSE})
    register_exception_handlers(app)

    # add_middleware wraps outward: request context -> CORS -> 500 catcher -> rate limit -> routes.
    # CORS sits outside the catcher and the limiter so 500 and 429 envelopes carry CORS headers,
    # and inside the request context so preflight responses still get X-Request-ID and security headers.
    if settings.RATE_LIMIT_ENABLED:
        app.add_middleware(
            RateLimitMiddleware,
            per_minute=settings.RATE_LIMIT_PER_MINUTE,
            write_per_minute=settings.RATE_LIMIT_WRITE_PER_MINUTE,
            trigger_per_minute=settings.RATE_LIMIT_TRIGGER_PER_MINUTE,
            auth_per_minute=settings.RATE_LIMIT_AUTH_PER_MINUTE,
        )
    app.add_middleware(ServerErrorCatcher)
    app.add_middleware(
        EnvelopeCORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        # Browsers refuse credentials with a wildcard origin
        allow_credentials="*" not in settings.CORS_ORIGINS,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=[
            "X-Request-ID",
            "Retry-After",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        ],
    )
    app.add_middleware(RequestContextMiddleware)
