import hashlib
import re
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from scripts.create_admin import run_create_admin
from src import main
from src.api import security
from src.api.passwords import hash_password, verify_password
from src.config import settings
from src.services import redis_client

pytestmark = pytest.mark.unit


@pytest.fixture
def auth_db(monkeypatch):
    """In-memory users and sessions database mock."""
    users = {}  # id -> user_dict
    users_by_email = {}  # email -> id
    sessions = {}  # token_hash -> session_dict

    next_user_id = 1

    async def fake_create_user(email: str, username: str, password_hash: str, role: str = "user"):
        nonlocal next_user_id
        clean_email = email.strip().lower()
        clean_username = username.strip().lower()
        if clean_email in users_by_email:
            from src.api.exceptions import Conflict

            raise Conflict("Email or username is already registered")
        for u in users.values():
            if u["username"] == clean_username:
                from src.api.exceptions import Conflict

                raise Conflict("Email or username is already registered")

        uid = next_user_id
        next_user_id += 1
        user_dict = {
            "id": uid,
            "email": clean_email,
            "username": clean_username,
            "password_hash": password_hash,
            "role": role,
            "created_at": datetime.now(UTC).isoformat(),
        }
        users[uid] = user_dict
        users_by_email[clean_email] = uid
        return {k: v for k, v in user_dict.items() if k != "password_hash"}

    async def fake_get_user_by_email(email: str):
        clean_email = email.strip().lower()
        uid = users_by_email.get(clean_email)
        if uid and uid in users:
            return dict(users[uid])
        return None

    async def fake_update_user(user_id: int, **kwargs):
        if user_id in users:
            users[user_id].update(kwargs)
            return dict(users[user_id])
        return None

    async def fake_create_session(token_hash: str, user_id: int, expires_at: datetime):
        sessions[token_hash] = {
            "token_hash": token_hash,
            "user_id": user_id,
            "expires_at": expires_at,
            "created_at": datetime.now(UTC),
        }

    async def fake_get_session_user(token_hash: str):
        sess = sessions.get(token_hash)
        if not sess:
            return None
        if sess["expires_at"] < datetime.now(UTC):
            return None
        user = users.get(sess["user_id"])
        if not user:
            return None
        return {
            "id": user["id"],
            "email": user["email"],
            "username": user["username"],
            "role": user["role"],
            "created_at": user["created_at"],
        }

    async def fake_cleanup_expired_sessions():
        now = datetime.now(UTC)
        expired = [k for k, v in sessions.items() if v["expires_at"] < now]
        for k in expired:
            del sessions[k]

    async def fake_delete_session(token_hash: str):
        if token_hash in sessions:
            del sessions[token_hash]
            return True
        return False

    async def fake_delete_user_sessions(user_id: int):
        to_del = [k for k, v in sessions.items() if v["user_id"] == user_id]
        for k in to_del:
            del sessions[k]
        return len(to_del)

    # Patch database services in main and security
    for module in [main, security]:
        monkeypatch.setattr(module, "create_user", fake_create_user, raising=False)
        monkeypatch.setattr(module, "get_user_by_email", fake_get_user_by_email, raising=False)
        monkeypatch.setattr(module, "update_user", fake_update_user, raising=False)
        monkeypatch.setattr(module, "create_session", fake_create_session, raising=False)
        monkeypatch.setattr(module, "get_session_user", fake_get_session_user, raising=False)
        monkeypatch.setattr(module, "cleanup_expired_sessions", fake_cleanup_expired_sessions, raising=False)
        monkeypatch.setattr(module, "delete_session", fake_delete_session, raising=False)
        monkeypatch.setattr(module, "delete_user_sessions", fake_delete_user_sessions, raising=False)

    # Patch in src.services.db and scripts.create_admin
    import scripts.create_admin as ca_module
    from src.services import db

    async def mock_init_db():
        return True

    async def mock_close_db():
        pass

    for mod in [db, ca_module]:
        monkeypatch.setattr(mod, "init_db", mock_init_db)
        monkeypatch.setattr(mod, "close_db", mock_close_db)
        monkeypatch.setattr(mod, "create_user", fake_create_user)
        monkeypatch.setattr(mod, "get_user_by_email", fake_get_user_by_email)
        monkeypatch.setattr(mod, "update_user", fake_update_user)
        monkeypatch.setattr(mod, "delete_user_sessions", fake_delete_user_sessions)

    return {
        "users": users,
        "sessions": sessions,
        "create_user": fake_create_user,
        "create_session": fake_create_session,
    }


