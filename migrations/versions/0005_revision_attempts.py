"""Durable revision attempts, their guards, and the app_audit role.

Implements C3_HARDENING_DESIGN.md A, C.1, H.2-H.7 and I.6.1-I.6.2. A thin
`_run(filename)` wrapper over hand-written SQL, matching 0002 and 0004;
`scripts/gen_down_migration.py` generates only 0002's down file, so this
revision's down SQL is hand-written exactly as 0004's is.

Two environment switches, both opt-in and both deliberately explicit:

* ``JOBFORGE_SKIP_ROLE_GRANTS=1`` skips the roles-and-grants half only. The
  table, its indexes and its triggers always run: they are not optional on
  any platform. This is why the SQL is split across two files rather than
  the one H.1 sketched - H.1 predates the split of schema from privileges.
* ``JOBFORGE_ALLOW_DESTRUCTIVE_REVISION_ROLLBACK=1`` lets the down migration
  drop a non-empty attempts table. Without it the downgrade refuses, because
  revision attempts are audit history and a failed deploy is not a reason to
  destroy it.

Revision ID: 0005_revision_attempts
Revises: 0004_events_immutable
Create Date: 2026-09-15
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path

from alembic import op

from packages.shared.sql_script import split_sql_statements

revision: str = "0005_revision_attempts"
down_revision: str | None = "0004_events_immutable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

#: Alembic configures this logger, so the message lands in migration
#: output the same way its own progress lines do.
log = logging.getLogger("alembic.runtime.migration")

#: Escape hatch for managed platforms where roles are provisioned out of band
#: (for example by Terraform against RDS). Never set this in local
#: development: the security tests need the roles, and a skipped security
#: test is not a passing one.
SKIP_ENV = "JOBFORGE_SKIP_ROLE_GRANTS"

#: Consent for a destructive downgrade. Read here and handed to the SQL as a
#: session GUC, so the refusal lives in the SQL next to the count it reports.
DESTRUCTIVE_ENV = "JOBFORGE_ALLOW_DESTRUCTIVE_REVISION_ROLLBACK"


def _run(filename: str) -> None:
    """Execute a SQL file statement by statement, in order, on one connection.

    NOT one `op.execute(whole_file)`. Migrations here run over asyncpg, which
    uses PostgreSQL's extended query protocol: the server parses one statement
    per Parse message and rejects a multi-statement string with "cannot insert
    multiple commands into a prepared statement". Splitting is done by a
    scanner that understands dollar-quoted bodies, so the DO blocks and
    PL/pgSQL functions below survive intact (see packages.shared.sql_script).

    Every statement runs on the same connection inside Alembic's transaction,
    so transactional DDL is preserved: a failure part-way through rolls the
    whole revision back exactly as before.
    """
    sql = (SQL_DIR / filename).read_text(encoding="utf-8")
    for statement in split_sql_statements(sql):
        op.execute(statement)


def upgrade() -> None:
    _run("0005_revision_attempts.up.sql")
    if os.getenv(SKIP_ENV) == "1":
        log.info("%s=1 set; skipping 0005 role and grant creation", SKIP_ENV)
        return
    _run("0005_revision_attempts.grants.up.sql")


def downgrade() -> None:
    allow = "1" if os.getenv(DESTRUCTIVE_ENV) == "1" else "0"
    op.execute(
        f"SET LOCAL jobforge.allow_destructive_revision_rollback = '{allow}'"
    )
    if os.getenv(SKIP_ENV) != "1":
        _run("0005_revision_attempts.grants.down.sql")
    _run("0005_revision_attempts.down.sql")
