#!/usr/bin/env python3
"""Initialise credentials for the local demo user (SECURITY.md §3.1).

    docker compose run --rm api python -m scripts.setup_demo_auth --generate-key
    docker compose run --rm --interactive api python -m scripts.setup_demo_auth

No credential in this file, and none written to the repository. The password is
read from a no-echo prompt, hashed with argon2id and stored only as a hash; the
TOTP secret is generated, encrypted with `TOTP_ENCRYPTION_KEY` and stored only
as ciphertext.

The provisioning URI is printed **once**, here, at setup. It contains the TOTP
secret in full, so it is never logged and never stored - if it scrolls away,
re-run this command to enrol a fresh secret rather than trying to recover it.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from packages.core.auth import generate_secret, hash_password, provisioning_uri
from packages.core.crypto import (
    MissingEncryptionKeyError,
    encrypt_totp_secret,
    generate_encryption_key,
)
from packages.core.db import (
    check_database,
    dispose_engine,
    get_migration_revision,
    init_engine,
    session_scope,
)
from packages.core.models import User
from packages.shared.config import get_settings

DEMO_EMAIL = "demo.candidate@example.com"

MIN_PASSWORD_LENGTH = 12


def _read_password() -> str | None:
    """Prompt twice, without echo. Returns None when the input is unusable."""
    try:
        first = getpass.getpass("New password (not echoed): ")
        second = getpass.getpass("Repeat password: ")
    except (EOFError, KeyboardInterrupt):
        print("\naborted")
        return None
    if first != second:
        print("passwords do not match")
        return None
    if len(first) < MIN_PASSWORD_LENGTH:
        print(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        return None
    return first


async def setup(email: str) -> int:
    settings = get_settings()
    if not settings.totp_encryption_key:
        print("TOTP_ENCRYPTION_KEY is not set.")
        print("Generate one with:  python -m scripts.setup_demo_auth --generate-key")
        print("then put it in .env and re-run this command.")
        return 1

    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if user is None:
            print(f"no user with email {email}. Run `make seed` first.")
            return 1

        password = _read_password()
        if password is None:
            return 1

        secret = generate_secret()
        try:
            blob = encrypt_totp_secret(secret, settings.totp_encryption_key)
        except MissingEncryptionKeyError as exc:
            print(f"cannot encrypt the TOTP secret: {exc.detail}")
            return 1

        user.password_hash = hash_password(password)
        user.totp_secret_enc = blob
        del password  # no reason to keep it alive any longer

        print()
        print("Credentials set for", email)
        print()
        print("Enrol this secret in your authenticator app now - it is shown once:")
        print()
        print("  ", provisioning_uri(secret, email))
        print()
        print("  manual entry secret:", secret)
        print()
        print("Then sign in at http://localhost:3000/login")
        return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generate-key",
        action="store_true",
        help="print a fresh TOTP_ENCRYPTION_KEY and exit (touches no database)",
    )
    parser.add_argument("--email", default=DEMO_EMAIL, help="user to configure")
    args = parser.parse_args()

    if args.generate_key:
        print(generate_encryption_key())
        return 0

    settings = get_settings()
    await init_engine(settings)
    try:
        db = await check_database()
        if db["status"] != "ok":
            print("cannot continue: database unavailable")
            return 1
        if await get_migration_revision() is None:
            print("cannot continue: run `make migrate` first")
            return 1
        return await setup(args.email)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
