"""Database roles and least-privilege grants (DATABASE_DESIGN.md §8).

Separate from the schema migration on purpose: roles are cluster-level
objects and creating them needs elevated rights (superuser locally,
rds_superuser on RDS). Keeping them in their own revision means a schema
upgrade never fails because the deploying role could not create a role.

If the connected role lacks permission to create roles, the migration raises
rather than silently continuing. A database whose grants did not apply is a
database where "agents cannot invent facts" is untrue, and failing loudly is
the only safe behaviour.

Revision ID: 0003_roles_grants
Revises: 0002_core_schema
Create Date: 2026-09-09
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0003_roles_grants"
down_revision: str | None = "0002_core_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

#: Escape hatch for managed platforms where roles are provisioned out of band
#: (for example by Terraform against RDS). Set JOBFORGE_SKIP_ROLE_GRANTS=1 to
#: skip. Never set this in local development: the security tests need the roles.
SKIP_ENV = "JOBFORGE_SKIP_ROLE_GRANTS"


def _run(filename: str) -> None:
    op.execute((SQL_DIR / filename).read_text(encoding="utf-8"))


def upgrade() -> None:
    if os.getenv(SKIP_ENV) == "1":
        print(f"{SKIP_ENV}=1 set; skipping role and grant creation")
        return
    _run("0003_roles_grants.up.sql")


def downgrade() -> None:
    if os.getenv(SKIP_ENV) == "1":
        return
    _run("0003_roles_grants.down.sql")
