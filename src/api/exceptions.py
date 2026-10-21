import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api.responses import error

_LOC_PREFIXES = ("body", "query", "path")

logger = logging.getLogger(__name__)


class AppException(Exception):
    code = 400

    def __init__(
        self,
        message: str | None = None,
        code: int | None = None,
        errors: list[dict] | None = None,
        headers: dict | None = None,
    ):
        self.code = code or self.code
        self.message = message or HTTPStatus(self.code).phrase
        self.errors = errors or []
        self.headers = headers
        super().__init__(self.message)


class BadRequest(AppException):
    code = 400


class Unauthorized(AppException):
    code = 401


class Forbidden(AppException):
    code = 403


class NotFound(AppException):
    code = 404


class Conflict(AppException):
    code = 409


class UnprocessableEntity(AppException):
    code = 422


class TooManyRequests(AppException):
    code = 429


class BadGateway(AppException):
    code = 502


class ServiceUnavailable(AppException):
    code = 503


def _field(loc: tuple) -> str:
    parts = [str(p) for p in loc]
    if len(parts) > 1 and parts[0] in _LOC_PREFIXES:
        parts = parts[1:]
    return ".".join(parts)


async def _app_exception(request: Request, exc: AppException):
    if exc.code >= 500:
        logger.error("%s on %s %s", exc.message, request.method, request.url.path, exc_info=exc.__cause__ or exc)
    return error(exc.code, exc.message, exc.errors, exc.headers)


async def _validation_error(request: Request, exc: RequestValidationError):
    errors = [{"field": _field(e["loc"]), "message": e["msg"]} for e in exc.errors()]
    return error(422, "Validation failed", errors)


async def _http_exception(request: Request, exc: StarletteHTTPException):
    message = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
    return error(exc.status_code, message, headers=exc.headers)


def register_exception_handlers(app: FastAPI) -> None:
    # Unhandled exceptions are turned into the 500 envelope by ServerErrorCatcher, see src/api/middleware.py
    app.add_exception_handler(AppException, _app_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
