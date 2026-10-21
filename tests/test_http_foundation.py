import hashlib
import importlib
import json
import logging
import re
import sys
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Query
from fastapi.responses import HTMLResponse
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from src.api import log, security, setup_http
from src.api.exceptions import AppException, BadGateway, Conflict, NotFound, ServiceUnavailable, Unauthorized
from src.api.log import JsonFormatter, RequestIdFilter, configure_logging, sanitize, sanitize_text
from src.api.rate_limit import MemoryWindow
from src.api.responses import Envelope, paginated, success
from src.config import settings
from src.services import redis_client

pytestmark = pytest.mark.unit

ORIGIN = "https://app.example.com"


class Item(BaseModel):
    name: str
    qty: int


def build_app() -> FastAPI:
    app = FastAPI(title="Simpletrade")
    setup_http(app)

    @app.get("/")
    async def index():
        return HTMLResponse("<h1>hi</h1>")

    @app.get("/api/ok", response_model=Envelope[dict])
    async def ok():
        logging.getLogger("tests.route").info("inside route")
        return success({"a": 1})

    @app.post("/api/items", status_code=201)
    async def create(item: Item, page: Annotated[int, Query(ge=1)] = 1):
        return success(item.model_dump(), message="Created", code=201)

    @app.get("/api/page")
    async def page():
        return paginated([1, 2, 3], total=7, page=2, per_page=3)

    @app.get("/api/missing")
    async def missing():
        raise NotFound("Signal not found")

    @app.get("/api/app-error/{kind}")
    async def app_error(kind: str):
        raise {
            "conflict": Conflict("Cron job is busy"),
            "gateway": BadGateway(),
            "unavailable": ServiceUnavailable("Retry later", headers={"Retry-After": "30"}),
            "custom": AppException("Bad symbol", errors=[{"field": "symbol", "message": "Unknown"}]),
        }[kind]

    @app.get("/api/wrapped")
    async def wrapped():
        try:
            raise RuntimeError("db exploded")
        except RuntimeError as exc:
            raise AppException("Failed to save", code=500) from exc

    @app.get("/api/boom")
    async def boom():
        raise RuntimeError("db password=hunter2 leaked")

    @app.get("/api/cached")
    async def cached():
        return success([], headers={"Cache-Control": "public, max-age=60"})

    @app.post("/api/protected", dependencies=[Depends(security.require_admin)])
    async def protected():
        return success(True)

    @app.get("/api/health")
    async def health():
        return success({"status": "healthy"})

    return app


@pytest.fixture(autouse=True)
def foundation_settings(monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(settings, "CORS_ORIGINS", [ORIGIN])

    async def fake_get_session_user(token_hash):
        if token_hash == hashlib.sha256(b"admin_cookie").hexdigest():
            return {"id": 1, "username": "admin", "role": "administrator", "email": "admin@example.com"}
        return None

    monkeypatch.setattr(security, "get_session_user", fake_get_session_user)


def client_for(app: FastAPI, base_url: str = "http://test") -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url=base_url)


@pytest.fixture
async def client():
    async with client_for(build_app()) as c:
        yield c


