"""Represent unresolved evaluations without a fabricated score.

Revision ID: 0007_evaluation_assessment
Revises: 0006_resume_rework_limit
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

from packages.shared.sql_script import split_sql_statements

revision: str = "0007_evaluation_assessment"
down_revision: str | None = "0006_resume_rework_limit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _run(direction: str) -> None:
    path = Path(__file__).resolve().parent.parent / "sql"
    for statement in split_sql_statements(
        (path / f"0007_evaluation_assessment.{direction}.sql").read_text()
    ):
        op.execute(statement)


def upgrade() -> None:
    _run("up")


def downgrade() -> None:
    _run("down")
