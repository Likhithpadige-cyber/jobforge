"""Bind rendered artifacts to the approved resume version and template.

Revision ID: 0008_artifact_binding
Revises: 0007_evaluation_assessment
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

from packages.shared.sql_script import split_sql_statements

revision: str = "0008_artifact_binding"
down_revision: str | None = "0007_evaluation_assessment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _run(direction: str) -> None:
    path = Path(__file__).resolve().parent.parent / "sql"
    for statement in split_sql_statements(
        (path / f"0008_artifact_binding.{direction}.sql").read_text()
    ):
        op.execute(statement)


def upgrade() -> None:
    _run("up")


def downgrade() -> None:
    _run("down")
