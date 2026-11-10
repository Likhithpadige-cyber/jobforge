"""Add applications.applied_on and follow_up_on (M19 application tracking).

Revision ID: 0011_application_tracking
Revises: 0010_submission_retry_schedule
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

from packages.shared.sql_script import split_sql_statements

revision: str = "0011_application_tracking"
down_revision: str | None = "0010_submission_retry_schedule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _run(direction: str) -> None:
    path = Path(__file__).resolve().parent.parent / "sql"
    for statement in split_sql_statements(
        (path / f"0011_application_tracking.{direction}.sql").read_text()
    ):
        op.execute(statement)


def upgrade() -> None:
    _run("up")


def downgrade() -> None:
    _run("down")
