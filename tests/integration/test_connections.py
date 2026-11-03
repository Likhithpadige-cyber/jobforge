"""Real PostgreSQL and Redis connectivity.

Skipped automatically when the services are not reachable, so `make test`
works on a laptop with nothing running.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from packages.core.cache import check_redis, close_redis, init_redis
from packages.core.db import check_database, dispose_engine, init_engine, session_scope

pytestmark = pytest.mark.integration


async def test_postgres_connects_and_answers(postgres_available: bool) -> None:
    if not postgres_available:
        pytest.skip("PostgreSQL not reachable - start it with `make dev`")
    await init_engine()
    try:
        health = await check_database()
        assert health["status"] == "ok"
        async with session_scope() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await dispose_engine()


async def test_required_extensions_are_installed(postgres_available: bool) -> None:
    """Fails until `make migrate` has run - which is the point."""
    if not postgres_available:
        pytest.skip("PostgreSQL not reachable")
    await init_engine()
    try:
        async with session_scope() as session:
            result = await session.execute(text("SELECT extname FROM pg_extension"))
            installed = {row[0] for row in result}
        assert {
            "pgcrypto",
            "pg_trgm",
        } <= installed, f"missing extensions; run `make migrate`. installed={sorted(installed)}"
    finally:
        await dispose_engine()


async def test_redis_connects_and_round_trips(redis_available: bool) -> None:
    if not redis_available:
        pytest.skip("Redis not reachable - start it with `make dev`")
    client = await init_redis()
    try:
        health = await check_redis()
        assert health["status"] == "ok"
        await client.set("jobforge:test:key", "value", ex=10)
        assert await client.get("jobforge:test:key") == "value"
        await client.delete("jobforge:test:key")
    finally:
        await close_redis()


async def test_health_probes_never_raise_when_uninitialised() -> None:
    """A health endpoint that throws is worse than one that reports failure."""
    await dispose_engine()
    await close_redis()
    db = await check_database()
    redis = await check_redis()
    assert db["status"] == "unavailable"
    assert redis["status"] == "unavailable"
    assert "error" in db and "error" in redis
