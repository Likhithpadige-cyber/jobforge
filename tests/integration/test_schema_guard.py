"""M20 step 3: staging/production refuse to run against the wrong schema.

Uses the real test database (at head) and temporary scratch databases this
test creates and drops itself on the throwaway test server, so the shared
test database's `alembic_version` is never modified.
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from packages.core.db import schema_guard
from packages.core.db.schema_guard import (
    SchemaRevisionMismatchError,
    assert_schema_current,
    code_head_revision,
)
from packages.shared.config import Settings
from tests.integration.test_migrations import _expected_head

pytestmark = [pytest.mark.integration, pytest.mark.safety]

PRODUCTION = Settings(
    _env_file=None,
    environment="production",
    postgres_password="synthetic",
    session_secret="synthetic",
    totp_encryption_key=base64.b64encode(bytes(32)).decode(),
)
LOCAL = Settings(_env_file=None, environment="local")


@asynccontextmanager
async def _scratch_database(engine, revision: str | None, *, table: bool = True):  # type: ignore[no-untyped-def]
    """A throwaway database holding only an alembic_version row (or nothing)."""
    name = f"jobforge_schema_guard_{uuid4().hex[:10]}"
    admin = engine.execution_options(isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_async_engine(engine.url.set(database=name))
    try:
        async with scratch.begin() as conn:
            if table:
                await conn.execute(text("CREATE TABLE alembic_version (version_num varchar(32))"))
            if revision is not None:
                await conn.execute(text("INSERT INTO alembic_version VALUES (:r)"), {"r": revision})
        yield scratch
    finally:
        await scratch.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


def test_code_head_matches_the_revision_files() -> None:
    assert code_head_revision() == _expected_head()


async def test_production_accepts_a_database_at_head(engine) -> None:
    await assert_schema_current(PRODUCTION, engine)


@pytest.mark.parametrize(
    ("revision", "table", "reported"),
    [
        ("0010_submission_retry_schedule", True, "0010_submission_retry_schedule"),
        ("9999_from_the_future", True, "9999_from_the_future"),
        (None, True, "no revision"),
        (None, False, "no revision"),
    ],
)
async def test_production_refuses_any_other_schema(
    engine, revision: str | None, table: bool, reported: str
) -> None:
    async with _scratch_database(engine, revision, table=table) as scratch:
        with pytest.raises(SchemaRevisionMismatchError, match=reported) as exc:
            await assert_schema_current(PRODUCTION, scratch)
        assert code_head_revision() in str(exc.value)
        assert "backup" in str(exc.value)


async def test_local_is_never_blocked(engine) -> None:
    async with _scratch_database(engine, "0001_something_old") as scratch:
        await assert_schema_current(LOCAL, scratch)


async def test_an_unreachable_database_keeps_the_existing_unhealthy_behaviour() -> None:
    # Nothing listens on port 1; this is reported via /health, not a crash.
    unreachable = create_async_engine(
        "postgresql+asyncpg://nobody:synthetic@127.0.0.1:1/none",
        connect_args={"timeout": 2},
    )
    try:
        await assert_schema_current(PRODUCTION, unreachable)
    finally:
        await unreachable.dispose()


async def test_api_lifespan_stops_on_a_schema_mismatch(monkeypatch) -> None:
    """Wiring: the API never reaches `yield` (serving) when the guard refuses."""
    from apps.api import main

    calls: list[str] = []

    async def fake_guard(settings, engine) -> None:  # type: ignore[no-untyped-def]
        calls.append("guard")
        raise SchemaRevisionMismatchError("synthetic mismatch")

    async def noop(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(main, "assert_schema_current", fake_guard)
    monkeypatch.setattr(main, "init_engine", noop)
    monkeypatch.setattr(main, "get_engine", lambda: object())
    served = False
    with pytest.raises(SchemaRevisionMismatchError):
        async with main.lifespan(main.app):
            served = True
    assert calls == ["guard"] and served is False


def test_worker_runtime_checks_the_schema_before_consuming() -> None:
    """Wiring: the guard runs after role verification, before any consumer."""
    import inspect

    from workers import runtime

    source = inspect.getsource(runtime.run)
    guard, sessions = source.index("assert_schema_current("), source.index("async_sessionmaker(")
    assert source.index("verify_role(") < guard < sessions
    assert schema_guard.SchemaRevisionMismatchError.__name__ == "SchemaRevisionMismatchError"
