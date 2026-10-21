import logging

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport, AsyncClient

from src import main
from src.api import security
from src.api.exceptions import ServiceUnavailable, Unauthorized
from src.api.security import api_key_configured, verify_api_key

pytestmark = pytest.mark.unit

TEST_API_KEY = "unit_test_secret_key"


@pytest.fixture(autouse=True)
def api_secret_key(monkeypatch):
    monkeypatch.setattr(security, "API_SECRET_KEY", TEST_API_KEY)
    return TEST_API_KEY


def bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.mark.asyncio
async def test_verify_api_key_accepts_bearer_token():
    assert await verify_api_key(bearer(TEST_API_KEY)) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("credentials", [None, bearer(""), bearer("wrong_key"), bearer("ünïcode")])
async def test_verify_api_key_rejects_invalid_token(credentials):
    with pytest.raises(Unauthorized):
        await verify_api_key(credentials)


@pytest.mark.asyncio
@pytest.mark.parametrize("credentials", [None, bearer(""), bearer("anything")])
async def test_verify_api_key_fails_closed_when_unset(monkeypatch, credentials):
    monkeypatch.setattr(security, "API_SECRET_KEY", "")
    with pytest.raises(ServiceUnavailable) as exc:
        await verify_api_key(credentials)
    assert exc.value.message == "API key not configured on server"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured_key",
    ["your_secret_api_key_here", "YOUR_SECRET_API_KEY_HERE", "changeme", "secret", "a", "x" * 15],
)
async def test_verify_api_key_fails_closed_for_placeholder_or_short_key(monkeypatch, configured_key):
    monkeypatch.setattr(security, "API_SECRET_KEY", configured_key)
    with pytest.raises(ServiceUnavailable):
        await verify_api_key(bearer(configured_key))


@pytest.mark.asyncio
async def test_verify_api_key_accepts_minimum_length_key(monkeypatch):
    key = "k" * 16
    monkeypatch.setattr(security, "API_SECRET_KEY", key)
    assert await verify_api_key(bearer(key)) is True


@pytest.mark.parametrize(
    ("configured_key", "expected"),
    [
        ("", False),
        ("your_secret_api_key_here", False),
        ("changeme", False),
        ("short", False),
        ("x" * 15, False),
        ("x" * 16, True),
        (TEST_API_KEY, True),
    ],
)
def test_api_key_configured(monkeypatch, configured_key, expected):
    monkeypatch.setattr(security, "API_SECRET_KEY", configured_key)
    assert api_key_configured() is expected


async def _trigger(headers: dict):
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/cron/trigger", headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong_key"},
        {"Authorization": TEST_API_KEY},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"X-API-KEY": TEST_API_KEY},  # the old header is no longer accepted
    ],
)
async def test_protected_route_returns_401_envelope(headers):
    resp = await _trigger(headers)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    body = resp.json()
    assert body["status"] == "error" and body["code"] == 401
    assert body["message"] == "Invalid or missing API key"
    assert body["data"] is None and body["errors"] == []


@pytest.mark.asyncio
async def test_protected_route_returns_503_when_unconfigured(monkeypatch):
    monkeypatch.setattr(security, "API_SECRET_KEY", "")
    resp = await _trigger({"Authorization": "Bearer anything"})
    assert resp.status_code == 503
    assert resp.json()["message"] == "API key not configured on server"


async def _run_lifespan(monkeypatch, caplog):
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "init_db", noop)
    monkeypatch.setattr(main, "init_redis", noop)
    monkeypatch.setattr(main, "close_redis", noop)
    monkeypatch.setattr(main, "close_db", noop)
    monkeypatch.setattr(main.cron_scheduler, "start", lambda *args, **kwargs: None)
    monkeypatch.setattr(main.cron_scheduler, "stop", lambda *args, **kwargs: None)

    with caplog.at_level(logging.WARNING, logger="src.main"):
        async with main.lifespan(main.app):
            pass
    return [r for r in caplog.records if r.levelno == logging.WARNING and "API_SECRET_KEY" in r.getMessage()]


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_key", ["", "your_secret_api_key_here", "a"])
async def test_lifespan_warns_when_api_key_unset(monkeypatch, caplog, configured_key):
    monkeypatch.setattr(security, "API_SECRET_KEY", configured_key)
    assert await _run_lifespan(monkeypatch, caplog)


@pytest.mark.asyncio
async def test_lifespan_does_not_warn_when_api_key_configured(monkeypatch, caplog):
    assert not await _run_lifespan(monkeypatch, caplog)
