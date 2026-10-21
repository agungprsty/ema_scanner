import math
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.api.context import request_id_var, request_path_var


class Meta(BaseModel):
    request_id: str | None
    timestamp: str
    path: str | None


class PageMeta(Meta):
    total_data: int
    total_pages: int
    current_page: int
    per_page: int


class ErrorItem(BaseModel):
    field: str | None
    message: str


class Envelope[T](BaseModel):
    code: int
    status: Literal["success"]
    message: str
    data: T
    meta: Meta


class PageEnvelope[T](BaseModel):
    code: int
    status: Literal["success"]
    message: str
    data: list[T]
    meta: PageMeta


class ErrorEnvelope(BaseModel):
    code: int
    status: Literal["error"]
    message: str
    data: None = None
    errors: list[ErrorItem]
    meta: Meta


def _meta(**extra) -> dict:
    timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {"request_id": request_id_var.get(), "timestamp": timestamp, "path": request_path_var.get(), **extra}


def _envelope(code: int, message: str, data: Any, meta: dict, headers: dict | None, **extra) -> JSONResponse:
    body = {"code": code, "status": "success" if code < 400 else "error", "message": message, "data": data, **extra}
    return JSONResponse(jsonable_encoder({**body, "meta": meta}), status_code=code, headers=headers)


def success(data: Any = None, message: str = "OK", code: int = 200, headers: dict | None = None) -> JSONResponse:
    return _envelope(code, message, data, _meta(), headers)


def paginated(items: list, total: int, page: int, per_page: int, message: str = "OK") -> JSONResponse:
    meta = _meta(total_data=total, total_pages=math.ceil(total / per_page), current_page=page, per_page=per_page)
    return _envelope(200, message, items, meta, None)


def error(code: int, message: str, errors: list[dict] | None = None, headers: dict | None = None) -> JSONResponse:
    if code == 401:
        headers = {"WWW-Authenticate": "Bearer", **(headers or {})}
    return _envelope(code, message, None, _meta(), headers, errors=errors or [])
