import pytest

from src.api.passwords import DUMMY_HASH, get_dummy_hash, hash_password, verify_password

pytestmark = pytest.mark.unit


async def test_hash_and_verify_roundtrip():
    pw = "SuperSecretPassword123!"
    stored = await hash_password(pw)

    assert stored.startswith("scrypt$16384$8$1$")
    parts = stored.split("$")
    assert len(parts) == 6
    assert len(parts[4]) > 0  # salt
    assert len(parts[5]) > 0  # key

    assert await verify_password(pw, stored) is True
    assert await verify_password("WrongPassword123!", stored) is False


async def test_different_salts_for_same_password():
    pw = "SamePassword123!"
    h1 = await hash_password(pw)
    h2 = await hash_password(pw)

    assert h1 != h2
    assert await verify_password(pw, h1) is True
    assert await verify_password(pw, h2) is True


async def test_verify_malformed_hashes_fail_gracefully():
    assert await verify_password("test", "invalid_format") is False
    assert await verify_password("test", "scrypt$not$numbers$salt$hash") is False
    assert await verify_password("test", "bcrypt$2b$12$xyz") is False
    assert await verify_password("test", "") is False


async def test_dummy_hash_behavior():
    dummy = get_dummy_hash()
    assert dummy.startswith("scrypt$16384$8$1$")
    assert DUMMY_HASH == dummy
    assert await verify_password("some_random_input", dummy) is False
