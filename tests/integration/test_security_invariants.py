"""Security invariants, proved against a real PostgreSQL.

These are the tests that make SECURITY.md's claims checkable rather than
aspirational. If any of them is ever weakened to make a build pass, the
claim it protects becomes false.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from packages.shared.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.safety]


# ==========================================================================
# 1. An agent role cannot create or modify profile facts (SECURITY.md §4.1)
# ==========================================================================
async def test_agent_cannot_insert_profile_fact(agent_engine, demo_profile) -> None:
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(
                text(
                    "INSERT INTO profile_facts (id, profile_id, fact_type, text, "
                    "source_table, source_id, status) VALUES "
                    "(:id, :pid, 'certification', 'Holds a certification "
                    "the candidate does not have', 'certifications', :sid, 'verified')"
                ),
                {"id": uuid7(), "pid": demo_profile, "sid": uuid7()},
            )
        assert "permission denied" in str(exc.value).lower()


async def test_agent_cannot_update_verified_fact(agent_engine, verified_fact) -> None:
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(
                text("UPDATE profile_facts SET text = 'inflated claim' WHERE id = :id"),
                {"id": verified_fact},
            )
        assert "permission denied" in str(exc.value).lower()


async def test_agent_cannot_delete_profile_fact(agent_engine, verified_fact) -> None:
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(
                text("DELETE FROM profile_facts WHERE id = :id"), {"id": verified_fact}
            )
        assert "permission denied" in str(exc.value).lower()


async def test_agent_cannot_promote_a_draft_fact_to_verified(
    agent_engine, draft_fact
) -> None:
    """The most valuable single test in the suite.

    If an agent could flip status to 'verified', every other anti-fabrication
    control downstream would be citing a fact the model invented.
    """
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(
                text("UPDATE profile_facts SET status = 'verified' WHERE id = :id"),
                {"id": draft_fact},
            )
        assert "permission denied" in str(exc.value).lower()


async def test_agent_can_read_facts(agent_engine, committed_fact) -> None:
    """Agents must still be able to read - grounding depends on it.

    Uses `committed_fact`, not `verified_fact`: the agent connects on a
    separate connection, and an uncommitted row would be invisible to it.
    """
    async with agent_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT status FROM profile_facts WHERE id = :id"), {"id": committed_fact}
        )
        assert result.scalar_one() == "verified"


async def test_agent_can_increment_usage_via_definer_function(
    agent_engine, committed_fact
) -> None:
    """The one narrow exception, and it touches exactly one column.

    Also proves the SECURITY DEFINER function is genuinely the only write
    path: the same role cannot UPDATE the column directly (asserted above).
    """
    async with agent_engine.begin() as conn:
        before = (
            await conn.execute(
                text("SELECT usage_count FROM profile_facts WHERE id = :id"),
                {"id": committed_fact},
            )
        ).scalar_one()
        after = (
            await conn.execute(
                text("SELECT jobforge_increment_fact_usage(:id)"), {"id": committed_fact}
            )
        ).scalar_one()
        assert after == before + 1


# ==========================================================================
# 2. An agent role cannot approve anything (SECURITY.md §3.2)
# ==========================================================================
async def test_agent_cannot_insert_approval(agent_engine, demo_application) -> None:
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(
                text(
                    "INSERT INTO approvals (id, application_id, kind, status, "
                    "subject_snapshot, requested_at, decided_at, decided_by) VALUES "
                    "(:id, :app, 'application', 'approved', '{}'::jsonb, now(), "
                    "now(), :user)"
                ),
                {"id": uuid7(), "app": demo_application["id"],
                 "user": demo_application["user_id"]},
            )
        assert "permission denied" in str(exc.value).lower()


async def test_agent_cannot_update_existing_approval(
    agent_engine, pending_approval
) -> None:
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(
                text("UPDATE approvals SET status = 'approved' WHERE id = :id"),
                {"id": pending_approval},
            )
        assert "permission denied" in str(exc.value).lower()


async def test_agent_cannot_write_approval_columns_on_applications(
    agent_engine, demo_application
) -> None:
    """Column-level grant: agents drive the pipeline but not the approval.

    Without this, an agent holding table-level UPDATE could satisfy
    ck_applications_approved_requires_human by writing any user id.
    """
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(
                text("UPDATE applications SET approved_by = :user WHERE id = :id"),
                {"user": demo_application["user_id"], "id": demo_application["id"]},
            )
        assert "permission denied" in str(exc.value).lower()


async def test_agent_cannot_modify_site_policies(agent_engine, demo_site_policy) -> None:
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(
                text(
                    "UPDATE site_policies SET automation_allowed = true, "
                    "submission_method = 'official_api' WHERE id = :id"
                ),
                {"id": demo_site_policy},
            )
        assert "permission denied" in str(exc.value).lower()


async def test_agent_cannot_modify_settings(agent_engine) -> None:
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(
                text(
                    "INSERT INTO settings (key, value) "
                    "VALUES ('submission_enabled', 'true'::jsonb)"
                )
            )
        assert "permission denied" in str(exc.value).lower()


# ==========================================================================
# 3. Agents cannot escalate their own privileges
# ==========================================================================
async def test_agent_cannot_grant_itself_permissions(agent_engine) -> None:
    """Assert the OUTCOME, not the error.

    PostgreSQL does not raise when a non-owner issues a GRANT for a privilege
    it lacks grant option on: it emits `WARNING: no privileges were granted`
    and the statement succeeds having granted nothing. Asserting an exception
    would fail on a perfectly secure database. What matters is that the
    privilege is still absent afterwards.
    """
    async with agent_engine.connect() as conn:
        try:
            await conn.execute(text("GRANT INSERT ON profile_facts TO app_agent"))
        except (ProgrammingError, DBAPIError):
            pass  # some configurations do error; either outcome is acceptable

    # Fresh connection: if the GRANT errored above, that transaction is aborted.
    async with agent_engine.connect() as conn:
        granted = (
            await conn.execute(
                text("SELECT has_table_privilege('app_agent','profile_facts','INSERT')")
            )
        ).scalar_one()
        assert granted is False, (
            "app_agent granted itself INSERT on profile_facts - the agent role "
            "can now create facts, which defeats the entire grounding model"
        )


async def test_agent_cannot_grant_privileges_to_another_role(agent_engine) -> None:
    """The same hole, one step removed: granting to a different role."""
    async with agent_engine.connect() as conn:
        try:
            await conn.execute(text("GRANT INSERT ON profile_facts TO app_monitor"))
        except (ProgrammingError, DBAPIError):
            pass

    async with agent_engine.connect() as conn:
        granted = (
            await conn.execute(
                text("SELECT has_table_privilege('app_monitor','profile_facts','INSERT')")
            )
        ).scalar_one()
        assert granted is False


async def test_agent_cannot_create_tables(agent_engine) -> None:
    """No CREATE on schema public: an agent cannot define a view or function
    that would read or write around a grant."""
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(text("CREATE TABLE agent_workaround (id UUID)"))
        assert "permission denied" in str(exc.value).lower()


async def test_agent_cannot_drop_a_constraint(agent_engine) -> None:
    """Assert the OUTCOME, not merely that something raised.

    A raises-only test here would pass for the wrong reasons - a mistyped
    constraint name raises too. What matters is that the human-approval
    CHECK is still attached to the table afterwards.
    """
    async with agent_engine.connect() as conn:
        try:
            await conn.execute(
                text(
                    "ALTER TABLE applications DROP CONSTRAINT "
                    "ck_applications_approved_requires_human"
                )
            )
        except (ProgrammingError, DBAPIError):
            pass  # expected; the assertion below is what proves the outcome

    async with agent_engine.connect() as conn:
        still_there = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'ck_applications_approved_requires_human'"
                )
            )
        ).scalar_one()
        assert still_there == 1, (
            "the human-approval CHECK constraint was dropped by app_agent; "
            "an agent could now mark an application approved with no approver"
        )


async def test_agent_cannot_delete_the_audit_trail(agent_engine) -> None:
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(text("DELETE FROM audit_logs"))
        assert "permission denied" in str(exc.value).lower()


async def test_agent_cannot_delete_events(agent_engine) -> None:
    async with agent_engine.connect() as conn:
        with pytest.raises((ProgrammingError, DBAPIError)) as exc:
            await conn.execute(text("DELETE FROM events"))
        assert "permission denied" in str(exc.value).lower()
