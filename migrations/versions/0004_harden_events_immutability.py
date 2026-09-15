"""Harden the events immutability trigger to cover every column.

0002 is deliberately left untouched. It has already been applied on running
databases, so editing it would change what a fresh install builds without
changing anything that already exists - the two paths would diverge. A new
revision keeps `upgrade from 0003` and `build from scratch` identical.

Revision ID: 0004_events_immutable
Revises: 0003_roles_grants
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0004_events_immutable"
down_revision: str | None = "0003_roles_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _run(filename: str) -> None:
    op.execute((SQL_DIR / filename).read_text(encoding="utf-8"))


def upgrade() -> None:
    _run("0004_harden_events_immutability.up.sql")


def downgrade() -> None:
    _run("0004_harden_events_immutability.down.sql")
