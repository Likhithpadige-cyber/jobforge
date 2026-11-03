"""Constraint and trigger behaviour, proved against real PostgreSQL.

Each test names the guarantee it protects. A failure here means a guarantee
in the architecture is no longer true.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from packages.shared.ids import uuid7

pytestmark = pytest.mark.integration

ANY_DB_ERROR = (IntegrityError, DBAPIError)


# ---------------------------------------------- the human-approval invariant
@pytest.mark.safety
async def test_application_cannot_be_approved_without_approver(session, demo_application) -> None:
    """ck_applications_approved_requires_human."""
    with pytest.raises(ANY_DB_ERROR) as exc:
        await session.execute(
            text("UPDATE applications SET status = 'approved' WHERE id = :id"),
            {"id": demo_application["id"]},
        )
        await session.flush()
    assert "approved_requires_human" in str(exc.value) or "approvals row" in str(exc.value)
    await session.rollback()


@pytest.mark.safety
async def test_application_cannot_be_submitted_without_approval_row(
    session, demo_application, demo_user
) -> None:
    """trg_applications_require_real_approval.

    Writing approved_by directly is not enough: an approvals row decided by a
    human must exist. This is the gap the CHECK alone left open.
    """
    with pytest.raises(ANY_DB_ERROR) as exc:
        await session.execute(
            text(
                "UPDATE applications SET status = 'submitted', approved_by = :user, "
                "approved_at = now(), submitted_at = now() WHERE id = :id"
            ),
            {"user": demo_user, "id": demo_application["id"]},
        )
        await session.flush()
    assert "approvals row" in str(exc.value)
    await session.rollback()


@pytest.mark.safety
async def test_application_can_be_approved_with_a_real_approval(
    session, demo_application, demo_user
) -> None:
    """The positive case: the invariant permits the legitimate path."""
    await session.execute(
        text(
            "INSERT INTO approvals (id, application_id, kind, status, "
            "subject_snapshot, requested_at, decided_at, decided_by, "
            "created_at, updated_at) VALUES (:id, :app, 'application', 'approved', "
            "'{}'::jsonb, now(), now(), :user, now(), now())"
        ),
        {"id": uuid7(), "app": demo_application["id"], "user": demo_user},
    )
    await session.execute(
        text(
            "UPDATE applications SET status = 'approved', approved_by = :user, "
            "approved_at = now() WHERE id = :id"
        ),
        {"user": demo_user, "id": demo_application["id"]},
    )
    await session.flush()
    status = (
        await session.execute(
            text("SELECT status FROM applications WHERE id = :id"),
            {"id": demo_application["id"]},
        )
    ).scalar_one()
    assert status == "approved"


@pytest.mark.safety
async def test_approved_by_must_match_the_actual_approver(
    session, demo_application, demo_user
) -> None:
    other_user = uuid7()
    await session.execute(
        text(
            "INSERT INTO users (id, email, created_at, updated_at) "
            "VALUES (:id, :email, now(), now())"
        ),
        {"id": other_user, "email": f"other-{other_user.hex[:8]}@example.com"},
    )
    await session.execute(
        text(
            "INSERT INTO approvals (id, application_id, kind, status, "
            "subject_snapshot, requested_at, decided_at, decided_by, "
            "created_at, updated_at) VALUES (:id, :app, 'application', 'approved', "
            "'{}'::jsonb, now(), now(), :user, now(), now())"
        ),
        {"id": uuid7(), "app": demo_application["id"], "user": demo_user},
    )
    with pytest.raises(ANY_DB_ERROR) as exc:
        await session.execute(
            text(
                "UPDATE applications SET status = 'approved', approved_by = :other, "
                "approved_at = now() WHERE id = :id"
            ),
            {"other": other_user, "id": demo_application["id"]},
        )
        await session.flush()
    assert "does not match any approver" in str(exc.value)
    await session.rollback()


# ------------------------------------------------------ claim grounding
@pytest.mark.safety
async def test_claim_cannot_cite_zero_facts(session, demo_resume) -> None:
    """ck_resume_claims_claim_has_facts."""
    with pytest.raises(ANY_DB_ERROR):
        await session.execute(
            text(
                "INSERT INTO resume_claims (id, resume_id, section, path, "
                "claim_text, fact_ids) VALUES (:id, :resume, 'experience', "
                "'/sections/0', 'An uncited claim', ARRAY[]::UUID[])"
            ),
            {"id": uuid7(), "resume": demo_resume},
        )
        await session.flush()
    await session.rollback()


@pytest.mark.safety
async def test_claim_cannot_cite_a_draft_fact(session, demo_resume, draft_fact) -> None:
    """trg_resume_claims_facts_verified: citing an unverified fact is the
    fabrication path this system exists to close."""
    with pytest.raises(ANY_DB_ERROR) as exc:
        await session.execute(
            text(
                "INSERT INTO resume_claims (id, resume_id, section, path, "
                "claim_text, fact_ids) VALUES (:id, :resume, 'experience', "
                "'/sections/0', 'Cites a draft', ARRAY[:fact]::UUID[])"
            ),
            {"id": uuid7(), "resume": demo_resume, "fact": draft_fact},
        )
        await session.flush()
    assert "verified" in str(exc.value)
    await session.rollback()


@pytest.mark.safety
async def test_claim_cannot_cite_a_nonexistent_fact(session, demo_resume) -> None:
    with pytest.raises(ANY_DB_ERROR):
        await session.execute(
            text(
                "INSERT INTO resume_claims (id, resume_id, section, path, "
                "claim_text, fact_ids) VALUES (:id, :resume, 'experience', "
                "'/sections/0', 'Cites nothing real', ARRAY[:fact]::UUID[])"
            ),
            {"id": uuid7(), "resume": demo_resume, "fact": uuid7()},
        )
        await session.flush()
    await session.rollback()


@pytest.mark.safety
async def test_claim_citing_a_verified_fact_is_accepted(
    session, demo_resume, verified_fact
) -> None:
    await session.execute(
        text(
            "INSERT INTO resume_claims (id, resume_id, section, path, "
            "claim_text, fact_ids) VALUES (:id, :resume, 'experience', "
            "'/sections/0', 'Demo Engineer at Example Cloud Corp', "
            "ARRAY[:fact]::UUID[])"
        ),
        {"id": uuid7(), "resume": demo_resume, "fact": verified_fact},
    )
    await session.flush()


# ---------------------------------------------------- verified fact safety
@pytest.mark.safety
async def test_verified_fact_substance_is_immutable(session, verified_fact) -> None:
    """trg_profile_facts_protect_verified: to change what a verified fact
    says, it must go back to draft and be re-verified."""
    with pytest.raises(ANY_DB_ERROR) as exc:
        await session.execute(
            text("UPDATE profile_facts SET text = 'Something stronger' WHERE id = :id"),
            {"id": verified_fact},
        )
        await session.flush()
    assert "verified fact" in str(exc.value)
    await session.rollback()


async def test_verified_fact_can_be_returned_to_draft_then_edited(session, verified_fact) -> None:
    await session.execute(
        text(
            "UPDATE profile_facts SET status = 'draft', verified_by = NULL, "
            "verified_at = NULL WHERE id = :id"
        ),
        {"id": verified_fact},
    )
    await session.execute(
        text("UPDATE profile_facts SET text = 'Corrected wording' WHERE id = :id"),
        {"id": verified_fact},
    )
    await session.flush()


async def test_fact_cannot_be_verified_without_a_verifier(
    session, demo_profile, demo_experience
) -> None:
    """ck_profile_facts_verified_requires_actor."""
    with pytest.raises(ANY_DB_ERROR):
        await session.execute(
            text(
                "INSERT INTO profile_facts (id, profile_id, fact_type, text, "
                "source_table, source_id, status) VALUES (:id, :pid, 'skill', "
                "'Verified by nobody', 'experiences', :sid, 'verified')"
            ),
            {"id": uuid7(), "pid": demo_profile, "sid": demo_experience},
        )
        await session.flush()
    await session.rollback()


# ------------------------------------------------------ other invariants
async def test_one_live_application_per_job(session, demo_application, demo_profile) -> None:
    """uq_application_per_job."""
    job_id = (
        await session.execute(
            text("SELECT job_id FROM applications WHERE id = :id"),
            {"id": demo_application["id"]},
        )
    ).scalar_one()
    with pytest.raises(ANY_DB_ERROR):
        await session.execute(
            text(
                "INSERT INTO applications (id, job_id, profile_id, status, "
                "created_at, updated_at) VALUES (:id, :job, :profile, 'created', "
                "now(), now())"
            ),
            {"id": uuid7(), "job": job_id, "profile": demo_profile},
        )
        await session.flush()
    await session.rollback()


async def test_abandoned_application_frees_the_job(session, demo_application, demo_profile) -> None:
    """The partial index excludes terminal-failed states, so a job can be
    re-applied to after an abandoned attempt."""
    job_id = (
        await session.execute(
            text("SELECT job_id FROM applications WHERE id = :id"),
            {"id": demo_application["id"]},
        )
    ).scalar_one()
    await session.execute(
        text("UPDATE applications SET status = 'abandoned' WHERE id = :id"),
        {"id": demo_application["id"]},
    )
    await session.execute(
        text(
            "INSERT INTO applications (id, job_id, profile_id, status, "
            "created_at, updated_at) VALUES (:id, :job, :profile, 'created', "
            "now(), now())"
        ),
        {"id": uuid7(), "job": job_id, "profile": demo_profile},
    )
    await session.flush()


async def test_one_pending_approval_per_kind(session, demo_application, pending_approval) -> None:
    with pytest.raises(ANY_DB_ERROR):
        await session.execute(
            text(
                "INSERT INTO approvals (id, application_id, kind, status, "
                "subject_snapshot, requested_at, created_at, updated_at) VALUES "
                "(:id, :app, 'application', 'pending', '{}'::jsonb, now(), now(), now())"
            ),
            {"id": uuid7(), "app": demo_application["id"]},
        )
        await session.flush()
    await session.rollback()


@pytest.mark.safety
async def test_sensitive_answer_cannot_be_agent_authored(session, demo_application) -> None:
    """ck_application_answers_sensitive_human_only."""
    question_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO application_questions (id, application_id, question_text, "
            "input_type, question_class) VALUES (:id, :app, "
            "'Do you require visa sponsorship?', 'boolean', 'sensitive')"
        ),
        {"id": question_id, "app": demo_application["id"]},
    )
    with pytest.raises(ANY_DB_ERROR):
        await session.execute(
            text(
                "INSERT INTO application_answers (id, question_id, application_id, "
                "value, source, requires_human, answered_by, created_at, updated_at) "
                "VALUES (:id, :q, :app, 'No', 'agent', true, NULL, now(), now())"
            ),
            {"id": uuid7(), "q": question_id, "app": demo_application["id"]},
        )
        await session.flush()
    await session.rollback()


async def test_in_progress_certification_has_no_issue_date(session, demo_profile) -> None:
    with pytest.raises(ANY_DB_ERROR):
        await session.execute(
            text(
                "INSERT INTO certifications (id, profile_id, name, status, "
                "issued_date, created_at, updated_at) VALUES (:id, :pid, "
                "'Studying For This', 'in_progress', DATE '2026-01-01', now(), now())"
            ),
            {"id": uuid7(), "pid": demo_profile},
        )
        await session.flush()
    await session.rollback()


async def test_site_policy_automation_requires_a_review(session) -> None:
    with pytest.raises(ANY_DB_ERROR):
        await session.execute(
            text(
                "INSERT INTO site_policies (id, domain, automation_allowed, "
                "created_at, updated_at) VALUES (:id, 'unreviewed.example.invalid', "
                "true, now(), now())"
            ),
            {"id": uuid7()},
        )
        await session.flush()
    await session.rollback()


async def test_status_history_is_append_only(session, demo_application) -> None:
    history_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO application_status_history (id, application_id, "
            "to_status, actor_type) VALUES (:id, :app, 'created', 'system')"
        ),
        {"id": history_id, "app": demo_application["id"]},
    )
    await session.flush()
    with pytest.raises(ANY_DB_ERROR) as exc:
        await session.execute(
            text("UPDATE application_status_history SET reason = 'rewritten' " "WHERE id = :id"),
            {"id": history_id},
        )
        await session.flush()
    assert "append-only" in str(exc.value)
    await session.rollback()


async def test_events_are_immutable_except_for_publication(session) -> None:
    event_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO events (id, type, aggregate_type, correlation_id, "
            "actor_type, payload) VALUES (:id, 'job.discovered', 'job', :corr, "
            "'system', '{}'::jsonb)"
        ),
        {"id": event_id, "corr": uuid7()},
    )
    await session.flush()
    # Publication state may change.
    await session.execute(
        text("UPDATE events SET published = true, published_at = now() WHERE id = :id"),
        {"id": event_id},
    )
    await session.flush()
    # Content may not.
    with pytest.raises(ANY_DB_ERROR):
        await session.execute(
            text("UPDATE events SET payload = '{\"tampered\": true}'::jsonb " "WHERE id = :id"),
            {"id": event_id},
        )
        await session.flush()
    await session.rollback()


async def test_generated_is_current_column(session, demo_profile) -> None:
    exp_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO experiences (id, profile_id, company_name, title, "
            "start_date, created_at, updated_at) VALUES (:id, :pid, 'Example Corp', "
            "'Demo Role', DATE '2025-01-01', now(), now())"
        ),
        {"id": exp_id, "pid": demo_profile},
    )
    await session.flush()
    is_current = (
        await session.execute(
            text("SELECT is_current FROM experiences WHERE id = :id"), {"id": exp_id}
        )
    ).scalar_one()
    assert is_current is True

    await session.execute(
        text("UPDATE experiences SET end_date = DATE '2026-01-01' WHERE id = :id"),
        {"id": exp_id},
    )
    await session.flush()
    is_current = (
        await session.execute(
            text("SELECT is_current FROM experiences WHERE id = :id"), {"id": exp_id}
        )
    ).scalar_one()
    assert is_current is False


async def test_audit_trigger_records_profile_changes(session, demo_profile) -> None:
    await session.execute(
        text("UPDATE profiles SET headline = 'Changed headline' WHERE id = :id"),
        {"id": demo_profile},
    )
    await session.flush()
    count = (
        await session.execute(
            text(
                "SELECT count(*) FROM audit_logs WHERE resource_type = 'profiles' "
                "AND resource_id = :id AND action = 'update'"
            ),
            {"id": demo_profile},
        )
    ).scalar_one()
    assert count >= 1