def assert_meta(body: dict, path: str):
    meta = body["meta"]
    assert re.fullmatch(r"[0-9a-f]{32}", meta["request_id"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", meta["timestamp"])
    assert meta["path"] == path


async def test_success_envelope(client):
    res = await client.get("/api/ok?x=1")
    body = res.json()
    assert res.status_code == 200
    assert {k: body[k] for k in ("code", "status", "message", "data")} == {
        "code": 200,
        "status": "success",
        "message": "OK",
        "data": {"a": 1},
    }
    assert_meta(body, "/api/ok")
    assert body["meta"]["request_id"] == res.headers["X-Request-ID"]
    assert "errors" not in body


async def test_created_envelope(client):
    res = await client.post("/api/items", json={"name": "x", "qty": 2})
    assert res.status_code == 201
    assert res.json()["code"] == 201
    assert res.json()["message"] == "Created"


async def test_paginated_envelope(client):
    body = (await client.get("/api/page")).json()
    assert body["data"] == [1, 2, 3]
    assert {k: body["meta"][k] for k in ("total_data", "total_pages", "current_page", "per_page")} == {
        "total_data": 7,
        "total_pages": 3,
        "current_page": 2,
        "per_page": 3,
    }
    assert_meta(body, "/api/page")


def test_paginated_zero_total_has_zero_pages():
    assert json.loads(paginated([], 0, 1, 50).body)["meta"]["total_pages"] == 0


async def test_compact_body_keeps_health_grep_working(client):
    assert b'"status":"healthy"' in (await client.get("/api/health")).content


def assert_error(res, code: int, message: str, errors: list | None = None):
    body = res.json()
    assert res.status_code == code
    assert body["code"] == code
    assert body["status"] == "error"
    assert body["message"] == message
    assert body["data"] is None
    assert body["errors"] == (errors or [])
    assert body["meta"]["request_id"] == res.headers["X-Request-ID"]


async def test_unknown_route_and_wrong_method(client):
    assert_error(await client.get("/api/nope"), 404, "Not Found")
    assert_error(await client.delete("/api/ok"), 405, "Method Not Allowed")


async def test_validation_error_field_mapping(client):
    res = await client.post("/api/items?page=0", json={"name": "x", "qty": "many"})
    assert_error(
        res,
        422,
        "Validation failed",
        [
            {"field": "page", "message": "Input should be greater than or equal to 1"},
            {"field": "qty", "message": "Input should be a valid integer, unable to parse string as an integer"},
        ],
    )


async def test_validation_error_missing_body_keeps_body_field(client):
    res = await client.post("/api/items")
    assert res.json()["errors"] == [{"field": "body", "message": "Field required"}]


@pytest.mark.parametrize(
    ("kind", "code", "message", "errors"),
    [
        ("conflict", 409, "Cron job is busy", []),
        ("gateway", 502, "Bad Gateway", []),
        ("unavailable", 503, "Retry later", []),
        ("custom", 400, "Bad symbol", [{"field": "symbol", "message": "Unknown"}]),
    ],
)
async def test_app_exceptions(client, kind, code, message, errors):
    res = await client.get(f"/api/app-error/{kind}")
    assert_error(res, code, message, errors)
    if kind == "unavailable":
        assert res.headers["Retry-After"] == "30"


async def test_not_found_exception(client):
    assert_error(await client.get("/api/missing"), 404, "Signal not found")


async def test_app_exception_5xx_logs_its_cause(client, caplog):
    res = await client.get("/api/wrapped")
    assert_error(res, 500, "Failed to save")
    assert "db exploded" not in res.text
    logged = [r for r in caplog.records if r.levelno == logging.ERROR and r.name == "src.api.exceptions"]
    assert logged and "db exploded" in str(logged[0].exc_info[1])
    assert logged[0].getMessage() == "Failed to save on GET /api/wrapped"


async def test_unhandled_error_is_500_envelope_with_headers(client, caplog):
    res = await client.get("/api/boom", headers={"Origin": ORIGIN})
    assert_error(res, 500, "Internal server error")
    assert "hunter2" not in res.text and "RuntimeError" not in res.text
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Access-Control-Allow-Origin"] == ORIGIN
    logged = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert logged and logged[0].exc_info


async def test_protected_dependency(client):
    res = await client.post("/api/protected")
    assert_error(res, 401, "Authentication required")
    assert res.headers["WWW-Authenticate"] == "Bearer"

    auth_res = await client.post("/api/protected", cookies={"st_session": "admin_cookie"})
    assert auth_res.status_code == 200

    invalid_res = await client.post("/api/protected", cookies={"st_session": "wrong_cookie"})
    assert_error(invalid_res, 401, "Authentication required")


def test_openapi_shows_error_envelope():
    spec = build_app().openapi()
    responses = spec["paths"]["/api/items"]["post"]["responses"]
    assert responses["4XX"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/ErrorEnvelope"}
    assert "422" not in responses
    assert "Envelope_dict_" in spec["components"]["schemas"]


@pytest.mark.parametrize(
    ("sent", "echoed"),
    [("abc-123_X.y", True), ("bad id!", False), ("x" * 65, False), ("", False)],
)
async def test_request_id_header(client, sent, echoed):
    res = await client.get("/api/ok", headers={"X-Request-ID": sent})
    rid = res.headers["X-Request-ID"]
    assert (rid == sent) is echoed
    if not echoed:
        assert re.fullmatch(r"[0-9a-f]{32}", rid)
    assert res.json()["meta"]["request_id"] == rid


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.addFilter(RequestIdFilter())

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def captured():
    handler = _Capture()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield handler.records
    root.removeHandler(handler)
    root.setLevel(old_level)


async def test_request_id_bound_in_logs_and_access_line(client, captured):
    res = await client.get("/api/ok?api_key=zzz", headers={"X-Request-ID": "req-1", "User-Agent": "pytest"})
    assert res.status_code == 200
    route = next(r for r in captured if r.name == "tests.route")
    assert route.request_id == "req-1"
    access = next(r for r in captured if r.name == "src.api.middleware")
    assert access.request_id == "req-1"
    assert (access.method, access.path, access.status, access.user_agent) == ("GET", "/api/ok", 200, "pytest")
    assert isinstance(access.duration_ms, float)
    assert "zzz" not in access.getMessage()

    logging.getLogger("tests.outside").info("no request")
    assert captured[-1].request_id is None


def test_json_formatter_output():
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.getLogger("tests.fmt").makeRecord(
            "tests.fmt",
            logging.WARNING,
            __file__,
            1,
            "sent %s",
            ("token=abc123",),
            sys.exc_info(),
            extra={"symbol": "BTCUSDT", "headers": {"Authorization": "Bearer xyz", "accept": "json"}},
        )
    RequestIdFilter().filter(record)
    out = json.loads(JsonFormatter().format(record))
    assert out["level"] == "WARNING"
    assert out["logger"] == "tests.fmt"
    assert out["message"] == "sent token=[REDACTED]"
    assert out["request_id"] is None
    assert out["symbol"] == "BTCUSDT"
    assert out["headers"] == {"Authorization": "[REDACTED]", "accept": "json"}
    assert "ValueError: boom" in out["exc_info"]
    assert out["timestamp"].endswith("Z")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer abc.def-123", "Authorization: Bearer [REDACTED]"),
        (
            "GET /fapi?symbol=BTC&api_key=k1&signature=s1",
            "GET /fapi?symbol=BTC&api_key=[REDACTED]&signature=[REDACTED]",
        ),
        (
            "https://api.telegram.org/bot12345:AAE-x_yZ/sendMessage",
            "https://api.telegram.org/bot[REDACTED]/sendMessage",
        ),
        ("listen_key=a access_token=b", "listen_key=a access_token=[REDACTED]"),
    ],
)
def test_sanitize_text(text, expected):
    assert sanitize_text(text) == expected


def test_sanitize_redacts_cookie_header():
    headers = {"cookie": "st_session=abc", "Set-Cookie": "st_session=abc; HttpOnly", "accept": "json"}
    assert sanitize(headers) == {"cookie": "[REDACTED]", "Set-Cookie": "[REDACTED]", "accept": "json"}


def test_sanitize_nested_extra():
    assert sanitize({"cfg": {"API_SECRET_KEY": "s", "x-api-key": "k", "items": [{"password": "p", "n": 1}]}}) == {
        "cfg": {
            "API_SECRET_KEY": "[REDACTED]",
            "x-api-key": "[REDACTED]",
            "items": [{"password": "[REDACTED]", "n": 1}],
        }
    }


def test_configure_logging(monkeypatch):
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    monkeypatch.setattr(log.settings, "LOG_FORMAT", "json")
    monkeypatch.setattr(log.settings, "LOG_LEVEL", "WARNING")
    try:
        configure_logging()
        assert len(root.handlers) == 1 and isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.WARNING
        assert logging.getLogger("uvicorn.error").propagate
        assert not logging.getLogger("uvicorn.access").handlers
        assert not logging.getLogger("uvicorn.access").propagate
    finally:
        root.handlers, _ = saved
        root.setLevel(saved[1])


async def test_security_headers_and_csp(client):
    res = await client.get("/")
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert res.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert "camera=()" in res.headers["Permissions-Policy"]
    assert "script-src 'self' https://unpkg.com" in res.headers["Content-Security-Policy"]
    assert "Strict-Transport-Security" not in res.headers
    assert "Cache-Control" not in res.headers

    docs = await client.get("/docs")
    assert "https://cdn.jsdelivr.net" in docs.headers["Content-Security-Policy"]


async def test_cache_control_on_json(client):
    assert (await client.get("/api/ok")).headers["Cache-Control"] == "no-store"
    assert (await client.get("/api/cached")).headers["Cache-Control"] == "public, max-age=60"


async def test_hsts_only_on_https(client):
    spoofed = await client.get("/api/ok", headers={"X-Forwarded-Proto": "https"})
    assert "Strict-Transport-Security" not in spoofed.headers
    async with client_for(build_app(), "https://test") as https_client:
        res = await https_client.get("/api/ok")
    assert res.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"


async def test_cors_preflight_and_error_headers(client):
    res = await client.options(
        "/api/protected",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert res.status_code == 200
    assert res.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert res.headers["Access-Control-Allow-Credentials"] == "true"
    assert "X-Request-ID" in res.headers

    blocked = await client.options(
        "/api/protected", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "TRACE"}
    )
    assert_error(blocked, 400, "Disallowed CORS origin, method")
    assert "X-Request-ID" in blocked.headers and "Access-Control-Allow-Methods" in blocked.headers

    err = await client.get("/api/nope", headers={"Origin": ORIGIN})
    assert err.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "X-Request-ID" in err.headers["Access-Control-Expose-Headers"]


@pytest.mark.parametrize("bad", ["example.com", "https://example.com/", "ftp://x.com", "https://a.com/path"])
def test_settings_reject_invalid_cors_origin(monkeypatch, bad):
    monkeypatch.setenv("CORS_ORIGINS", f"https://ok.example,{bad}")
    try:
        with pytest.raises(ValueError, match="CORS_ORIGINS"):
            importlib.reload(settings)
    finally:
        monkeypatch.undo()
        importlib.reload(settings)


@pytest.mark.parametrize("name", ["RATE_LIMIT_PER_MINUTE", "RATE_LIMIT_WRITE_PER_MINUTE", "RATE_LIMIT_TRIGGER_PER_MINUTE"])
def test_settings_reject_rate_limit_below_one(monkeypatch, name):
    monkeypatch.setenv(name, "0")
    try:
        with pytest.raises(ValueError, match="RATE_LIMIT"):
            importlib.reload(settings)
    finally:
        monkeypatch.undo()
        importlib.reload(settings)


def test_memory_window_slides():
    window = MemoryWindow()
    assert [window.hit("k", 2, 1000, t)[0] for t in (0, 100, 200)] == [True, True, False]
    assert window.hit("k", 2, 1000, 999) == (False, 2, 0)
    # The hit at 0 leaves the window at 1000, freeing one slot
    assert window.hit("k", 2, 1000, 1000) == (True, 2, 100)
    assert window.hit("k", 2, 1000, 1050) == (False, 2, 100)
    assert window.hit("other", 2, 1000, 1050)[0]


def test_memory_window_prunes_idle_keys():
    window = MemoryWindow()
    window.hit("idle", 5, 1000, 0)
    window.hit("active", 5, 1000, 1500)
    window.hit("active", 5, 1000, 2600)
    assert set(window._hits) == {"active"}


@pytest.fixture
def limited_app(monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MINUTE", 3)
    monkeypatch.setattr(settings, "RATE_LIMIT_WRITE_PER_MINUTE", 2)
    monkeypatch.setattr(settings, "RATE_LIMIT_TRIGGER_PER_MINUTE", 1)
    monkeypatch.setattr(redis_client, "_redis", None)
    return build_app()


async def test_rate_limit_memory_fallback(limited_app):
    async with client_for(limited_app) as c:
        allowed = [await c.get("/api/ok", headers={"Origin": ORIGIN}) for _ in range(3)]
        assert [r.headers["X-RateLimit-Remaining"] for r in allowed] == ["2", "1", "0"]
        assert allowed[0].headers["X-RateLimit-Limit"] == "3"

        blocked = await c.get("/api/ok", headers={"Origin": ORIGIN})
        assert_error(blocked, 429, "Too many requests")
        assert 1 <= int(blocked.headers["Retry-After"]) <= 60
        assert blocked.headers["X-RateLimit-Remaining"] == "0"
        assert blocked.headers["Access-Control-Allow-Origin"] == ORIGIN

        # Writes have their own stricter bucket; health and pages outside /api are exempt
        assert (await c.post("/api/protected")).headers["X-RateLimit-Limit"] == "2"
        for _ in range(5):
            assert (await c.get("/api/health")).status_code == 200
            assert (await c.get("/")).status_code == 200


async def test_rate_limit_uses_redis_when_connected(limited_app, monkeypatch):
    calls = []

    class FakeRedis:
        async def eval(self, script, numkeys, key, now_ms, window_ms, limit, member):
            calls.append((key, window_ms, limit))
            # Second call: window full, oldest hit 20s ago so a slot frees in 40s
            return [1, 1, now_ms] if len(calls) == 1 else [0, 3, now_ms - 20_000]

    monkeypatch.setattr(redis_client, "_redis", FakeRedis())
    async with client_for(limited_app) as c:
        first = await c.get("/api/ok")
        second = await c.post("/api/cron/trigger")
    assert first.headers["X-RateLimit-Remaining"] == "2"
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "40"
    assert calls == [("ratelimit:default:127.0.0.1", 60_000, 3), ("ratelimit:trigger:127.0.0.1", 60_000, 1)]


async def test_rate_limit_falls_back_when_redis_errors(limited_app, monkeypatch):
    class BrokenRedis:
        async def eval(self, *args):
            raise ConnectionError("down")

    monkeypatch.setattr(redis_client, "_redis", BrokenRedis())
    async with client_for(limited_app) as c:
        codes = [(await c.post("/api/protected")).status_code for _ in range(3)]
    assert codes == [401, 401, 429]


def test_unauthorized_exception_defaults():
    exc = Unauthorized()
    assert (exc.code, exc.message) == (401, "Unauthorized")
