import logging

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src import main
from src.main import verify_api_key

pytestmark = pytest.mark.unit

TEST_API_KEY = "unit_test_secret_key"


@pytest.fixture(autouse=True)
def api_secret_key(monkeypatch):
    monkeypatch.setattr(main, "API_SECRET_KEY", TEST_API_KEY)
    return TEST_API_KEY


def _request(method: str = "POST", headers: dict | None = None) -> Request:
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": method, "path": "/", "headers": raw})


@pytest.mark.asyncio
async def test_verify_api_key_accepts_x_api_key():
    assert await verify_api_key(_request(), x_api_key=TEST_API_KEY, authorization=None) is True


@pytest.mark.asyncio
async def test_verify_api_key_accepts_bearer_token():
    assert await verify_api_key(_request(), x_api_key=None, authorization=f"Bearer {TEST_API_KEY}") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("x_api_key", "authorization"),
    [
        (None, None),
        ("", None),
        ("wrong_key", None),
        (None, "Bearer wrong_key"),
        (None, "Bearer "),
        (None, TEST_API_KEY),
        ("ünïcode", None),
    ],
)
async def test_verify_api_key_rejects_invalid_token(x_api_key, authorization):
    with pytest.raises(HTTPException) as exc:
        await verify_api_key(_request(), x_api_key=x_api_key, authorization=authorization)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("x_api_key", [None, "", "anything"])
async def test_verify_api_key_fails_closed_when_unset(monkeypatch, x_api_key):
    monkeypatch.setattr(main, "API_SECRET_KEY", "")
    with pytest.raises(HTTPException) as exc:
        await verify_api_key(_request(), x_api_key=x_api_key, authorization=None)
    assert exc.value.status_code == 503
    assert exc.value.detail == "API key not configured on server"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured_key",
    ["your_secret_api_key_here", "YOUR_SECRET_API_KEY_HERE", "changeme", "secret", "a", "x" * 15],
)
async def test_verify_api_key_fails_closed_for_placeholder_or_short_key(monkeypatch, configured_key):
    monkeypatch.setattr(main, "API_SECRET_KEY", configured_key)
    with pytest.raises(HTTPException) as exc:
        await verify_api_key(_request(), x_api_key=configured_key, authorization=None)
    assert exc.value.status_code == 503
    assert exc.value.detail == "API key not configured on server"


@pytest.mark.asyncio
async def test_verify_api_key_accepts_minimum_length_key(monkeypatch):
    key = "k" * 16
    monkeypatch.setattr(main, "API_SECRET_KEY", key)
    assert await verify_api_key(_request(), x_api_key=key, authorization=None) is True


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
    monkeypatch.setattr(main, "API_SECRET_KEY", configured_key)
    assert main._api_key_configured() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_key", ["", "your_secret_api_key_here", "a"])
async def test_lifespan_warns_when_api_key_unset(monkeypatch, caplog, configured_key):
    monkeypatch.setattr(main, "API_SECRET_KEY", configured_key)

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

    assert any(
        r.levelno == logging.WARNING and "API_SECRET_KEY" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_lifespan_does_not_warn_when_api_key_configured(monkeypatch, caplog):
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

    assert not any("API_SECRET_KEY" in r.getMessage() for r in caplog.records)
