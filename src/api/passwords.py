import asyncio
import base64
import hashlib
import os
import secrets

DEFAULT_N = 2**14  # 16384
DEFAULT_R = 8
DEFAULT_P = 1
DEFAULT_DKLEN = 64

_dummy_hash: str | None = None


def _hash_sync(
    password: str,
    n: int = DEFAULT_N,
    r: int = DEFAULT_R,
    p: int = DEFAULT_P,
    dklen: int = DEFAULT_DKLEN,
) -> str:
    salt = os.urandom(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    key_b64 = base64.b64encode(key).decode("ascii")
    return f"scrypt${n}${r}${p}${salt_b64}${key_b64}"


def _verify_sync(password: str, stored: str) -> bool:
    try:
        parts = stored.split("$")
        if len(parts) != 6 or parts[0] != "scrypt":
            return False
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = base64.b64decode(parts[4])
        expected_key = base64.b64decode(parts[5])
        computed_key = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected_key),
        )
        return secrets.compare_digest(computed_key, expected_key)
    except Exception:
        return False


async def hash_password(password: str) -> str:
    """Hash password using scrypt in a background thread."""
    return await asyncio.to_thread(_hash_sync, password)


async def verify_password(password: str, stored: str) -> bool:
    """Verify password against stored scrypt hash in a background thread."""
    return await asyncio.to_thread(_verify_sync, password, stored)


def get_dummy_hash() -> str:
    """Return a lazily computed scrypt hash for timing attack mitigation on failed lookups."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _hash_sync("dummy-password-for-timing-mitigation")
    return _dummy_hash


def __getattr__(name: str):
    if name == "DUMMY_HASH":
        return get_dummy_hash()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
