import argparse
import asyncio
import getpass
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


async def run_create_admin(email: str, username: str, role: str | None = None, password: str | None = None) -> None:
    # password is only passed by tests; the CLI always prompts so it never lands in shell history or .env
    if password is None:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Confirm password: ") != password:
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
            # The stored username is kept, so the register rule is checked against it, not the CLI value
            if password.lower() == existing["username"]:
                raise ValueError("Password cannot match email or username")
            changes = {"password_hash": pw_hash} | ({"role": role} if role else {})
            await update_user(existing["id"], **changes)
            sessions_cleared = await delete_user_sessions(existing["id"])
            print(f"Updated user {clean_email} (role: {role or existing['role']}). Revoked {sessions_cleared} active session(s).")
        else:
            user = await create_user(
                email=clean_email,
                username=clean_username,
                password_hash=pw_hash,
                role=role or "administrator",
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
        help="Role for a new account (default: administrator); on an existing account it changes only when given",
    )

    args = parser.parse_args()
    try:
        asyncio.run(run_create_admin(args.email, args.username, args.role))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
