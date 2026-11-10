"""Refuse to serve against a database schema this release was not built for.

M20 step 3. Migrations run only in the one-shot release job, never on
startup, so an API or worker started before (or instead of) that job would
otherwise run new code against an old schema - or old code against a newer
one after a partial rollback. In staging/production that is refused at
startup. Local and test environments are unaffected.

An unreachable database is a different failure and keeps its existing
behaviour (the API starts and reports itself unhealthy): this guard only
fails when it has positively read a revision that is not the code's head,
or found a reachable database with no Alembic version at all.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError

from packages.shared.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from packages.shared.config import Settings

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[3]


class SchemaRevisionMismatchError(RuntimeError):
    """The database is not at the revision this code requires."""


@lru_cache(maxsize=1)
def code_head_revision(root: Path = ROOT) -> str:
    """The single Alembic head shipped with this code."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise SchemaRevisionMismatchError(f"migration chain has {len(heads)} heads")
    return heads[0]


async def database_revision(engine: AsyncEngine) -> str | None:
    """The applied revision; None when the version table is missing or empty.

    Connection failures propagate (as OperationalError or OSError) so the
    caller can tell "unreachable" from "reachable but unmigrated".
    """
    async with engine.connect() as conn:
        try:
            row = (await conn.execute(text("SELECT version_num FROM alembic_version"))).first()
        except ProgrammingError:
            return None
    return row[0] if row else None


async def assert_schema_current(settings: Settings, engine: AsyncEngine) -> None:
    """Raise SchemaRevisionMismatchError outside local/test on any mismatch."""
    if settings.is_local:
        return
    try:
        current = await database_revision(engine)
    except (OperationalError, DBAPIError, OSError) as exc:
        log.warning("schema_guard.database_unreachable", error=type(exc).__name__)
        return
    expected = code_head_revision()
    if current != expected:
        log.error("schema_guard.mismatch", database=current, expected=expected)
        raise SchemaRevisionMismatchError(
            f"database schema is at {current or 'no revision'}, this release requires "
            f"{expected}; take a backup, then run the release migration job"
        )
    log.info("schema_guard.ok", revision=current)
