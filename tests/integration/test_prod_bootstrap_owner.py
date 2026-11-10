"""M20 step 4: owner bootstrap with a verified TOTP enrolment.

The success path runs in a scratch database this test creates and drops on
the throwaway server (the shared test database already has owners, which is
exactly the refusal case, tested against it read-only).
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from uuid import uuid4

import pyotp
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from packages.core.auth import verify_password
from packages.core.crypto import decrypt_totp_secret
from scripts import prod_env

pytestmark = [pytest.mark.integration, pytest.mark.safety]

KEY = base64.b64encode(bytes(range(32))).decode()
PASSWORD = "synthetic-owner-password-0001"


@asynccontextmanager
async def _scratch(engine):  # type: ignore[no-untyped-def]
    name = f"jobforge_bootstrap_{uuid4().hex[:10]}"
    admin = engine.execution_options(isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_async_engine(engine.url.set(database=name))
    try:
        async with scratch.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE users (id uuid PRIMARY KEY, email text NOT NULL UNIQUE, "
                    "password_hash text, totp_secret_enc bytea, role varchar(32) NOT NULL, "
                    "last_login_at timestamptz, created_at timestamptz NOT NULL, "
                    "updated_at timestamptz NOT NULL)"
                )
            )
        yield scratch, name
    finally:
        await scratch.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


def _io(code_for: dict[str, str]):  # type: ignore[no-untyped-def]
    shown: list[str] = []

    def show(uri: str) -> None:
        shown.append(uri)
        code_for["code"] = pyotp.TOTP(pyotp.parse_uri(uri).secret).now()

    return shown, show


async def test_bootstrap_creates_one_owner_with_hash_and_encrypted_totp(engine) -> None:
    async with _scratch(engine) as (scratch, name):
        box: dict[str, str] = {}
        shown, show = _io(box)
        user_id = await prod_env.bootstrap_owner(
            scratch,
            email=" Owner@Example.com ",
            encryption_key=KEY,
            read_password=lambda: PASSWORD,
            read_code=lambda: box["code"],
            show=show,
            expected_db=name,
        )
        assert len(shown) == 1 and shown[0].startswith("otpauth://")
        async with scratch.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT id, email, role, password_hash, totp_secret_enc FROM users")
                )
            ).one()
        assert str(row.id) == user_id and row.email == "owner@example.com" and row.role == "owner"
        assert row.password_hash != PASSWORD and verify_password(PASSWORD, row.password_hash)
        secret = decrypt_totp_secret(row.totp_secret_enc, KEY)
        assert secret == pyotp.parse_uri(shown[0]).secret

        # A second bootstrap is refused and changes nothing.
        with pytest.raises(prod_env.ProdGuardError, match="already exists"):
            await prod_env.bootstrap_owner(
                scratch,
                email="second@example.com",
                encryption_key=KEY,
                read_password=lambda: PASSWORD,
                read_code=lambda: "000000",
                show=lambda u: None,
                expected_db=name,
            )


@pytest.mark.parametrize(
    ("password", "code", "message"),
    [
        ("short", None, "at least"),
        (PASSWORD, "000000", "did not verify"),
        (PASSWORD, "not-a-code", "did not verify"),
    ],
)
async def test_bootstrap_writes_nothing_unless_password_and_code_are_valid(
    engine, password: str, code: str | None, message: str
) -> None:
    async with _scratch(engine) as (scratch, name):
        with pytest.raises(prod_env.ProdGuardError, match=message):
            await prod_env.bootstrap_owner(
                scratch,
                email="owner@example.com",
                encryption_key=KEY,
                read_password=lambda: password,
                read_code=lambda: code or "",
                show=lambda u: None,
                expected_db=name,
            )
        async with scratch.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM users")) == 0


async def test_bootstrap_refuses_the_wrong_database_and_existing_owners(engine) -> None:
    with pytest.raises(prod_env.ProdGuardError, match="not 'jobforge_prod'"):
        await prod_env.bootstrap_owner(
            engine,
            email="owner@example.com",
            encryption_key=KEY,
            read_password=lambda: PASSWORD,
            read_code=lambda: "",
            show=lambda u: None,
        )
    async with engine.connect() as conn:
        name = await conn.scalar(text("SELECT current_database()"))
    with pytest.raises(prod_env.ProdGuardError, match="already exists"):
        await prod_env.bootstrap_owner(
            engine,
            email="owner@example.com",
            encryption_key=KEY,
            read_password=lambda: PASSWORD,
            read_code=lambda: "",
            show=lambda u: None,
            expected_db=name,
        )


async def test_insert_matches_the_real_users_table(engine) -> None:
    """Every NOT NULL column without a default is one the bootstrap writes."""
    async with engine.connect() as conn:
        required = {
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'users' AND table_schema = 'public' "
                        "AND is_nullable = 'NO' AND column_default IS NULL"
                    )
                )
            ).all()
        }
    assert required <= {
        "id",
        "email",
        "role",
        "password_hash",
        "totp_secret_enc",
        "created_at",
        "updated_at",
    }
