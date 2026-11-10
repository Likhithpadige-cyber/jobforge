"""M20 step 3: preflight database/Redis checks against the throwaway server.

Read-only: nothing here writes to the shared test database or Redis.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from packages.shared.config import get_settings
from scripts import prod_env
from tests.integration.conftest import _url

pytestmark = [pytest.mark.integration, pytest.mark.safety]


async def _checks(engine, **kw) -> dict[str, prod_env.Check]:  # type: ignore[no-untyped-def]
    return {c.name: c for c in await prod_env.database_checks(engine, **kw)}


async def test_preflight_reads_identity_roles_schema_and_switch(engine) -> None:
    async with engine.connect() as conn:
        name = await conn.scalar(text("SELECT current_database()"))
        user = await conn.scalar(text("SELECT current_user"))
    checks = await _checks(engine, expected_db=name, expected_role=user, bootstrap=True)
    assert checks["database_name"].ok and checks["least_privilege_role"].ok
    assert checks["runtime_roles"].ok, checks["runtime_roles"].detail
    assert checks["schema_at_head"].ok
    assert "dispatch_switch_off" in checks
    # The test database is not the production database: both identity checks
    # fail closed when asked about production.
    unmarked = await _checks(
        engine, expected_db=prod_env.PROD_DATABASE, expected_role=user, bootstrap=True
    )
    assert not unmarked["database_name"].ok and not unmarked["database_marker"].ok
    assert unmarked["owner_with_totp"].fatal is False  # bootstrap only relaxes this one


async def test_preflight_rejects_the_owner_role_and_accepts_app_api(engine) -> None:
    async with engine.connect() as conn:
        name = await conn.scalar(text("SELECT current_database()"))
    as_owner = await _checks(engine, expected_db=name, expected_role="app_api", bootstrap=True)
    assert not as_owner["least_privilege_role"].ok
    api = create_async_engine(_url("app_api"))
    try:
        as_api = await _checks(api, expected_db=name, expected_role="app_api", bootstrap=True)
    finally:
        await api.dispose()
    assert as_api["least_privilege_role"].ok and as_api["schema_at_head"].ok


async def test_preflight_detects_a_redis_without_a_password() -> None:
    """The throwaway Redis has no password, which production must refuse."""
    checks = {c.name: c for c in await prod_env.redis_checks(get_settings())}
    assert checks["redis_reachable"].ok
    assert checks["redis_requires_password"].ok is False