async def test_scrypt_password_hashing():
    pw = "SuperSecurePassword123"
    hashed = await hash_password(pw)

    # Verify format: scrypt$n$r$p$salt_b64$hash_b64
    assert re.match(r"^scrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$", hashed)
    assert await verify_password(pw, hashed) is True
    assert await verify_password("WrongPassword123", hashed) is False

    # Unique salts per generation
    hashed2 = await hash_password(pw)
    assert hashed != hashed2


async def test_register_endpoint(auth_db, monkeypatch):
    monkeypatch.setattr(main, "SESSION_COOKIE_SECURE", True)  # independent of a local .env
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "email": "NewTrader@Example.COM",
            "username": "Trader_One",
            "password": "Password12345",
            "role": "administrator",  # should be ignored
        }
        resp = await client.post("/api/auth/register", json=payload)
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["email"] == "newtrader@example.com"
        assert data["username"] == "trader_one"
        assert data["role"] == "user"
        assert "password" not in data
        assert "password_hash" not in data

        # Check Set-Cookie: token only in the cookie, never in the body
        cookie = resp.cookies.get("st_session")
        assert cookie is not None and cookie not in resp.text
        set_cookie = resp.headers["set-cookie"].lower()
        for attr in ("httponly", "secure", "samesite=lax", "path=/", f"max-age={settings.SESSION_TTL_HOURS * 3600}"):
            assert attr in set_cookie


async def test_register_duplicate_conflict(auth_db):
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "email": "trader@example.com",
            "username": "trader1",
            "password": "Password12345",
        }
        res1 = await client.post("/api/auth/register", json=payload)
        assert res1.status_code == 201

        # Same email, different username -> 409
        res2 = await client.post(
            "/api/auth/register",
            json={"email": "trader@example.com", "username": "trader2", "password": "Password12345"},
        )
        assert res2.status_code == 409

        # Different email, same username -> 409
        res3 = await client.post(
            "/api/auth/register",
            json={"email": "other@example.com", "username": "trader1", "password": "Password12345"},
        )
        assert res3.status_code == 409


async def test_register_validation_rules(auth_db):
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Invalid email
        res = await client.post(
            "/api/auth/register",
            json={"email": "not-an-email", "username": "user1", "password": "Password12345"},
        )
        assert res.status_code == 422

        # Password too short (<10)
        res = await client.post(
            "/api/auth/register",
            json={"email": "u@example.com", "username": "user1", "password": "Short12"},
        )
        assert res.status_code == 422

        # Password matches email
        res = await client.post(
            "/api/auth/register",
            json={"email": "u@example.com", "username": "user1", "password": "u@example.com"},
        )
        assert res.status_code == 422

        # Password matches username (case-insensitive), reported on the password field
        res = await client.post(
            "/api/auth/register",
            json={"email": "u@example.com", "username": "user123456", "password": "USER123456"},
        )
        assert res.status_code == 422
        assert [e["field"] for e in res.json()["errors"]] == ["password"]

        # Errors are reported per field
        res = await client.post(
            "/api/auth/register",
            json={"email": "not-an-email", "username": "x", "password": "Password12345"},
        )
        assert {e["field"] for e in res.json()["errors"]} == {"email", "username"}

        # Email is trimmed before it is validated
        res = await client.post(
            "/api/auth/register",
            json={"email": "  Spaced@Example.com ", "username": "spaced", "password": "Password12345"},
        )
        assert res.status_code == 201
        assert res.json()["data"]["email"] == "spaced@example.com"


