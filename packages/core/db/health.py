"""Database health probe."""

from __future__ import annotations

import time

from sqlalchemy import text

from packages.core.db.session import get_engine
from packages.shared.logging import get_logger

log = get_logger(__name__)


async def check_database(timeout_seconds: float = 3.0) -> dict[str, object]:
    """Return a health record for PostgreSQL.

    Never raises: a health endpoint that throws is worse than one that
    reports a failure.
    """
    started = time.perf_counter()
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        return {"status": "ok", "latency_ms": latency_ms}
    except Exception as exc:  # noqa: BLE001 - health probes report, never raise
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        log.warning("health.database.failed", error=type(exc).__name__)
        return {
            "status": "unavailable",
            "latency_ms": latency_ms,
            "error": type(exc).__name__,
        }


async def get_migration_revision() -> str | None:
    """Current Alembic revision, or None if the table does not exist yet."""
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            row = result.first()
            return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None
