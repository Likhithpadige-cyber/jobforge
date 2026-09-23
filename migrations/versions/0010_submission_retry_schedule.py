"""Add submissions.next_attempt_after (M11 Phase 1 retry-schedule foundation).

Revision ID: 0010_submission_retry_schedule
Revises: 0009_application_preferences
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

from packages.shared.sql_script import split_sql_statements

revision: str = "0010_submission_retry_schedule"
down_revision: str | None = "0009_application_preferences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _run(direction: str) -> None:
    path = Path(__file__).resolve().parent.parent / "sql"
    for statement in split_sql_statements(
        (path / f"0010_submission_retry_schedule.{direction}.sql").read_text()
    ):
        op.execute(statement)


def upgrade() -> None:
    _run("up")


def downgrade() -> None:
    _run("down")
