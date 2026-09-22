"""Candidate employment-type preference and an agent-answer evidence guard.

Revision ID: 0009_application_preferences
Revises: 0008_artifact_binding
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

from packages.shared.sql_script import split_sql_statements

revision: str = "0009_application_preferences"
down_revision: str | None = "0008_artifact_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _run(direction: str) -> None:
    path = Path(__file__).resolve().parent.parent / "sql"
    for statement in split_sql_statements(
        (path / f"0009_application_preferences.{direction}.sql").read_text()
    ):
        op.execute(statement)


def upgrade() -> None:
    _run("up")


def downgrade() -> None:
    _run("down")
