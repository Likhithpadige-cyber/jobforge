"""The audit engine: a second connection pool that shares nothing.

**Why a second engine rather than a second session on the first.** The point
of the audit boundary is that its writes outlive a domain rollback. A session
is not enough for that - two sessions on one connection would still be one
transaction to PostgreSQL, so the domain's `ROLLBACK` would take the audit
rows with it. Independence has to reach all the way down to the connection.

**Why a second role, when configured.** `app_audit` may insert into
`agent_runs`, `agent_run_steps`, `llm_calls` and `events`, and nothing else
(migration 0005 grants). That is defence in depth against exactly the failure
this module makes more likely: a code path that now holds a live connection
outside the request's transaction, where a bug could otherwise write anywhere.
The column-level grants mean a boundary bug cannot touch `applications`,
`approvals` or `resume_revision_attempts` even if it tries.

**The fallback, when `AUDIT_DATABASE_URL` is unset.** Its own engine, its own
pool, its own transactions - against the domain DSN. Durability across a
domain rollback is unchanged; what is absent is privilege separation, and
`Settings.audit_dsn_is_separate` reports that a separate DSN was configured.

**It reports configuration, not enforcement, and the distinction is the
point.** A configured DSN could be the domain DSN, or a superuser one; the URL
cannot tell you what the cluster will actually permit. So nothing here claims
privilege isolation - that claim is only made where it can be established, by
`tests/integration/test_app_audit_privileges.py` asking PostgreSQL through
`has_table_privilege`. The two properties are provisioned on different
timescales anyway: transaction independence ships with this code, the role
ships with an ops change. An unprovisioned environment is degraded, not
broken, and never silently: startup logs it once.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from packages.shared.config import Settings, get_settings
from packages.shared.logging import get_logger

log = get_logger(__name__)

_audit_engine: AsyncEngine | None = None
_audit_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def create_audit_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    return create_async_engine(
        settings.audit_dsn,
        echo=settings.db_echo,
        pool_size=settings.audit_db_pool_size,
        max_overflow=settings.audit_db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_pre_ping=True,
        connect_args={
            "timeout": settings.db_connect_timeout_seconds,
            # A distinct application_name so `pg_stat_activity` separates
            # audit connections from domain ones without guessing.
            "server_settings": {"application_name": f"{settings.app_name}-audit"},
        },
    )


async def init_audit_engine(settings: Settings | None = None) -> AsyncEngine:
    """Create the process-wide audit engine. Idempotent."""
    global _audit_engine, _audit_sessionmaker
    settings = settings or get_settings()
    if _audit_engine is None:
        _audit_engine = create_audit_engine(settings)
        _audit_sessionmaker = async_sessionmaker(
            bind=_audit_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        log.info(
            "database.audit_engine.created",
            # Named for what it is. A field called `privileges_enforced` here
            # would be an unverified guarantee sitting in the log an incident
            # review reads first.
            separate_dsn=settings.audit_dsn_is_separate,
        )
        if not settings.audit_dsn_is_separate:
            # Once, at startup, at warning level. An operator who has not
            # provisioned the role should find out here rather than from a
            # privilege test that quietly skipped.
            log.warning("database.audit_engine.using_domain_dsn")
    return _audit_engine


async def dispose_audit_engine() -> None:
    global _audit_engine, _audit_sessionmaker
    if _audit_engine is not None:
        await _audit_engine.dispose()
        log.info("database.audit_engine.disposed")
    _audit_engine = None
    _audit_sessionmaker = None


def audit_engine_ready() -> bool:
    """Whether the audit engine exists. Never raises.

    The boundary asks this rather than catching `RuntimeError`, so "audit was
    never initialised" is a distinguishable state and not an exception shape
    shared with a real connection failure.
    """
    return _audit_sessionmaker is not None


def get_audit_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _audit_sessionmaker is None:
        raise RuntimeError("Audit engine not initialised. Call init_audit_engine() first.")
    return _audit_sessionmaker


@asynccontextmanager
async def audit_session_scope() -> AsyncIterator[AsyncSession]:
    """One audit transaction. Commits on success, rolls back on any exception.

    Never handed a domain session and never handing one out: callers that want
    to write audit rows get this scope, so there is no signature anywhere that
    would let a domain session be passed in by accident.
    """
    async with get_audit_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
