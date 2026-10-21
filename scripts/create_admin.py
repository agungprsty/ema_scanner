import argparse
import asyncio
import getpass
import os
import re
import sys

from src.api.passwords import hash_password
from src.services.db import (
    close_db,
    create_user,
    delete_user_sessions,
    get_user_by_email,
    init_db,
    update_user,
)

EMAIL_REGEX = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
USERNAME_REGEX = r"^[A-Za-z0-9_]{3,32}$"


def _validate_inputs(email: str, username: str, password: str, confirm_password: str) -> tuple[str, str]:
    clean_email = email.strip().lower()
    if len(clean_email) > 254 or not re.fullmatch(EMAIL_REGEX, clean_email):
        raise ValueError("Invalid email format")

    clean_username = username.strip().lower()
    if not re.fullmatch(USERNAME_REGEX, clean_username):
        raise ValueError("Username must be 3-32 characters (letters, numbers, underscores)")

    if password != confirm_password:
        raise ValueError("Passwords do not match")

    if len(password) < 10 or len(password) > 128:
        raise ValueError("Password must be between 10 and 128 characters")

    pw_lower = password.lower()
    if pw_lower == clean_email:
        raise ValueError("Password cannot match email")
    if pw_lower == clean_username:
        raise ValueError("Password cannot match username")

    return clean_email, clean_username


async def run_create_admin(email: str, username: str, role: str, password: str | None = None) -> None:
    if password is None:
        password = os.getenv("ADMIN_PASSWORD")
        if not password:
            password = getpass.getpass("Password: ")
            confirm = getpass.getpass("Confirm password: ")
        else:
            confirm = password
    else:
        confirm = password

    clean_email, clean_username = _validate_inputs(email, username, password, confirm)

    if not await init_db():
        print("Error: Could not connect to database", file=sys.stderr)
        sys.exit(1)

    try:
        pw_hash = await hash_password(password)
        existing = await get_user_by_email(clean_email)
        if existing:
            await update_user(existing["id"], password_hash=pw_hash, role=role, username=clean_username)
            sessions_cleared = await delete_user_sessions(existing["id"])
            print(f"Updated user {clean_email} (role: {role}). Revoked {sessions_cleared} active session(s).")
        else:
            user = await create_user(
                email=clean_email,
                username=clean_username,
                password_hash=pw_hash,
                role=role,
            )
            print(f"Created user {user['email']} with role '{user['role']}' (ID: {user['id']}).")
    finally:
        await close_db()


def main():
    parser = argparse.ArgumentParser(description="Create or update an administrator / user account.")
    parser.add_argument("--email", required=True, help="User email address")
    parser.add_argument("--username", required=True, help="User display username")
    parser.add_argument(
        "--role",
        choices=["administrator", "user"],
        default="administrator",
        help="Account role (default: administrator)",
    )
    parser.add_argument("--password", help="Optional password (if not provided, will prompt)")

    args = parser.parse_args()
    try:
        asyncio.run(run_create_admin(args.email, args.username, args.role, args.password))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
