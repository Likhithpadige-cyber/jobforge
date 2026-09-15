"""Baseline: required PostgreSQL extensions.

Phase 1 creates no domain tables. It establishes the migration chain and
enables the extensions the Phase 2 schema depends on:

  pgcrypto  - gen_random_uuid() for any server-side default we need
  pg_trgm   - trigram indexes for fuzzy title and fact-text search
  vector    - pgvector, for profile_facts.embedding retrieval

The compose stack uses the pgvector/pgvector:pg16 image so `vector` is
available. If you swap to a plain postgres image, this migration will fail
loudly, which is the correct behaviour.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTENSIONS = ("pgcrypto", "pg_trgm", "btree_gin", "vector")


def upgrade() -> None:
    for extension in EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{extension}"')


def downgrade() -> None:
    # Extensions are intentionally not dropped: other schemas in the same
    # database may depend on them, and dropping them is not reversible in a
    # way that is safe to automate.
    pass
