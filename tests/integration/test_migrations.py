"""Migration upgrade and downgrade against a real database.

Runs Alembic programmatically against a throwaway database so the main dev
database is never dropped by a test run.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _expected_head() -> str:
    """The head derived from the revision files themselves.

    Hard-coding it meant this test went stale the moment 0004 was added -
    and a stale assertion here invites "fixing" it by not applying the new
    migration, which is exactly backwards. Deriving it means adding a
    revision never breaks this test, while a BROKEN chain (two heads, or a
    dangling down_revision) still fails loudly.
    """
    import re

    versions = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    revisions: dict[str, str | None] = {}
    for path in versions.glob("*.py"):
        text_content = path.read_text()
        revision = re.search(r'^revision: str = "([^"]+)"', text_content, re.M)
        down = re.search(
            r'^down_revision: str \| None = (?:"([^"]+)"|None)', text_content, re.M
        )
        if revision:
            revisions[revision.group(1)] = down.group(1) if down and down.group(1) else None

    heads = [r for r in revisions if r not in set(revisions.values())]
    assert len(heads) == 1, f"migration chain has {len(heads)} heads: {heads}"
    return heads[0]


async def test_head_revision_is_applied(session) -> None:
    """The database must be at the head the revision files define."""
    revision = (
        await session.execute(text("SELECT version_num FROM alembic_version"))
    ).scalar_one()
    expected = _expected_head()
    assert revision == expected, (
        f"database is at {revision}, revision files head is {expected}. "
        "Run `make migrate` - do not change this assertion."
    )


async def test_all_expected_tables_exist(session) -> None:
    from packages.core.models import ALL_TABLES

    rows = await session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
    )
    present = {row[0] for row in rows}
    missing = sorted(set(ALL_TABLES) - present)
    assert not missing, f"missing tables: {missing}"


async def test_all_enum_types_exist(session) -> None:
    from packages.core.models import ENUM_DEFINITIONS

    for name, expected in ENUM_DEFINITIONS.items():
        rows = await session.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = :name ORDER BY e.enumsortorder"
            ),
            {"name": name},
        )
        actual = [row[0] for row in rows]
        assert actual == expected, f"enum {name} differs: {actual} != {expected}"


async def test_required_extensions_are_installed(session) -> None:
    rows = await session.execute(text("SELECT extname FROM pg_extension"))
    installed = {row[0] for row in rows}
    assert {"pgcrypto", "pg_trgm", "vector", "citext"} <= installed


async def test_expected_indexes_exist(session) -> None:
    expected = {
        "uq_application_per_job",
        "uq_one_pending_approval",
        "uq_facts_source",
        "uq_jobs_source_external",
        "idx_facts_embedding",
        "idx_facts_text_trgm",
        "idx_jobs_title_trgm",
        "idx_events_unpublished",
    }
    rows = await session.execute(
        text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    )
    present = {row[0] for row in rows}
    assert expected <= present, f"missing indexes: {sorted(expected - present)}"


async def test_expected_triggers_exist(session) -> None:
    expected = {
        "trg_applications_require_real_approval",
        "trg_resume_claims_facts_verified",
        "trg_cover_letter_claims_facts_verified",
        "trg_profile_facts_protect_verified",
        "trg_application_status_history_append_only",
        "trg_audit_logs_append_only",
        "trg_events_publish_only",
    }
    rows = await session.execute(
        text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
    )
    present = {row[0] for row in rows}
    assert expected <= present, f"missing triggers: {sorted(expected - present)}"


async def test_roles_exist_with_expected_grants(session) -> None:
    """Skips rather than fails when roles were provisioned out of band."""
    if os.getenv("JOBFORGE_SKIP_ROLE_GRANTS") == "1":
        pytest.skip("role grants intentionally skipped in this environment")

    rows = await session.execute(
        text("SELECT rolname FROM pg_roles WHERE rolname LIKE 'app\\_%'")
    )
    roles = {row[0] for row in rows}
    if not roles:
        pytest.skip("roles not created - run `make db-roles`")
    assert {"app_api", "app_agent", "app_monitor", "app_migrate"} <= roles

    # app_agent must hold no write privilege on profile_facts.
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        has = (
            await session.execute(
                text(
                    "SELECT has_table_privilege('app_agent', 'profile_facts', :priv)"
                ),
                {"priv": privilege},
            )
        ).scalar_one()
        assert has is False, f"app_agent unexpectedly has {privilege} on profile_facts"

    assert (
        await session.execute(
            text("SELECT has_table_privilege('app_agent', 'profile_facts', 'SELECT')")
        )
    ).scalar_one() is True
