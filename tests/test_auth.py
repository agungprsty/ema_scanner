import hashlib
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from src import main
from src.api import security
from src.api.exceptions import Forbidden, Unauthorized
from src.api.security import Principal, check_same_origin, get_principal, require_admin

pytestmark = pytest.mark.unit

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
