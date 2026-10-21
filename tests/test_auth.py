import hashlib
import logging
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from src import main
from src.api import security
from src.api.exceptions import Forbidden, Unauthorized
from src.api.security import Principal, api_key_configured, check_same_origin, get_principal, require_admin

pytestmark = pytest.mark.unit

TEST_API_KEY = "test_machine_api_key_123456"
TEST_SESSION_TOKEN = "test_valid_session_token_12345"
TEST_TOKEN_HASH = hashlib.sha256(TEST_SESSION_TOKEN.encode("utf-8")).hexdigest()

ADMIN_USER = {
    "id": 1,
    "email": "admin@example.com",
    "username": "admin",
    "role": "administrator",
    "created_at": "2026-10-21T00:00:00Z",
}

NORMAL_USER = {
    "id": 2,
    "email": "user@example.com",
    "username": "trader",
    "role": "user",
    "created_at": "2026-10-21T00:00:00Z",
}


def _mock_request(
    method: str = "GET",
    headers: dict | None = None,
    cookies: dict | None = None,
    host: str = "testserver",
) -> Request:
    req = MagicMock(spec=Request)
    req.method = method
    req.headers = headers or {}
    req.cookies = cookies or {}
    req.url = MagicMock()
    req.url.hostname = host
    return req


@pytest.fixture(autouse=True)
def mock_db_sessions(monkeypatch):
    sessions = {TEST_TOKEN_HASH: ADMIN_USER}

    async def fake_get_session_user(token_hash: str):
        return sessions.get(token_hash)

    monkeypatch.setattr(security, "get_session_user", fake_get_session_user)
    return sessions


@pytest.mark.asyncio
async def test_get_principal_with_valid_session_cookie():
    req = _mock_request(cookies={"st_session": TEST_SESSION_TOKEN})
    principal = await get_principal(req)

    assert principal.id == 1
    assert principal.username == "admin"
    assert principal.role == "administrator"


@pytest.mark.asyncio
async def test_get_principal_fails_when_no_cookie():
    req = _mock_request(cookies={})
    with pytest.raises(Unauthorized) as exc:
        await get_principal(req)
    assert exc.value.message == "Authentication required"


@pytest.mark.asyncio
async def test_get_principal_fails_when_session_expired_or_invalid():
    req = _mock_request(cookies={"st_session": "invalid_or_expired_token"})
    with pytest.raises(Unauthorized) as exc:
        await get_principal(req)
    assert exc.value.message == "Authentication required"


@pytest.mark.asyncio
async def test_check_same_origin_validation():
    req_ok = _mock_request(headers={"origin": "http://testserver:7860"}, host="testserver")
    check_same_origin(req_ok)  # should not raise

    req_no_origin = _mock_request(headers={}, host="testserver")
    check_same_origin(req_no_origin)  # non-browser requests allowed

    req_bad = _mock_request(headers={"origin": "https://malicious-site.com"}, host="testserver")
    with pytest.raises(Forbidden) as exc:
        check_same_origin(req_bad)
    assert exc.value.message == "Request rejected"


@pytest.mark.asyncio
async def test_require_admin_permits_administrator():
    admin = Principal(id=1, username="admin", role="administrator")
    assert await require_admin(admin) == admin


@pytest.mark.asyncio
async def test_require_admin_forbids_normal_user():
    user = Principal(id=2, username="trader", role="user")
    with pytest.raises(Forbidden) as exc:
        await require_admin(user)
    assert exc.value.message == "Administrator only"


@pytest.mark.asyncio
async def test_protected_route_via_client_requires_cookie():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Without cookie
        resp = await client.post("/api/cron/trigger")
        assert resp.status_code == 401
        assert resp.json()["message"] == "Authentication required"

        # With admin cookie
        resp_auth = await client.post(
            "/api/cron/trigger",
            cookies={"st_session": TEST_SESSION_TOKEN},
            headers={"origin": "http://testserver"},
        )
        assert resp_auth.status_code != 401


@pytest.mark.parametrize(
    ("configured_key", "expected"),
    [("", False), ("your_secret_api_key_here", False), ("x" * 15, False), ("x" * 16, True), (TEST_API_KEY, True)],
)
def test_api_key_configured(monkeypatch, configured_key, expected):
    monkeypatch.setattr(security, "API_SECRET_KEY", configured_key)
    assert api_key_configured() is expected


async def _trigger(headers: dict):
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/api/cron/trigger", headers=headers)


@pytest.mark.asyncio
async def test_bearer_api_key_is_administrator(monkeypatch):
    monkeypatch.setattr(security, "API_SECRET_KEY", TEST_API_KEY)
    principal = await get_principal(_mock_request(), security.HTTPAuthorizationCredentials(scheme="Bearer", credentials=TEST_API_KEY))
    assert principal == Principal(id=None, username="api-key", role="administrator", via="api_key")

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {TEST_API_KEY}"})
    assert me.status_code == 200
    assert me.json()["data"]["id"] is None
    assert me.json()["data"]["username"] == "api-key"
    assert me.json()["data"]["role"] == "administrator"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_key", "sent"),
    [(TEST_API_KEY, "wrong_key"), ("", "anything"), ("your_secret_api_key_here", "your_secret_api_key_here")],
)
async def test_bad_or_unconfigured_api_key_returns_401(monkeypatch, configured_key, sent):
    monkeypatch.setattr(security, "API_SECRET_KEY", configured_key)
    resp = await _trigger({"Authorization": f"Bearer {sent}"})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json()["message"] == "Invalid or missing API key"


@pytest.mark.asyncio
@pytest.mark.parametrize(("configured_key", "warns"), [("", True), ("changeme", True), (TEST_API_KEY, False)])
async def test_lifespan_warns_when_api_key_unset(monkeypatch, caplog, configured_key, warns):
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(security, "API_SECRET_KEY", configured_key)
    for name in ("init_db", "init_redis", "close_redis", "close_db"):
        monkeypatch.setattr(main, name, noop)
    monkeypatch.setattr(main.cron_scheduler, "start", lambda *args, **kwargs: None)
    monkeypatch.setattr(main.cron_scheduler, "stop", lambda *args, **kwargs: None)

    with caplog.at_level(logging.WARNING, logger="src.main"):
        async with main.lifespan(main.app):
            pass
    messages = [r.getMessage() for r in caplog.records if "API_SECRET_KEY" in r.getMessage()]
    assert bool(messages) is warns
    assert all("machine access via Bearer is disabled" in m for m in messages)
