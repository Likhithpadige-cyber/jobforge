"""Limit distinct human resume rework decisions to three.

Revision ID: 0006_resume_rework_limit
Revises: 0005_revision_attempts
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

from packages.shared.sql_script import split_sql_statements

revision: str = "0006_resume_rework_limit"
down_revision: str | None = "0005_revision_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _run(filename: str) -> None:
    for statement in split_sql_statements((SQL_DIR / filename).read_text(encoding="utf-8")):
        op.execute(statement)


def upgrade() -> None:
    _run("0006_resume_rework_limit.up.sql")


def downgrade() -> None:
    _run("0006_resume_rework_limit.down.sql")
