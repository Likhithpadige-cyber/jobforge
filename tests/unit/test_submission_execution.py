"""Unit tests for submission execution service (M11 Phase 2).

Exercises:
- Destination email grounding and domain validation
- Precondition validation
- Human submission request & event emission
- Execution attempt outcomes and idempotency
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.core.models import (
    Application,
    ApplicationStatus,
    Artifact,
    Job,
    Resume,
    SitePolicy,
    Submission,
    SubmissionMethod,
    SubmissionResult,
    User,
)
from packages.core.services.submission_execution import (
    execute_submission_attempt,
    extract_and_validate_authoritative_email,
    request_submission,
    validate_submit_preconditions,
)
from packages.services.delivery.email import (
    EmailSendResult,
    EmailSendStatus,
    FakeEmailTransport,
)
from packages.services.ingestion.policy import PolicyDecision
from packages.services.rendering.store import FilesystemArtifactStore
from packages.shared.config import Settings
from packages.shared.errors import ConflictError, NotFoundError, ValidationError
from packages.shared.ids import uuid7

pytestmark = [pytest.mark.unit, pytest.mark.safety]


# ---------------------------------------------------------------------------
# Authoritative destination email validation
# ---------------------------------------------------------------------------

def test_extract_and_validate_authoritative_email_from_mailto_job_url() -> None:
    job = MagicMock(spec=Job)
    job.id = uuid7()
    job.url = "mailto:jobs@example.com"
    policy = MagicMock(spec=SitePolicy)
    policy.target_domain = "example.com"

    dest = extract_and_validate_authoritative_email(job, policy)
    assert dest == "jobs@example.com"


def test_extract_and_validate_authoritative_email_from_mailto_subdomain() -> None:
    job = MagicMock(spec=Job)
    job.id = uuid7()
    job.url = "mailto:careers@jobs.example.com"
    policy = MagicMock(spec=SitePolicy)
    policy.domain = "example.com"

    dest = extract_and_validate_authoritative_email(job, policy)
    assert dest == "careers@jobs.example.com"


def test_extract_and_validate_authoritative_email_rejects_mailto_domain_mismatch() -> None:
    job = MagicMock(spec=Job)
    job.id = uuid7()
    job.url = "mailto:jobs@otherdomain.com"
    policy = MagicMock(spec=SitePolicy)
    policy.domain = "example.com"

    with pytest.raises(ValidationError, match="does not match target policy domain"):
        extract_and_validate_authoritative_email(job, policy)


def test_extract_and_validate_authoritative_email_rejects_non_mailto_url() -> None:
    job = MagicMock(spec=Job)
    job.id = uuid7()
    job.url = "https://example.com/careers/apply"
    policy = MagicMock(spec=SitePolicy)
    policy.domain = "example.com"

    with pytest.raises(ValidationError, match="not an authoritative mailto"):
        extract_and_validate_authoritative_email(job, policy)


def test_extract_and_validate_authoritative_email_rejects_empty_url() -> None:
    job = MagicMock(spec=Job)
    job.id = uuid7()
    job.url = ""
    policy = MagicMock(spec=SitePolicy)
    policy.domain = "example.com"

    with pytest.raises(ValidationError, match="not an authoritative mailto"):
        extract_and_validate_authoritative_email(job, policy)


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preconditions_rejects_non_ready_status() -> None:
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.status = ApplicationStatus.AWAITING_APPROVAL
    session = AsyncMock()

    with pytest.raises(ConflictError, match="is not submission_ready"):
        await validate_submit_preconditions(session, app, Settings())


@pytest.mark.asyncio
async def test_preconditions_rejects_switches_disabled() -> None:
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_READY
    session = AsyncMock()

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=False),
        ),
        pytest.raises(ConflictError, match="Real submission is disabled"),
    ):
        await validate_submit_preconditions(session, app, Settings())


@pytest.mark.asyncio
async def test_preconditions_rejects_incomplete_reviews() -> None:
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_READY
    session = AsyncMock()

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=False),
        ),
        pytest.raises(ConflictError, match="do not satisfy all required approvals"),
    ):
        await validate_submit_preconditions(session, app, Settings())


@pytest.mark.asyncio
async def test_preconditions_rejects_disallowed_policy() -> None:
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_READY
    session = AsyncMock()

    disallowed_eval = MagicMock(
        decision=PolicyDecision.BLOCKED,
        reason="domain blocked",
    )
    mock_snapshot = MagicMock()
    mock_snapshot.submission_method = SubmissionMethod.BLOCKED
    policy_ret = (disallowed_eval, mock_snapshot, None, "example.com")

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(return_value=policy_ret),
        ),
        pytest.raises(ConflictError, match="does not permit automated email submission"),
    ):
        await validate_submit_preconditions(session, app, Settings())


@pytest.mark.asyncio
async def test_preconditions_rejects_expired_policy() -> None:
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_READY
    session = AsyncMock()

    allowed_eval = MagicMock(decision=PolicyDecision.ALLOWED, reason=None)
    mock_snapshot = MagicMock()
    mock_snapshot.submission_method = SubmissionMethod.EMAIL
    mock_policy_row = MagicMock(spec=SitePolicy)
    mock_policy_row.next_review_due = datetime.date(2020, 1, 1)
    policy_ret = (allowed_eval, mock_snapshot, mock_policy_row, "example.com")

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(return_value=policy_ret),
        ),
        pytest.raises(ConflictError, match="expired"),
    ):
        await validate_submit_preconditions(session, app, Settings())


@pytest.mark.asyncio
async def test_preconditions_rejects_missing_submission_record() -> None:
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_READY
    session = AsyncMock()

    allowed_eval = MagicMock(decision=PolicyDecision.ALLOWED, reason=None)
    mock_snapshot = MagicMock()
    mock_snapshot.submission_method = SubmissionMethod.EMAIL
    mock_policy_row = MagicMock(spec=SitePolicy)
    mock_policy_row.next_review_due = datetime.date(2099, 1, 1)

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(allowed_eval, mock_snapshot, mock_policy_row, "example.com")
            ),
        ),
        patch(
            "packages.core.services.submission_execution.existing_submission",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(ConflictError, match="has no attempt-1 submission record"),
    ):
        await validate_submit_preconditions(session, app, Settings())


@pytest.mark.asyncio
async def test_preconditions_rejects_already_executed() -> None:
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_READY
    session = AsyncMock()

    allowed_eval = MagicMock(decision=PolicyDecision.ALLOWED, reason=None)
    mock_snapshot = MagicMock()
    mock_snapshot.submission_method = SubmissionMethod.EMAIL
    mock_policy_row = MagicMock(spec=SitePolicy)
    mock_policy_row.next_review_due = datetime.date(2099, 1, 1)
    mock_sub = MagicMock(spec=Submission)
    mock_sub.result = SubmissionResult.SUCCEEDED
    mock_sub.started_at = datetime.datetime.now(datetime.UTC)

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(allowed_eval, mock_snapshot, mock_policy_row, "example.com")
            ),
        ),
        patch(
            "packages.core.services.submission_execution.existing_submission",
            new=AsyncMock(return_value=mock_sub),
        ),
        pytest.raises(ConflictError, match="already been executed"),
    ):
        await validate_submit_preconditions(session, app, Settings())


@pytest.mark.asyncio
async def test_preconditions_succeeds_with_valid_state() -> None:
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_READY
    session = AsyncMock()

    allowed_eval = MagicMock(decision=PolicyDecision.ALLOWED, reason=None)
    mock_snapshot = MagicMock()
    mock_snapshot.submission_method = SubmissionMethod.EMAIL
    mock_policy_row = MagicMock(spec=SitePolicy)
    mock_policy_row.target_domain = "example.com"
    mock_policy_row.next_review_due = datetime.date(2099, 1, 1)
    mock_sub = MagicMock(spec=Submission)
    mock_sub.result = SubmissionResult.PENDING
    mock_sub.started_at = None

    mock_job = MagicMock(spec=Job)
    mock_job.url = "mailto:hiring@example.com"
    session.get = AsyncMock(return_value=mock_job)

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(allowed_eval, mock_snapshot, mock_policy_row, "example.com")
            ),
        ),
        patch(
            "packages.core.services.submission_execution.existing_submission",
            new=AsyncMock(return_value=mock_sub),
        ),
    ):
        sub, dest = await validate_submit_preconditions(session, app, Settings())
        assert sub is mock_sub
        assert dest == "hiring@example.com"


@pytest.mark.asyncio
async def test_preconditions_rejects_non_mailto_url() -> None:
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_READY
    session = AsyncMock()

    allowed_eval = MagicMock(decision=PolicyDecision.ALLOWED, reason=None)
    mock_snapshot = MagicMock()
    mock_snapshot.submission_method = SubmissionMethod.EMAIL
    mock_policy_row = MagicMock(spec=SitePolicy)
    mock_policy_row.target_domain = "example.com"
    mock_policy_row.next_review_due = datetime.date(2099, 1, 1)
    mock_sub = MagicMock(spec=Submission)
    mock_sub.result = SubmissionResult.PENDING
    mock_sub.started_at = None

    mock_job = MagicMock(spec=Job)
    mock_job.url = "https://example.com/apply"
    session.get = AsyncMock(return_value=mock_job)

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(allowed_eval, mock_snapshot, mock_policy_row, "example.com")
            ),
        ),
        patch(
            "packages.core.services.submission_execution.existing_submission",
            new=AsyncMock(return_value=mock_sub),
        ),
        pytest.raises(ValidationError, match="not an authoritative mailto"),
    ):
        await validate_submit_preconditions(session, app, Settings())


@pytest.mark.asyncio
async def test_preconditions_rejects_mailto_domain_mismatch() -> None:
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_READY
    session = AsyncMock()

    allowed_eval = MagicMock(decision=PolicyDecision.ALLOWED, reason=None)
    mock_snapshot = MagicMock()
    mock_snapshot.submission_method = SubmissionMethod.EMAIL
    mock_policy_row = MagicMock(spec=SitePolicy)
    mock_policy_row.target_domain = "example.com"
    mock_policy_row.next_review_due = datetime.date(2099, 1, 1)
    mock_sub = MagicMock(spec=Submission)
    mock_sub.result = SubmissionResult.PENDING
    mock_sub.started_at = None

    mock_job = MagicMock(spec=Job)
    mock_job.url = "mailto:hiring@otherdomain.com"
    session.get = AsyncMock(return_value=mock_job)

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(allowed_eval, mock_snapshot, mock_policy_row, "example.com")
            ),
        ),
        patch(
            "packages.core.services.submission_execution.existing_submission",
            new=AsyncMock(return_value=mock_sub),
        ),
        pytest.raises(ValidationError, match="does not match target policy domain"),
    ):
        await validate_submit_preconditions(session, app, Settings())


# ---------------------------------------------------------------------------
# request_submission
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_submission_not_found() -> None:
    session = AsyncMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=exec_result)

    user = MagicMock(spec=User)
    user.id = uuid7()

    with pytest.raises(NotFoundError, match="not found"):
        await request_submission(session, uuid7(), Settings(), actor=user)


@pytest.mark.asyncio
async def test_request_submission_transitions_and_emits() -> None:
    session = AsyncMock()
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_READY

    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = app
    session.execute = AsyncMock(return_value=exec_result)

    mock_sub = MagicMock(spec=Submission)
    mock_sub.id = uuid7()
    mock_sub.attempt = 1
    mock_sub.target_domain = "example.com"
    mock_sub.started_at = None

    user = MagicMock(spec=User)
    user.id = uuid7()

    with (
        patch(
            "packages.core.services.submission_execution.validate_submit_preconditions",
            new=AsyncMock(return_value=(mock_sub, "jobs@example.com")),
        ),
        patch(
            "packages.core.services.submission_execution.transition_application",
            new=AsyncMock(),
        ) as mock_trans,
        patch(
            "packages.core.services.submission_execution.emit",
            new=AsyncMock(),
        ) as mock_emit,
    ):
        result = await request_submission(
            session, app.id, Settings(), actor=user
        )
        assert result is mock_sub
        # Proves API does not set started_at
        assert mock_sub.started_at is None

        mock_trans.assert_awaited_once()
        assert mock_trans.call_args[0][2] == ApplicationStatus.SUBMITTING
        assert mock_trans.call_args[1]["actor_type"] == "human"

        mock_emit.assert_awaited_once()
        assert mock_emit.call_args[1]["type"] == "submission.requested"
        assert mock_emit.call_args[1]["actor_type"] == "human"
        assert "destination_email" not in mock_emit.call_args[1]["payload"]
        assert mock_emit.call_args[1]["payload"]["submission_id"] == str(mock_sub.id)


# ---------------------------------------------------------------------------
# execute_submission_attempt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_attempt_dead_when_app_missing(tmp_path) -> None:
    session = AsyncMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=exec_result)

    event = MagicMock()
    event.payload = {"application_id": str(uuid7())}

    store = FilesystemArtifactStore(tmp_path)
    res = await execute_submission_attempt(session, event, settings=Settings(), store=store)
    assert res == "dead"


@pytest.mark.asyncio
async def test_execute_attempt_skips_if_not_submitting(tmp_path) -> None:
    session = AsyncMock()
    app = MagicMock(spec=Application)
    app.status = ApplicationStatus.SUBMITTED

    sub = MagicMock(spec=Submission)
    sub.result = SubmissionResult.SUCCEEDED
    sub.finished_at = datetime.datetime.now(datetime.UTC)

    exec_res1 = MagicMock()
    exec_res1.scalar_one_or_none.return_value = app
    exec_res2 = MagicMock()
    exec_res2.scalar_one_or_none.return_value = sub
    session.execute = AsyncMock(side_effect=[exec_res1, exec_res2])

    event = MagicMock()
    event.payload = {"application_id": str(uuid7())}

    store = FilesystemArtifactStore(tmp_path)
    res = await execute_submission_attempt(session, event, settings=Settings(), store=store)
    assert res == "completed"


@pytest.mark.asyncio
async def test_execute_attempt_switches_disabled_at_execution(tmp_path) -> None:
    session = AsyncMock()
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.status = ApplicationStatus.SUBMITTING

    sub = MagicMock(spec=Submission)
    sub.id = uuid7()
    sub.attempt = 1
    sub.result = SubmissionResult.PENDING
    sub.finished_at = None
    sub.started_at = None

    exec_res1 = MagicMock()
    exec_res1.scalar_one_or_none.return_value = app
    exec_res2 = MagicMock()
    exec_res2.scalar_one_or_none.return_value = sub
    session.execute = AsyncMock(side_effect=[exec_res1, exec_res2])

    event = MagicMock()
    event.id = uuid7()
    event.payload = {"application_id": str(app.id)}

    store = FilesystemArtifactStore(tmp_path)
    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "packages.core.services.submission_execution.transition_application",
            new=AsyncMock(),
        ) as mock_trans,
        patch(
            "packages.core.services.submission_execution.emit",
            new=AsyncMock(),
        ) as mock_emit,
    ):
        res = await execute_submission_attempt(session, event, settings=Settings(), store=store)
        assert res == "completed"
        assert sub.result == SubmissionResult.FAILED
        assert "disabled at execution" in sub.failure_reason
        assert sub.started_at is None
        mock_trans.assert_awaited_once()
        assert mock_trans.call_args[0][2] == ApplicationStatus.SUBMISSION_FAILED
        assert mock_trans.call_args[1]["actor_type"] == "system"
        mock_emit.assert_awaited_once()
        assert mock_emit.call_args[1]["type"] == "submission.failed"
        assert mock_emit.call_args[1]["actor_type"] == "system"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "send_status,expected_app_status,expected_sub_result,expected_event",
    [
        (
            EmailSendStatus.SENT,
            ApplicationStatus.SUBMITTED,
            SubmissionResult.SUCCEEDED,
            "submission.succeeded",
        ),
        (
            EmailSendStatus.REJECTED,
            ApplicationStatus.SUBMISSION_FAILED,
            SubmissionResult.FAILED,
            "submission.failed",
        ),
        (
            EmailSendStatus.AUTH_FAILED,
            ApplicationStatus.SUBMISSION_FAILED,
            SubmissionResult.FAILED,
            "submission.failed",
        ),
        (
            EmailSendStatus.TRANSIENT_FAILURE,
            ApplicationStatus.SUBMISSION_FAILED,
            SubmissionResult.FAILED,
            "submission.failed",
        ),
        (
            EmailSendStatus.AMBIGUOUS,
            ApplicationStatus.SUBMISSION_FAILED,
            SubmissionResult.FAILED,
            "submission.failed",
        ),
    ],
)
async def test_execute_attempt_outcome_mapping(
    tmp_path,
    send_status: EmailSendStatus,
    expected_app_status: ApplicationStatus,
    expected_sub_result: SubmissionResult,
    expected_event: str,
) -> None:
    session = AsyncMock()
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMITTING

    sub = MagicMock(spec=Submission)
    sub.id = uuid7()
    sub.attempt = 1
    sub.result = SubmissionResult.PENDING
    sub.finished_at = None
    sub.target_domain = "example.com"
    sub.method = SubmissionMethod.EMAIL
    sub.policy_id = uuid7()

    exec_res1 = MagicMock()
    exec_res1.scalar_one_or_none.return_value = app
    exec_res2 = MagicMock()
    exec_res2.scalar_one_or_none.return_value = sub
    session.execute = AsyncMock(side_effect=[exec_res1, exec_res2])

    event = MagicMock()
    event.id = uuid7()
    event.payload = {
        "application_id": str(app.id),
    }

    allowed_eval = MagicMock(decision=PolicyDecision.ALLOWED, reason=None)
    mock_snapshot = MagicMock()
    mock_snapshot.submission_method = SubmissionMethod.EMAIL
    mock_policy_row = MagicMock(spec=SitePolicy)
    mock_policy_row.id = sub.policy_id
    mock_policy_row.target_domain = "example.com"
    mock_policy_row.next_review_due = datetime.date(2099, 1, 1)

    mock_job = MagicMock(spec=Job)
    mock_job.title = "Software Engineer"
    mock_job.company = "Acme Corp"
    mock_job.url = "mailto:jobs@example.com"
    session.get = AsyncMock(return_value=mock_job)

    mock_resume = MagicMock(spec=Resume)
    mock_resume.id = uuid7()

    mock_art = MagicMock(spec=Artifact)
    mock_art.storage_key = "pdf_key"

    mock_cl = MagicMock()
    mock_cl.body_text = "Dear hiring manager, please find my application."

    store = MagicMock(spec=FilesystemArtifactStore)
    store.get = AsyncMock(return_value=b"%PDF-1.4 dummy")

    fake_transport = FakeEmailTransport(
        responses=[EmailSendResult(send_status, detail="test detail", message_id="msg123")]
    )

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(allowed_eval, mock_snapshot, mock_policy_row, "example.com")
            ),
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.latest_tailored_resume",
            new=AsyncMock(return_value=mock_resume),
        ),
        patch(
            "packages.core.services.submission_execution.latest_cover_letter",
            new=AsyncMock(return_value=mock_cl),
        ),
        patch(
            "packages.core.services.submission_execution.build_email_transport",
            return_value=fake_transport,
        ),
        patch(
            "packages.core.services.submission_execution.transition_application",
            new=AsyncMock(),
        ) as mock_trans,
        patch(
            "packages.core.services.submission_execution.emit",
            new=AsyncMock(),
        ) as mock_emit,
    ):
        # mock queries: app, sub, job, artifact
        exec_job = MagicMock()
        exec_job.scalar_one_or_none.return_value = mock_job
        exec_art = MagicMock()
        exec_art.scalar_one_or_none.return_value = mock_art
        session.execute = AsyncMock(side_effect=[exec_res1, exec_res2, exec_job, exec_art])

        res = await execute_submission_attempt(session, event, settings=Settings(), store=store)
        assert res == "completed"
        assert sub.result == expected_sub_result
        assert sub.started_at is not None
        assert sub.finished_at is not None
        assert sub.next_attempt_after is None
        if send_status == EmailSendStatus.AMBIGUOUS:
            assert sub.failure_reason.startswith("ambiguous:")

        mock_trans.assert_awaited_once()
        assert mock_trans.call_args[0][2] == expected_app_status
        assert mock_trans.call_args[1]["actor_type"] == "system"

        # Emits submission.started immediately before transport, then expected terminal event
        emit_types = [call[1]["type"] for call in mock_emit.call_args_list]
        assert "submission.started" in emit_types
        assert expected_event in emit_types
        for call in mock_emit.call_args_list:
            assert call[1]["actor_type"] == "system"


@pytest.mark.asyncio
async def test_execute_attempt_destination_changed_between_api_and_worker_prevents_io(
    tmp_path,
) -> None:
    session = AsyncMock()
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMITTING

    sub = MagicMock(spec=Submission)
    sub.id = uuid7()
    sub.attempt = 1
    sub.result = SubmissionResult.PENDING
    sub.finished_at = None
    sub.started_at = None

    exec_res1 = MagicMock()
    exec_res1.scalar_one_or_none.return_value = app
    exec_res2 = MagicMock()
    exec_res2.scalar_one_or_none.return_value = sub
    session.execute = AsyncMock(side_effect=[exec_res1, exec_res2])

    event = MagicMock()
    event.id = uuid7()
    event.payload = {"application_id": str(app.id)}

    allowed_eval = MagicMock(decision=PolicyDecision.ALLOWED, reason=None)
    mock_snapshot = MagicMock()
    mock_snapshot.submission_method = SubmissionMethod.EMAIL
    mock_policy_row = MagicMock(spec=SitePolicy)
    mock_policy_row.target_domain = "example.com"
    mock_policy_row.next_review_due = datetime.date(2099, 1, 1)

    # Job URL changed to non-mailto!
    mock_job = MagicMock(spec=Job)
    mock_job.url = "https://example.com/apply"

    store = MagicMock(spec=FilesystemArtifactStore)
    fake_transport = FakeEmailTransport()

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(allowed_eval, mock_snapshot, mock_policy_row, "example.com")
            ),
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.build_email_transport",
            return_value=fake_transport,
        ),
        patch(
            "packages.core.services.submission_execution.transition_application",
            new=AsyncMock(),
        ) as mock_trans,
        patch(
            "packages.core.services.submission_execution.emit",
            new=AsyncMock(),
        ) as mock_emit,
    ):
        exec_job = MagicMock()
        exec_job.scalar_one_or_none.return_value = mock_job
        session.execute = AsyncMock(side_effect=[exec_res1, exec_res2, exec_job])

        res = await execute_submission_attempt(session, event, settings=Settings(), store=store)
        assert res == "completed"
        assert sub.result == SubmissionResult.FAILED
        assert sub.started_at is None
        assert len(fake_transport.sent) == 0

        mock_trans.assert_awaited_once()
        assert mock_trans.call_args[0][2] == ApplicationStatus.SUBMISSION_FAILED
        assert mock_trans.call_args[1]["actor_type"] == "system"

        mock_emit.assert_awaited_once()
        assert mock_emit.call_args[1]["type"] == "submission.failed"
        assert mock_emit.call_args[1]["actor_type"] == "system"


@pytest.mark.asyncio
async def test_execute_attempt_policy_expired_between_api_and_worker_prevents_io(
    tmp_path,
) -> None:
    session = AsyncMock()
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.status = ApplicationStatus.SUBMITTING

    sub = MagicMock(spec=Submission)
    sub.id = uuid7()
    sub.attempt = 1
    sub.result = SubmissionResult.PENDING
    sub.finished_at = None
    sub.started_at = None

    exec_res1 = MagicMock()
    exec_res1.scalar_one_or_none.return_value = app
    exec_res2 = MagicMock()
    exec_res2.scalar_one_or_none.return_value = sub
    session.execute = AsyncMock(side_effect=[exec_res1, exec_res2])

    event = MagicMock()
    event.id = uuid7()
    event.payload = {"application_id": str(app.id)}

    allowed_eval = MagicMock(decision=PolicyDecision.ALLOWED, reason=None)
    mock_snapshot = MagicMock()
    mock_snapshot.submission_method = SubmissionMethod.EMAIL
    mock_policy_row = MagicMock(spec=SitePolicy)
    mock_policy_row.target_domain = "example.com"
    # Policy review expired yesterday!
    mock_policy_row.next_review_due = datetime.date(2020, 1, 1)

    store = MagicMock(spec=FilesystemArtifactStore)
    fake_transport = FakeEmailTransport()

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(allowed_eval, mock_snapshot, mock_policy_row, "example.com")
            ),
        ),
        patch(
            "packages.core.services.submission_execution.build_email_transport",
            return_value=fake_transport,
        ),
        patch(
            "packages.core.services.submission_execution.transition_application",
            new=AsyncMock(),
        ) as mock_trans,
        patch(
            "packages.core.services.submission_execution.emit",
            new=AsyncMock(),
        ) as mock_emit,
    ):
        res = await execute_submission_attempt(session, event, settings=Settings(), store=store)
        assert res == "completed"
        assert sub.result == SubmissionResult.FAILED
        assert sub.started_at is None
        assert len(fake_transport.sent) == 0

        mock_trans.assert_awaited_once()
        assert mock_trans.call_args[0][2] == ApplicationStatus.SUBMISSION_FAILED
        assert mock_trans.call_args[1]["actor_type"] == "system"

        mock_emit.assert_awaited_once()
        assert mock_emit.call_args[1]["type"] == "submission.failed"
        assert mock_emit.call_args[1]["actor_type"] == "system"
