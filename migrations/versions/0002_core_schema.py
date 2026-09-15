"""Phase 2 core schema: all 37 tables, 17 enums, indexes, constraints, triggers.

The DDL lives in migrations/sql/0002_core_schema.{up,down}.sql rather than in
op.create_table() calls. That is a deliberate implementation choice, recorded
as D-1 in docs/DATABASE_IMPLEMENTATION.md:

  * DATABASE_DESIGN.md specifies the schema in SQL, so SQL migrations map onto
    it one-to-one and are reviewable against it line by line.
  * Generated columns, partial and expression indexes, HNSW index options and
    plpgsql triggers are all clearer as SQL than as Alembic operation objects.
  * The down migration is generated from the up migration by
    scripts/gen_down_migration.py, so the two cannot drift.

The SQLAlchemy models in packages/core/models mirror this schema; the
conformance checker (scripts/verify_schema.py) fails the build if they
disagree.

Revision ID: 0002_core_schema
Revises: 0001_baseline
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

from packages.shared.sql_script import split_sql_statements

revision: str = "0002_core_schema"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


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
    _run("0002_core_schema.up.sql")


def downgrade() -> None:
    _run("0002_core_schema.down.sql")
