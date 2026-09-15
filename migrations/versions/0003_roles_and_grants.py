"""Database roles and least-privilege grants (DATABASE_DESIGN.md §8).

Separate from the schema migration on purpose: roles are cluster-level
objects and creating them needs elevated rights (superuser locally,
rds_superuser on RDS). Keeping them in their own revision means a schema
upgrade never fails because the deploying role could not create a role.

If the connected role lacks permission to create roles, the migration raises
rather than silently continuing. A database whose grants did not apply is a
database where "agents cannot invent facts" is untrue, and failing loudly is
the only safe behaviour.

Revision ID: 0003_roles_grants
Revises: 0002_core_schema
Create Date: 2026-09-09
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path

from alembic import op

from packages.shared.sql_script import split_sql_statements

revision: str = "0003_roles_grants"
down_revision: str | None = "0002_core_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

#: Alembic configures this logger, so the message lands in migration
#: output the same way its own progress lines do.
log = logging.getLogger("alembic.runtime.migration")

#: Escape hatch for managed platforms where roles are provisioned out of band
#: (for example by Terraform against RDS). Set JOBFORGE_SKIP_ROLE_GRANTS=1 to
#: skip. Never set this in local development: the security tests need the roles.
SKIP_ENV = "JOBFORGE_SKIP_ROLE_GRANTS"


def _run(filename: str) -> None:
    """Execute a SQL file statement by statement, in order, on one connection.

    NOT one `op.execute(whole_file)`. Migrations run over asyncpg, which uses
    PostgreSQL's extended query protocol: the server parses one statement per
    Parse message and rejects a multi-statement string with "cannot insert
    multiple commands into a prepared statement". Splitting is done by a
    scanner that understands dollar-quoted bodies, so the DO blocks and
    PL/pgSQL functions in this migration survive intact (see
    packages.shared.sql_script).

    Every statement runs on the same connection inside Alembic's transaction,
    so transactional DDL is unchanged: a failure part-way through rolls the
    whole revision back exactly as before.
    """
    sql = (SQL_DIR / filename).read_text(encoding="utf-8")
    for statement in split_sql_statements(sql):
        op.execute(statement)


def upgrade() -> None:
    if os.getenv(SKIP_ENV) == "1":
        log.info("%s=1 set; skipping role and grant creation", SKIP_ENV)
        return
    _run("0003_roles_grants.up.sql")


def downgrade() -> None:
    if os.getenv(SKIP_ENV) == "1":
        return
    _run("0003_roles_grants.down.sql")