async def test_login_timing_identical_messages(auth_db):
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create user
        await auth_db["create_user"]("alice@example.com", "alice", await hash_password("ValidPassword123"))

        # Wrong password
        res_wrong_pw = await client.post(
            "/api/auth/login",
            json={"email": "alice@example.com", "password": "WrongPassword123"},
        )
        assert res_wrong_pw.status_code == 401
        assert res_wrong_pw.json()["message"] == "Invalid email or password"

        # Non-existent email
        res_unknown_email = await client.post(
            "/api/auth/login",
            json={"email": "nobody@example.com", "password": "SomePassword123"},
        )
        assert res_unknown_email.status_code == 401
        assert res_unknown_email.json()["message"] == "Invalid email or password"


async def test_auth_me_and_logout_lifecycle(auth_db):
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Register user
        reg_resp = await client.post(
            "/api/auth/register",
            json={"email": "bob@example.com", "username": "bobby", "password": "Password12345"},
        )
        assert reg_resp.status_code == 201
        token = reg_resp.cookies.get("st_session")

        # GET /api/auth/me with session cookie
        me_resp = await client.get("/api/auth/me", cookies={"st_session": token})
        assert me_resp.status_code == 200
        assert me_resp.json()["data"]["username"] == "bobby"
        assert me_resp.json()["data"]["role"] == "user"

        # Logout
        logout_resp = await client.post("/api/auth/logout", cookies={"st_session": token})
        assert logout_resp.status_code == 200

        # Subsequent GET /api/auth/me should fail with 401
        me_after = await client.get("/api/auth/me", cookies={"st_session": token})
        assert me_after.status_code == 401


async def test_expired_session_fails(auth_db):
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        user = await auth_db["create_user"]("charlie@example.com", "charlie", "dummy_hash")
        token = "expired_token_123"
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        # Session expired 1 hour ago
        await auth_db["create_session"](token_hash, user["id"], datetime.now(UTC) - timedelta(hours=1))

        res = await client.get("/api/auth/me", cookies={"st_session": token})
        assert res.status_code == 401


async def test_role_authorization_and_journal_stripping(auth_db, monkeypatch):
    transport = ASGITransport(app=main.app)

    # Create admin and standard user
    admin = await auth_db["create_user"](
        "admin@example.com", "admin", "hash", role="administrator"
    )
    user = await auth_db["create_user"]("viewer@example.com", "viewer", "hash", role="user")

    admin_token = "admin_token_xyz"
    user_token = "user_token_xyz"
    expires = datetime.now(UTC) + timedelta(days=1)
    await auth_db["create_session"](
        hashlib.sha256(admin_token.encode("utf-8")).hexdigest(), admin["id"], expires
    )
    await auth_db["create_session"](
        hashlib.sha256(user_token.encode("utf-8")).hexdigest(), user["id"], expires
    )

    # Mock signals
    sample_signal = {
        "id": 10,
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "journal_status": "TAKEN",
        "journal": {"status": "TAKEN", "notes": "Admin note"},
    }

    async def fake_list_signals(**kwargs):
        return [dict(sample_signal)], 1, False

    async def fake_get_signal(sig_id: int):
        return dict(sample_signal)

    monkeypatch.setattr(main, "list_signals", fake_list_signals)
    monkeypatch.setattr(main, "get_signal", fake_get_signal)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Standard user: 403 on write and admin-only endpoints
        assert (
            await client.post(
                "/api/watchlist",
                json={"symbol": "ETHUSDT"},
                cookies={"st_session": user_token},
            )
        ).status_code == 403

        assert (
            await client.post("/api/cron/trigger", cookies={"st_session": user_token})
        ).status_code == 403

        assert (
            await client.get("/api/journal", cookies={"st_session": user_token})
        ).status_code == 403

        assert (
            await client.put(
                "/api/signals/10/journal",
                json={"status": "TAKEN"},
                cookies={"st_session": user_token},
            )
        ).status_code == 403

        # Standard user: journal_status is stripped from /api/signals
        user_sigs = await client.get("/api/signals", cookies={"st_session": user_token})
        assert user_sigs.status_code == 200
        assert "journal_status" not in user_sigs.json()["data"][0]

        # Standard user: journal detail is stripped from /api/signals/{id}
        user_sig = await client.get("/api/signals/10", cookies={"st_session": user_token})
        assert user_sig.status_code == 200
        assert "journal" not in user_sig.json()["data"]

        # Admin user: journal_status and journal detail preserved
        admin_sigs = await client.get("/api/signals", cookies={"st_session": admin_token})
        assert admin_sigs.status_code == 200
        assert admin_sigs.json()["data"][0]["journal_status"] == "TAKEN"

        admin_sig = await client.get("/api/signals/10", cookies={"st_session": admin_token})
        assert admin_sig.status_code == 200
        assert admin_sig.json()["data"]["journal"]["status"] == "TAKEN"


