import pytest
from httpx import ASGITransport, AsyncClient

from src import main
from src.services import auth, db

pytestmark = pytest.mark.unit

TEST_API_KEY = "session_test_secret_key"
CSRF = {"X-Requested-With": "crossline"}
SECURE_COOKIE = "__Host-crossline_session"


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(main, "API_SECRET_KEY", TEST_API_KEY)
    monkeypatch.setattr(main, "COOKIE_SECURE", True)
    monkeypatch.setattr(main, "SESSION_TTL_SECONDS", 3600)


@pytest.fixture(autouse=True)
def fake_services(monkeypatch):
    added = []

    async def fake_add(symbol: str, notes: str = ""):
        added.append(symbol)
        return {"symbol": symbol, "is_active": True, "notes": notes}

    async def fake_get_journal(status=None, symbol=None, limit=50, offset=0):
        return [], 0, db._empty_journal_summary()

    async def fake_get_signals(**kwargs):
        return [{"id": 7, "symbol": "BTCUSDT", "journal_status": "TAKEN"}], 1, {}

    async def fake_get_signal(signal_id):
        return {"id": signal_id, "symbol": "BTCUSDT", "journal": {"status": "TAKEN", "notes": "private"}}

    monkeypatch.setattr(main, "add_watchlist_symbol", fake_add)
    monkeypatch.setattr(main, "get_journal", fake_get_journal)
    monkeypatch.setattr(main, "get_macd_signals", fake_get_signals)
    monkeypatch.setattr(main, "get_signal_by_id", fake_get_signal)
    return added


def _client(base_url: str = "https://test") -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=main.app), base_url=base_url)


async def _login(client: AsyncClient, key: str = TEST_API_KEY):
    return await client.post("/api/auth/login", json={"api_key": key}, headers=CSRF)


# Token signing
def test_token_round_trip():
    token, expires = auth.issue_session_token(TEST_API_KEY, 60, now=1_000)
    assert expires == 1_060
    assert auth.verify_session_token(token, TEST_API_KEY, now=1_059)
    assert TEST_API_KEY not in token


def test_token_expires():
    token, _ = auth.issue_session_token(TEST_API_KEY, 60, now=1_000)
    assert not auth.verify_session_token(token, TEST_API_KEY, now=1_060)
    assert not auth.verify_session_token(token, TEST_API_KEY, now=5_000)


def test_tokens_are_unique():
    first, _ = auth.issue_session_token(TEST_API_KEY, 60, now=1_000)
    second, _ = auth.issue_session_token(TEST_API_KEY, 60, now=1_000)
    assert first != second


@pytest.mark.parametrize(
    "tamper",
    [
        lambda t: t[:-1] + ("A" if t[-1] != "A" else "B"),  # signature
        lambda t: t.replace(t.split(".")[1], "9999999999", 1),  # extended expiry
        lambda t: "v2" + t[2:],  # version
        lambda t: t.rsplit(".", 1)[0],  # signature dropped
        lambda t: t + ".extra",
        lambda t: "",
        lambda t: "x" * 300,
        lambda t: t[:-2] + "é",
    ],
)
def test_tampered_token_is_rejected(tamper):
    token, _ = auth.issue_session_token(TEST_API_KEY, 60, now=1_000)
    assert not auth.verify_session_token(tamper(token), TEST_API_KEY, now=1_010)


def test_token_from_another_key_is_rejected():
    token, _ = auth.issue_session_token("another_secret_key_value", 60, now=1_000)
    assert not auth.verify_session_token(token, TEST_API_KEY, now=1_010)
    assert not auth.verify_session_token(token, "", now=1_010)
    assert not auth.verify_session_token(None, TEST_API_KEY, now=1_010)


# Login, logout and the cookie
async def test_login_sets_hardened_cookie():
    async with _client() as client:
        resp = await _login(client)
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is True
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(f"{SECURE_COOKIE}=")
    for attr in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age=3600"):
        assert attr.lower() in cookie.lower()
    assert TEST_API_KEY not in cookie


async def test_cookie_can_be_made_non_secure_for_local_http(monkeypatch):
    monkeypatch.setattr(main, "COOKIE_SECURE", False)
    async with _client("http://test") as client:
        resp = await _login(client)
        cookie = resp.headers["set-cookie"]
        assert cookie.startswith("crossline_session=")
        assert "secure" not in cookie.lower()
        assert (await client.get("/api/journal")).status_code == 200


async def test_login_rejects_wrong_key():
    async with _client() as client:
        resp = await _login(client, "wrong_key_wrong_key")
        assert resp.status_code == 401
        assert "set-cookie" not in resp.headers
        assert (await client.get("/api/auth/session")).json()["authenticated"] is False


@pytest.mark.parametrize("configured_key", ["", "changeme", "short"])
async def test_login_fails_closed_when_key_unset(monkeypatch, configured_key):
    monkeypatch.setattr(main, "API_SECRET_KEY", configured_key)
    async with _client() as client:
        resp = await _login(client, configured_key or "anything")
        assert resp.status_code == 503
        assert "set-cookie" not in resp.headers
        session = (await client.get("/api/auth/session")).json()
        assert session == {"authenticated": False, "configured": False}


