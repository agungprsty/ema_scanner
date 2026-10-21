import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

# Add project root to sys.path so the script can be run directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.passwords import hash_password
from src.main import RegisterRequest
from src.services.db import (
    close_db,
    create_user,
    delete_user_sessions,
    get_user_by_email,
    init_db,
    update_user,
)


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

    if password != confirm:
        raise ValueError("Passwords do not match")

    req = RegisterRequest(email=email, username=username, password=password)
    clean_email, clean_username = req.email, req.username

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