async def test_page_routing_and_navigation(auth_db):
    transport = ASGITransport(app=main.app)
    user = await auth_db["create_user"]("dan@example.com", "dan", "hash", role="user")
    token = "dan_token_abc"
    await auth_db["create_session"](
        hashlib.sha256(token.encode("utf-8")).hexdigest(),
        user["id"],
        datetime.now(UTC) + timedelta(days=1),
    )

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Unauthenticated /dashboard -> 303 Redirect to /login
        unauth_dash = await client.get("/dashboard", follow_redirects=False)
        assert unauth_dash.status_code == 303
        assert unauth_dash.headers["location"] == "/login"

        # Authenticated /dashboard -> 200 with Cache-Control: no-store
        auth_dash = await client.get(
            "/dashboard", cookies={"st_session": token}, follow_redirects=False
        )
        assert auth_dash.status_code == 200
        assert "no-store" in auth_dash.headers.get("cache-control", "")

        # Authenticated /login and /register -> 303 Redirect to /dashboard
        for p in ("/login", "/register"):
            res = await client.get(p, cookies={"st_session": token}, follow_redirects=False)
            assert res.status_code == 303
            assert res.headers["location"] == "/dashboard"

        # Signal detail page: same guard as the dashboard
        unauth_signal = await client.get("/signals/7", follow_redirects=False)
        assert unauth_signal.status_code == 303
        assert unauth_signal.headers["location"] == "/login"
        auth_signal = await client.get("/signals/7", cookies={"st_session": token}, follow_redirects=False)
        assert auth_signal.status_code == 200
        assert "no-store" in auth_signal.headers.get("cache-control", "")
        assert 'id="chart-viewport"' in auth_signal.text
        assert (await client.get("/signals/not-a-number", cookies={"st_session": token})).status_code == 404

        # Unauthenticated / -> 200 Landing Page
        landing = await client.get("/", follow_redirects=False)
        assert landing.status_code == 200
        assert "Supertrend + EMA 50 + RSI" in landing.text


async def test_static_html_files_not_served():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/static/index.html")).status_code == 404
        assert (await client.get("/static/dashboard.html")).status_code == 404
        # CSS should be served
        assert (await client.get("/static/base.css")).status_code == 200


async def test_csrf_origin_header_check(auth_db):
    transport = ASGITransport(app=main.app)
    user = await auth_db["create_user"]("eve@example.com", "eve", "hash", role="user")
    token = "eve_token_123"
    await auth_db["create_session"](
        hashlib.sha256(token.encode("utf-8")).hexdigest(),
        user["id"],
        datetime.now(UTC) + timedelta(days=1),
    )

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Foreign Origin on login -> 403
        bad_origin_login = await client.post(
            "/api/auth/login",
            json={"email": "eve@example.com", "password": "password"},
            headers={"Origin": "https://attacker.com"},
        )
        assert bad_origin_login.status_code == 403

        # Foreign Origin on logout -> 403
        bad_origin_logout = await client.post(
            "/api/auth/logout",
            cookies={"st_session": token},
            headers={"Origin": "https://attacker.com"},
        )
        assert bad_origin_logout.status_code == 403


