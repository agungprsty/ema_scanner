import hashlib
import re
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from scripts.create_admin import run_create_admin
from src import main
from src.api import security
from src.api.passwords import hash_password, verify_password

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


async def test_register_endpoint(auth_db):
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

        # Check Set-Cookie
        cookie = resp.cookies.get("st_session")
        assert cookie is not None


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

        # Password matches username
        res = await client.post(
            "/api/auth/register",
            json={"email": "u@example.com", "username": "user123456", "password": "user123456"},
        )
        assert res.status_code == 422


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

        # Unauthenticated / -> 200 Landing Page
        landing = await client.get("/", follow_redirects=False)
        assert landing.status_code == 200
        assert "RSI 28/72" in landing.text


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
