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

revision: str = "0002_core_schema"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _run(filename: str) -> None:
    op.execute((SQL_DIR / filename).read_text(encoding="utf-8"))


def upgrade() -> None:
    _run("0002_core_schema.up.sql")


def downgrade() -> None:
    _run("0002_core_schema.down.sql")