async def test_session_cookie_authenticates_reads_and_csrf_guarded_writes(fake_services):
    async with _client() as client:
        assert (await client.get("/api/journal")).status_code == 401
        assert (await client.get("/api/auth/session")).json()["authenticated"] is False

        await _login(client)
        assert (await client.get("/api/auth/session")).json() == {"authenticated": True, "configured": True}
        assert (await client.get("/api/journal")).status_code == 200

        # A cookie alone is not enough to change anything
        resp = await client.post("/api/watchlist", json={"symbol": "SUIUSDT"})
        assert resp.status_code == 403
        assert "X-Requested-With" in resp.json()["detail"]
        resp = await client.post("/api/watchlist", json={"symbol": "SUIUSDT"}, headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.status_code == 403
        assert fake_services == []

        resp = await client.post("/api/watchlist", json={"symbol": "SUIUSDT"}, headers=CSRF)
        assert resp.status_code == 200
        assert fake_services == ["SUIUSDT"]

        resp = await client.post("/api/auth/logout", headers=CSRF)
        assert resp.status_code == 200
        assert (await client.get("/api/auth/session")).json()["authenticated"] is False
        assert (await client.get("/api/journal")).status_code == 401


async def test_expired_or_forged_cookie_is_rejected():
    expired, _ = auth.issue_session_token(TEST_API_KEY, 60, now=1_000)
    forged, _ = auth.issue_session_token("attacker_controlled_key", 3600)
    for token in (expired, forged):
        async with _client() as client:
            client.cookies.set(SECURE_COOKIE, token)
            assert (await client.get("/api/journal")).status_code == 401
            resp = await client.post("/api/watchlist", json={"symbol": "SUIUSDT"}, headers=CSRF)
            assert resp.status_code == 401


async def test_cookie_stops_working_when_key_is_unset(monkeypatch):
    async with _client() as client:
        await _login(client)
        monkeypatch.setattr(main, "API_SECRET_KEY", "")
        assert (await client.get("/api/journal")).status_code == 503
        resp = await client.post("/api/watchlist", json={"symbol": "SUIUSDT"}, headers=CSRF)
        assert resp.status_code == 503


async def test_wrong_header_is_not_rescued_by_a_valid_cookie():
    async with _client() as client:
        await _login(client)
        resp = await client.post(
            "/api/watchlist", json={"symbol": "SUIUSDT"}, headers={**CSRF, "X-API-KEY": "wrong_key"}
        )
        assert resp.status_code == 401


async def test_header_auth_needs_no_cookie_and_no_csrf_header(fake_services):
    async with _client() as client:
        resp = await client.post("/api/watchlist", json={"symbol": "SUIUSDT"}, headers={"X-API-KEY": TEST_API_KEY})
        assert resp.status_code == 200
        resp = await client.get("/api/journal", headers={"Authorization": f"Bearer {TEST_API_KEY}"})
        assert resp.status_code == 200
        assert (await client.get("/api/auth/session", headers={"X-API-KEY": TEST_API_KEY})).json()["authenticated"]


# What stays public, and what a public reader does not see
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/journal"),
        ("POST", "/api/scan"),
        ("POST", "/api/macd-scan"),
        ("POST", "/api/evaluate"),
        ("POST", "/api/profitloss-history/evaluate"),
        ("POST", "/api/cron/trigger"),
        ("POST", "/api/watchlist"),
        ("DELETE", "/api/watchlist/BTCUSDT"),
        ("PATCH", "/api/watchlist/BTCUSDT/toggle"),
        ("PUT", "/api/signals/1/journal"),
        ("DELETE", "/api/signals/1/journal"),
        ("POST", "/api/profitloss-history"),
        ("GET", "/api/profitloss-history/adjustments"),
    ],
)
async def test_private_endpoints_require_credentials(method, path):
    async with _client() as client:
        resp = await client.request(method, path, json={})
    assert resp.status_code == 401


async def test_public_signals_hide_the_owners_journal():
    async with _client() as client:
        public = (await client.get("/api/signals")).json()["signals"][0]
        assert "journal_status" not in public
        detail = (await client.get("/api/signals/7")).json()
        assert "journal" not in detail

        await _login(client)
        assert (await client.get("/api/signals")).json()["signals"][0]["journal_status"] == "TAKEN"
        assert (await client.get("/api/signals/7")).json()["journal"]["notes"] == "private"


async def test_cron_error_text_is_only_shown_when_signed_in(monkeypatch):
    monkeypatch.setattr(main.cron_scheduler, "_last_error", "ConnectError: postgres://internal-host")
    async with _client() as client:
        public = (await client.get("/api/cron/status")).json()
        assert "last_error" not in public
        assert "internal-host" not in (await client.get("/api/info")).text
        await _login(client)
        assert (await client.get("/api/cron/status")).json()["last_error"].startswith("ConnectError")


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_api_docs_are_off_by_default(path):
    async with _client() as client:
        assert (await client.get(path)).status_code == 404