async def test_create_admin_script(auth_db):
    # Run CLI bootstrap to create admin
    await run_create_admin(
        email="superadmin@example.com",
        username="superadmin",
        role="administrator",
        password="AdminPassword12345",
    )
    existing = [u for u in auth_db["users"].values() if u["email"] == "superadmin@example.com"]
    assert len(existing) == 1
    assert existing[0]["role"] == "administrator"
    assert existing[0]["username"] == "superadmin"

    # Add a session for this admin
    admin_token_hash = "token_hash_admin"
    await auth_db["create_session"](
        admin_token_hash, existing[0]["id"], datetime.now(UTC) + timedelta(days=1)
    )
    assert admin_token_hash in auth_db["sessions"]

    # Re-run create_admin to update role or password -> revokes active sessions
    await run_create_admin(
        email="superadmin@example.com",
        username="superadmin",
        role="user",
        password="NewAdminPassword12345",
    )
    assert existing[0]["role"] == "user"
    assert admin_token_hash not in auth_db["sessions"]


async def test_unauthenticated_access(monkeypatch):
    async def fake_list_signals(**kwargs):
        return [], 0, False

    monkeypatch.setattr(main, "list_signals", fake_list_signals)
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/signals")
        assert resp.status_code == 401
        assert resp.json()["message"] == "Authentication required"
        assert (await client.get("/api/info")).status_code == 200
        assert (await client.get("/api/health")).status_code in (200, 503)


async def test_pages_answer_head_and_survive_db_outage(monkeypatch):
    async def db_down(token_hash):
        raise ConnectionRefusedError("postgres is down")

    monkeypatch.setattr(main, "get_session_user", db_down)
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test", cookies={"st_session": "stale"}) as client:
        for path in ("/", "/login", "/register"):
            assert (await client.head(path)).status_code == 200
            assert (await client.get(path)).status_code == 200
        dash = await client.get("/dashboard", follow_redirects=False)
        assert dash.status_code == 303 and dash.headers["location"] == "/login"


async def test_cookie_write_with_foreign_origin_rejected_bearer_without_origin_passes(auth_db, monkeypatch):
    admin = await auth_db["create_user"]("root@example.com", "root", "hash", role="administrator")
    token = "root_token_123"
    await auth_db["create_session"](
        hashlib.sha256(token.encode("utf-8")).hexdigest(), admin["id"], datetime.now(UTC) + timedelta(days=1)
    )
    saved = []

    async def fake_upsert(signal_id, **fields):
        saved.append(signal_id)
        return {"signal_id": signal_id, **fields}

    api_key = "machine_key_for_tests_123"
    monkeypatch.setattr(main, "upsert_journal_entry", fake_upsert)
    monkeypatch.setattr(security, "API_SECRET_KEY", api_key)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        foreign = await client.put(
            "/api/signals/7/journal",
            json={"status": "TAKEN"},
            cookies={"st_session": token},
            headers={"Origin": "https://attacker.com"},
        )
        assert foreign.status_code == 403
        assert saved == []

        same_site = await client.put(
            "/api/signals/7/journal",
            json={"status": "TAKEN"},
            cookies={"st_session": token},
            headers={"Origin": "http://test"},
        )
        assert same_site.status_code == 200

        machine = await client.put(
            "/api/signals/8/journal",
            json={"status": "SKIPPED"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert machine.status_code == 200
        assert saved == [7, 8]


async def test_auth_bucket_rate_limits_after_five_attempts(auth_db, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_PER_MINUTE", 5)
    monkeypatch.setattr(redis_client, "_redis", None)
    from fastapi import FastAPI

    from src.api import setup_http

    # A fresh app picks up the enabled limiter; the routes are borrowed from main
    app = FastAPI()
    setup_http(app)
    app.router.routes.extend(r for r in main.app.router.routes if getattr(r, "path", "") == "/api/auth/login")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"email": "nobody@example.com", "password": "WrongPassword123"}
        statuses = [(await client.post("/api/auth/login", json=body)).status_code for _ in range(5)]
        assert statuses == [401] * 5
        blocked = await client.post("/api/auth/login", json=body)
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1
        assert blocked.headers["X-RateLimit-Limit"] == "5"


async def test_create_admin_reset_keeps_role_and_username(auth_db):
    await auth_db["create_user"]("bob@example.com", "bob_trader", "old_hash", role="user")

    # A password reset without --role must not promote the account
    await run_create_admin(email="bob@example.com", username="ignored", password="BrandNewPassword1")
    bob = next(u for u in auth_db["users"].values() if u["email"] == "bob@example.com")
    assert bob["role"] == "user"
    assert bob["username"] == "bob_trader"
    assert await verify_password("BrandNewPassword1", bob["password_hash"])

    # The register rule is checked against the stored username, not the CLI value
    with pytest.raises(ValueError):
        await run_create_admin(email="bob@example.com", username="ignored", password="BOB_TRADER")


async def test_signal_levels_edit_is_admin_only_validated_and_alerted(auth_db, monkeypatch):
    edits, alerts_sent = [], []
    row = {"id": 5, "symbol": "BTCUSDT", "side": "LONG", "entry_price": 99.5, "status": "PENDING"}

    async def fake_update(client, signal_id, side, entry_price, take_profit, stop_loss, now_ms):
        if signal_id != 5:
            return None
        edits.append((side, entry_price, take_profit, stop_loss))
        return row, signal_id == 5

    async def fake_get_signal(signal_id):
        return row

    async def fake_notify(r):
        alerts_sent.append(r)

    monkeypatch.setattr(main, "update_signal_levels", fake_update)
    monkeypatch.setattr(main, "get_signal", fake_get_signal)
    monkeypatch.setattr(main, "notify_signal_edit", fake_notify)

    admin = await auth_db["create_user"]("lead@example.com", "lead", "hash", role="administrator")
    user = await auth_db["create_user"]("sam@example.com", "sam", "hash", role="user")
    expires = datetime.now(UTC) + timedelta(days=1)
    for token, owner in (("lead_token", admin), ("sam_token", user)):
        await auth_db["create_session"](hashlib.sha256(token.encode("utf-8")).hexdigest(), owner["id"], expires)

    body = {"side": "LONG", "entry_price": 99.5, "take_profit": 101.6, "stop_loss": 98.6}
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        put = lambda sid, b, token: client.put(f"/api/signals/{sid}/levels", json=b, cookies={"st_session": token})  # noqa: E731
        assert (await put(5, body, "sam_token")).status_code == 403

        wrong_way = await put(5, {**body, "side": "SHORT"}, "lead_token")
        assert wrong_way.status_code == 422
        assert {e["field"] for e in wrong_way.json()["errors"]} == {"take_profit", "stop_loss"}
        assert (await put(5, {**body, "entry_price": 0}, "lead_token")).status_code == 422
        # Stored, Infinity would break every later response that holds the row; a raw body can carry it
        infinite = await client.put(
            "/api/signals/5/levels",
            content='{"side": "LONG", "entry_price": 99.5, "take_profit": Infinity, "stop_loss": 98.6}',
            headers={"Content-Type": "application/json"},
            cookies={"st_session": "lead_token"},
        )
        assert infinite.status_code == 422
        assert edits == []

        assert (await put(9, body, "lead_token")).status_code == 404

        ok = await put(5, body, "lead_token")
        assert ok.status_code == 200 and ok.json()["data"] == row
        assert edits == [("LONG", 99.5, 101.6, 98.6)]
        assert alerts_sent == [row]

        # Once filled the stop may pass the entry (break-even), but not the target
        row["status"] = "OPEN"
        assert (await put(5, {**body, "stop_loss": 99.8}, "lead_token")).status_code == 200
        crossed = await put(5, {**body, "stop_loss": 101.7}, "lead_token")
        assert crossed.status_code == 422 and [e["field"] for e in crossed.json()["errors"]] == ["stop_loss"]
