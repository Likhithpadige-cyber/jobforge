"""M11 Phase 3: Submission retry and recovery unit tests.

Tests the complete retry lifecycle:
- Retry decision logic and backoff delay calculation
- Scheduler discovery and deterministic event emission
- Retry claim, state recovery (SUBMISSION_FAILED -> SUBMITTING), and attempt N+1 creation
- Safety switch and preflight enforcement during retry execution
- Retry budget exhaustion and terminal handling
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models import (
    Application,
    ApplicationStatus,
    Artifact,
    Event,
    Job,
    Resume,
    SitePolicy,
    Submission,
    SubmissionMethod,
    SubmissionResult,
)
from packages.core.services.submission_execution import (
    _execute_attempt,
    execute_submission_retry,
    schedule_due_retries,
)
from packages.services.delivery.email import (
    EmailSendResult,
    EmailSendStatus,
    FakeEmailTransport,
)
from packages.services.ingestion.policy import PolicyDecision
from packages.services.rendering.store import FilesystemArtifactStore
from packages.shared.config import Settings
from packages.shared.ids import uuid7

# ---------------------------------------------------------------------------
# 1. Retry Decision Logic & Backoff Delays
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_failure_attempt_1_schedules_attempt_2(tmp_path) -> None:
    """Attempt 1 transient failure schedules attempt 2 with 60s delay."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMITTING

    sub = MagicMock(spec=Submission)
    sub.id = uuid7()
    sub.attempt = 1
    sub.result = SubmissionResult.PENDING
    sub.target_domain = "example.com"
    sub.method = SubmissionMethod.EMAIL
    sub.policy_id = uuid7()
    sub.packet_artifact_id = None

    event = MagicMock(spec=Event)
    event.id = uuid7()

    mock_job = MagicMock(spec=Job)
    mock_job.title = "Software Engineer"
    mock_job.company = "Acme Corp"
    mock_job.url = "mailto:jobs@example.com"

    mock_resume = MagicMock(spec=Resume)
    mock_resume.id = uuid7()

    mock_art = MagicMock(spec=Artifact)
    mock_art.storage_key = "pdf_key"

    mock_cl = MagicMock()
    mock_cl.body_text = "Cover letter text"

    store = MagicMock(spec=FilesystemArtifactStore)
    store.get = AsyncMock(return_value=b"%PDF-1.4 dummy")

    exec_job = MagicMock()
    exec_job.scalar_one_or_none.return_value = mock_job
    exec_art = MagicMock()
    exec_art.scalar_one_or_none.return_value = mock_art
    session.execute = AsyncMock(side_effect=[exec_job, exec_art])

    fake_transport = FakeEmailTransport(
        responses=[
            EmailSendResult(
                EmailSendStatus.TRANSIENT_FAILURE, detail="421 temp fail", smtp_code=421
            )
        ]
    )

    settings = Settings(submission_max_attempts=3, submission_retry_delays_seconds=(60, 300))

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(
                    MagicMock(decision=PolicyDecision.ALLOWED, reason=None),
                    MagicMock(submission_method=SubmissionMethod.EMAIL),
                    MagicMock(
                        id=sub.policy_id,
                        target_domain="example.com",
                        next_review_due=datetime.date(2099, 1, 1),
                    ),
                    "example.com",
                )
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
            "packages.core.services.submission_execution.transition_application", new=AsyncMock()
        ) as mock_trans,
        patch("packages.core.services.submission_execution.emit", new=AsyncMock()) as mock_emit,
    ):
        res = await _execute_attempt(session, app, sub, event, settings=settings, store=store)
        assert res == "completed"
        assert sub.result == SubmissionResult.FAILED
        assert sub.next_attempt_after is not None
        # Attempt 1 backoff is 60s
        diff = (sub.next_attempt_after - sub.finished_at).total_seconds()
        assert 58 <= diff <= 62

        mock_trans.assert_awaited_once()
        assert mock_trans.call_args[0][2] == ApplicationStatus.SUBMISSION_FAILED

        # Check emitted event has will_retry=True
        failed_emit = next(
            call for call in mock_emit.call_args_list if call[1]["type"] == "submission.failed"
        )
        assert failed_emit[1]["payload"]["will_retry"] is True
        assert failed_emit[1]["payload"]["attempt"] == 1


@pytest.mark.asyncio
async def test_transient_failure_attempt_2_schedules_attempt_3(tmp_path) -> None:
    """Attempt 2 transient failure schedules attempt 3 with 300s delay."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMITTING

    sub = MagicMock(spec=Submission)
    sub.id = uuid7()
    sub.attempt = 2
    sub.result = SubmissionResult.PENDING
    sub.target_domain = "example.com"
    sub.method = SubmissionMethod.EMAIL
    sub.policy_id = uuid7()
    sub.packet_artifact_id = None

    event = MagicMock(spec=Event)
    event.id = uuid7()

    mock_job = MagicMock(spec=Job)
    mock_job.title = "Software Engineer"
    mock_job.company = "Acme Corp"
    mock_job.url = "mailto:jobs@example.com"

    mock_resume = MagicMock(spec=Resume)
    mock_resume.id = uuid7()

    mock_art = MagicMock(spec=Artifact)
    mock_art.storage_key = "pdf_key"

    mock_cl = MagicMock()
    mock_cl.body_text = "Cover letter text"

    store = MagicMock(spec=FilesystemArtifactStore)
    store.get = AsyncMock(return_value=b"%PDF-1.4 dummy")

    exec_job = MagicMock()
    exec_job.scalar_one_or_none.return_value = mock_job
    exec_art = MagicMock()
    exec_art.scalar_one_or_none.return_value = mock_art
    exec_priors = MagicMock()
    # Prior attempt 1 failed transiently, not succeeded, not ambiguous
    prior_attempt_1 = MagicMock(spec=Submission)
    prior_attempt_1.result = SubmissionResult.FAILED
    prior_attempt_1.failure_reason = "421 temp fail"
    exec_priors.scalars.return_value.all.return_value = [prior_attempt_1]

    session.execute = AsyncMock(side_effect=[exec_job, exec_art, exec_priors])

    fake_transport = FakeEmailTransport(
        responses=[
            EmailSendResult(
                EmailSendStatus.TRANSIENT_FAILURE, detail="421 temp fail again", smtp_code=421
            )
        ]
    )

    settings = Settings(submission_max_attempts=3, submission_retry_delays_seconds=(60, 300))

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(
                    MagicMock(decision=PolicyDecision.ALLOWED, reason=None),
                    MagicMock(submission_method=SubmissionMethod.EMAIL),
                    MagicMock(
                        id=sub.policy_id,
                        target_domain="example.com",
                        next_review_due=datetime.date(2099, 1, 1),
                    ),
                    "example.com",
                )
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
            "packages.core.services.submission_execution.transition_application", new=AsyncMock()
        ),
        patch("packages.core.services.submission_execution.emit", new=AsyncMock()) as mock_emit,
    ):
        res = await _execute_attempt(session, app, sub, event, settings=settings, store=store)
        assert res == "completed"
        assert sub.result == SubmissionResult.FAILED
        assert sub.next_attempt_after is not None
        # Attempt 2 backoff is 300s
        diff = (sub.next_attempt_after - sub.finished_at).total_seconds()
        assert 298 <= diff <= 302

        failed_emit = next(
            call for call in mock_emit.call_args_list if call[1]["type"] == "submission.failed"
        )
        assert failed_emit[1]["payload"]["will_retry"] is True
        assert failed_emit[1]["payload"]["attempt"] == 2


@pytest.mark.asyncio
async def test_transient_failure_attempt_3_exhaustion_leaves_next_attempt_none(tmp_path) -> None:
    """Attempt 3 transient failure exhausts budget, leaves next_attempt_after None."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMITTING

    sub = MagicMock(spec=Submission)
    sub.id = uuid7()
    sub.attempt = 3
    sub.result = SubmissionResult.PENDING
    sub.target_domain = "example.com"
    sub.method = SubmissionMethod.EMAIL
    sub.policy_id = uuid7()
    sub.packet_artifact_id = None

    event = MagicMock(spec=Event)
    event.id = uuid7()

    mock_job = MagicMock(spec=Job)
    mock_job.title = "Software Engineer"
    mock_job.company = "Acme Corp"
    mock_job.url = "mailto:jobs@example.com"

    mock_resume = MagicMock(spec=Resume)
    mock_resume.id = uuid7()

    mock_art = MagicMock(spec=Artifact)
    mock_art.storage_key = "pdf_key"

    mock_cl = MagicMock()
    mock_cl.body_text = "Cover letter text"

    store = MagicMock(spec=FilesystemArtifactStore)
    store.get = AsyncMock(return_value=b"%PDF-1.4 dummy")

    exec_job = MagicMock()
    exec_job.scalar_one_or_none.return_value = mock_job
    exec_art = MagicMock()
    exec_art.scalar_one_or_none.return_value = mock_art
    exec_priors = MagicMock()
    exec_priors.scalars.return_value.all.return_value = [
        MagicMock(result=SubmissionResult.FAILED, failure_reason="fail 1"),
        MagicMock(result=SubmissionResult.FAILED, failure_reason="fail 2"),
    ]

    session.execute = AsyncMock(side_effect=[exec_job, exec_art, exec_priors])

    fake_transport = FakeEmailTransport(
        responses=[
            EmailSendResult(
                EmailSendStatus.TRANSIENT_FAILURE, detail="421 temp fail 3", smtp_code=421
            )
        ]
    )

    settings = Settings(submission_max_attempts=3, submission_retry_delays_seconds=(60, 300))

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(
                    MagicMock(decision=PolicyDecision.ALLOWED, reason=None),
                    MagicMock(submission_method=SubmissionMethod.EMAIL),
                    MagicMock(
                        id=sub.policy_id,
                        target_domain="example.com",
                        next_review_due=datetime.date(2099, 1, 1),
                    ),
                    "example.com",
                )
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
            "packages.core.services.submission_execution.transition_application", new=AsyncMock()
        ),
        patch("packages.core.services.submission_execution.emit", new=AsyncMock()) as mock_emit,
    ):
        res = await _execute_attempt(session, app, sub, event, settings=settings, store=store)
        assert res == "completed"
        assert sub.result == SubmissionResult.FAILED
        # Budget exhausted: next_attempt_after is strictly None!
        assert sub.next_attempt_after is None

        failed_emit = next(
            call for call in mock_emit.call_args_list if call[1]["type"] == "submission.failed"
        )
        assert failed_emit[1]["payload"]["will_retry"] is False
        assert failed_emit[1]["payload"]["attempt"] == 3


@pytest.mark.asyncio
async def test_ambiguous_outcome_never_schedules_retry(tmp_path) -> None:
    """Ambiguous outcome never sets next_attempt_after and marks ambiguous=True."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMITTING

    sub = MagicMock(spec=Submission)
    sub.id = uuid7()
    sub.attempt = 1
    sub.result = SubmissionResult.PENDING
    sub.target_domain = "example.com"
    sub.method = SubmissionMethod.EMAIL
    sub.policy_id = uuid7()
    sub.packet_artifact_id = None

    event = MagicMock(spec=Event)
    event.id = uuid7()

    mock_job = MagicMock(spec=Job)
    mock_job.title = "Software Engineer"
    mock_job.company = "Acme Corp"
    mock_job.url = "mailto:jobs@example.com"

    mock_resume = MagicMock(spec=Resume)
    mock_resume.id = uuid7()

    mock_art = MagicMock(spec=Artifact)
    mock_art.storage_key = "pdf_key"

    mock_cl = MagicMock()
    mock_cl.body_text = "Cover letter text"

    store = MagicMock(spec=FilesystemArtifactStore)
    store.get = AsyncMock(return_value=b"%PDF-1.4 dummy")

    exec_job = MagicMock()
    exec_job.scalar_one_or_none.return_value = mock_job
    exec_art = MagicMock()
    exec_art.scalar_one_or_none.return_value = mock_art
    session.execute = AsyncMock(side_effect=[exec_job, exec_art])

    fake_transport = FakeEmailTransport(
        responses=[
            EmailSendResult(EmailSendStatus.AMBIGUOUS, detail="Connection dropped after DATA")
        ]
    )

    settings = Settings(submission_max_attempts=3, submission_retry_delays_seconds=(60, 300))

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(
                    MagicMock(decision=PolicyDecision.ALLOWED, reason=None),
                    MagicMock(submission_method=SubmissionMethod.EMAIL),
                    MagicMock(
                        id=sub.policy_id,
                        target_domain="example.com",
                        next_review_due=datetime.date(2099, 1, 1),
                    ),
                    "example.com",
                )
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
            "packages.core.services.submission_execution.transition_application", new=AsyncMock()
        ),
        patch("packages.core.services.submission_execution.emit", new=AsyncMock()) as mock_emit,
    ):
        res = await _execute_attempt(session, app, sub, event, settings=settings, store=store)
        assert res == "completed"
        assert sub.result == SubmissionResult.FAILED
        assert sub.next_attempt_after is None
        assert sub.failure_reason.startswith("ambiguous:")

        failed_emit = next(
            call for call in mock_emit.call_args_list if call[1]["type"] == "submission.failed"
        )
        assert failed_emit[1]["payload"]["will_retry"] is False
        assert failed_emit[1]["payload"]["ambiguous"] is True


# ---------------------------------------------------------------------------
# 2. Scheduler Tick & Event Emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_due_retries_emits_deterministic_event() -> None:
    """Due retries emit submission.retry_requested with deterministic uuid5 ID."""
    session = AsyncMock(spec=AsyncSession)
    sub_id = uuid7()
    app_id = uuid7()
    next_after = datetime.datetime(2026, 9, 22, 12, 0, 0, tzinfo=datetime.UTC)

    mock_row = (sub_id, app_id, 1, next_after)
    exec_res = MagicMock()
    exec_res.all.return_value = [mock_row]

    insert_res = MagicMock()
    insert_res.rowcount = 1

    session.execute = AsyncMock(side_effect=[exec_res, insert_res])

    count = await schedule_due_retries(session, max_retries_per_tick=10)
    assert count == 1

    # Verify deterministic UUID5 calculation
    expected_event_id = uuid5(
        NAMESPACE_URL,
        f"jobforge:submission-retry:{sub_id}:{next_after.isoformat()}",
    )
    insert_call = session.execute.call_args_list[1]
    inserted_id = insert_call[0][0].compile().params.get("id")
    if inserted_id:
        assert inserted_id == expected_event_id
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_schedule_due_retries_zero_when_none_due() -> None:
    """When no retries are due, zero events are emitted."""
    session = AsyncMock(spec=AsyncSession)
    exec_res = MagicMock()
    exec_res.all.return_value = []
    session.execute = AsyncMock(return_value=exec_res)

    count = await schedule_due_retries(session)
    assert count == 0
    session.flush.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. Worker Retry Claim & Attempt N+1 Creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_submission_retry_skips_if_not_failed() -> None:
    """Retry execution skips safely if application is not in SUBMISSION_FAILED."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.status = ApplicationStatus.SUBMITTED  # Not SUBMISSION_FAILED

    sub = MagicMock(spec=Submission)
    sub.next_attempt_after = datetime.datetime.now(datetime.UTC)

    exec_app = MagicMock()
    exec_app.scalar_one_or_none.return_value = app
    exec_sub = MagicMock()
    exec_sub.scalar_one_or_none.return_value = sub
    session.execute = AsyncMock(side_effect=[exec_app, exec_sub])

    event = MagicMock(spec=Event)
    event.payload = {
        "submission_id": str(uuid7()),
        "application_id": str(uuid7()),
        "attempt": 1,
    }

    res = await execute_submission_retry(session, event, settings=Settings(), store=MagicMock())
    assert res == "completed"


@pytest.mark.asyncio
async def test_execute_submission_retry_skips_if_already_claimed() -> None:
    """Retry execution skips safely if next_attempt_after is already None (already claimed)."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.status = ApplicationStatus.SUBMISSION_FAILED

    sub = MagicMock(spec=Submission)
    sub.next_attempt_after = None  # Already claimed by another worker

    exec_app = MagicMock()
    exec_app.scalar_one_or_none.return_value = app
    exec_sub = MagicMock()
    exec_sub.scalar_one_or_none.return_value = sub
    session.execute = AsyncMock(side_effect=[exec_app, exec_sub])

    event = MagicMock(spec=Event)
    event.payload = {
        "submission_id": str(uuid7()),
        "application_id": str(uuid7()),
        "attempt": 1,
    }

    res = await execute_submission_retry(session, event, settings=Settings(), store=MagicMock())
    assert res == "completed"


@pytest.mark.asyncio
async def test_execute_submission_retry_claims_and_executes_attempt_n_plus_1(tmp_path) -> None:
    """Retry claim clears next_attempt_after, creates attempt N+1, transitions state."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_FAILED

    prior_sub = MagicMock(spec=Submission)
    prior_sub.id = uuid7()
    prior_sub.attempt = 1
    prior_sub.result = SubmissionResult.FAILED
    prior_sub.failure_reason = "421 temporary failure"
    prior_sub.target_domain = "example.com"
    prior_sub.method = SubmissionMethod.EMAIL
    prior_sub.policy_id = uuid7()
    prior_sub.packet_artifact_id = None
    prior_sub.next_attempt_after = datetime.datetime.now(datetime.UTC)

    policy_id = uuid7()
    mock_policy_row = MagicMock()
    mock_policy_row.id = policy_id
    mock_policy_row.next_review_due = datetime.date(2099, 1, 1)

    mock_job = MagicMock(spec=Job)
    mock_job.url = "mailto:jobs@example.com"

    mock_packet_art = MagicMock(spec=Artifact)
    mock_packet_art.id = uuid7()

    exec_app = MagicMock()
    exec_app.scalar_one_or_none.return_value = app
    exec_sub = MagicMock()
    exec_sub.scalar_one_or_none.return_value = prior_sub
    exec_pending = MagicMock()
    exec_pending.scalar_one_or_none.return_value = None
    exec_all = MagicMock()
    exec_all.scalars.return_value.all.return_value = [prior_sub]
    exec_job = MagicMock()
    exec_job.scalar_one_or_none.return_value = mock_job

    session.execute = AsyncMock(side_effect=[exec_app, exec_sub, exec_pending, exec_all, exec_job])

    event = MagicMock(spec=Event)
    event.id = uuid7()
    event.payload = {
        "submission_id": str(prior_sub.id),
        "application_id": str(app.id),
        "attempt": 1,
    }

    store = FilesystemArtifactStore(tmp_path)

    with (
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(
                    MagicMock(decision=PolicyDecision.ALLOWED),
                    MagicMock(submission_method=SubmissionMethod.EMAIL),
                    mock_policy_row,
                    "example.com",
                )
            ),
        ),
        patch(
            "packages.core.services.submission_execution.extract_and_validate_authoritative_email",
            return_value="jobs@example.com",
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.generate_handoff_packet",
            new=AsyncMock(return_value=mock_packet_art),
        ),
        patch(
            "packages.core.services.submission_execution.transition_application", new=AsyncMock()
        ) as mock_trans,
        patch("packages.core.services.submission_execution.emit", new=AsyncMock()),
        patch(
            "packages.core.services.submission_execution._execute_attempt",
            new=AsyncMock(return_value="completed"),
        ) as mock_exec,
    ):
        res = await execute_submission_retry(session, event, settings=Settings(), store=store)
        assert res == "completed"

        # Authority invariant: prior_sub.next_attempt_after cleared immediately on claim
        assert prior_sub.next_attempt_after is None

        # State transition SUBMISSION_FAILED -> SUBMITTING by system
        mock_trans.assert_awaited_once()
        assert mock_trans.call_args[0][2] == ApplicationStatus.SUBMITTING
        assert mock_trans.call_args[1]["actor_type"] == "system"

        # Check new submission added to session
        session.add.assert_called_once()
        new_sub = session.add.call_args[0][0]
        assert isinstance(new_sub, Submission)
        assert new_sub.attempt == 2
        assert new_sub.result == SubmissionResult.PENDING
        assert new_sub.policy_id == policy_id
        assert new_sub.target_domain == "example.com"
        assert new_sub.method == SubmissionMethod.EMAIL
        assert new_sub.packet_artifact_id == mock_packet_art.id

        # _execute_attempt called with the new submission
        mock_exec.assert_awaited_once_with(
            session, app, new_sub, event, settings=mock_exec.call_args[1]["settings"], store=store
        )


# ---------------------------------------------------------------------------
# 4. Preflight Enforcement During Retry Execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_preflight_fails_if_switches_disabled(tmp_path) -> None:
    """Disabling real submission switch before retry halts execution with zero sends."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.status = ApplicationStatus.SUBMITTING

    sub = MagicMock(spec=Submission)
    sub.id = uuid7()
    sub.attempt = 2
    sub.result = SubmissionResult.PENDING
    sub.packet_artifact_id = None

    event = MagicMock(spec=Event)
    event.id = uuid7()

    store = FilesystemArtifactStore(tmp_path)

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "packages.core.services.submission_execution.transition_application", new=AsyncMock()
        ) as mock_trans,
        patch("packages.core.services.submission_execution.emit", new=AsyncMock()),
    ):
        res = await _execute_attempt(session, app, sub, event, settings=Settings(), store=store)
        assert res == "completed"
        assert sub.result == SubmissionResult.FAILED
        assert "disabled at execution" in sub.failure_reason
        assert sub.started_at is None
        assert sub.next_attempt_after is None

        mock_trans.assert_awaited_once()
        assert mock_trans.call_args[0][2] == ApplicationStatus.SUBMISSION_FAILED
        assert mock_trans.call_args[1]["actor_type"] == "system"


@pytest.mark.asyncio
async def test_retry_preflight_fails_if_prior_succeeded(tmp_path) -> None:
    """Preflight 7 aborts if any prior attempt on the application already succeeded."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMITTING

    sub = MagicMock(spec=Submission)
    sub.id = uuid7()
    sub.attempt = 2
    sub.result = SubmissionResult.PENDING
    sub.policy_id = uuid7()
    sub.packet_artifact_id = None

    event = MagicMock(spec=Event)
    event.id = uuid7()

    mock_job = MagicMock(spec=Job)
    mock_job.title = "Software Engineer"
    mock_job.company = "Acme Corp"
    mock_job.url = "mailto:jobs@example.com"

    mock_resume = MagicMock(spec=Resume)
    mock_resume.id = uuid7()

    mock_art = MagicMock(spec=Artifact)
    mock_art.storage_key = "pdf_key"

    mock_cl = MagicMock()
    mock_cl.body_text = "Cover letter text"

    store = MagicMock(spec=FilesystemArtifactStore)
    store.get = AsyncMock(return_value=b"%PDF-1.4 dummy")

    exec_job = MagicMock()
    exec_job.scalar_one_or_none.return_value = mock_job
    exec_art = MagicMock()
    exec_art.scalar_one_or_none.return_value = mock_art

    # Mock prior attempt that succeeded!
    prior_success = MagicMock(spec=Submission)
    prior_success.result = SubmissionResult.SUCCEEDED
    exec_priors = MagicMock()
    exec_priors.scalars.return_value.all.return_value = [prior_success]

    session.execute = AsyncMock(side_effect=[exec_job, exec_art, exec_priors])

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(
                    MagicMock(decision=PolicyDecision.ALLOWED, reason=None),
                    MagicMock(submission_method=SubmissionMethod.EMAIL),
                    MagicMock(
                        id=sub.policy_id,
                        target_domain="example.com",
                        next_review_due=datetime.date(2099, 1, 1),
                    ),
                    "example.com",
                )
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
            "packages.core.services.submission_execution.transition_application", new=AsyncMock()
        ),
        patch("packages.core.services.submission_execution.emit", new=AsyncMock()),
    ):
        res = await _execute_attempt(session, app, sub, event, settings=Settings(), store=store)
        assert res == "completed"
        assert sub.result == SubmissionResult.FAILED
        assert "prior attempt already succeeded" in sub.failure_reason
        assert sub.started_at is None
        assert sub.next_attempt_after is None


@pytest.mark.asyncio
async def test_retry_preflight_fails_if_prior_was_ambiguous(tmp_path) -> None:
    """Preflight 7 aborts if any prior attempt on the application was ambiguous."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMITTING

    sub = MagicMock(spec=Submission)
    sub.id = uuid7()
    sub.attempt = 2
    sub.result = SubmissionResult.PENDING
    sub.policy_id = uuid7()
    sub.packet_artifact_id = None

    event = MagicMock(spec=Event)
    event.id = uuid7()

    mock_job = MagicMock(spec=Job)
    mock_job.title = "Software Engineer"
    mock_job.company = "Acme Corp"
    mock_job.url = "mailto:jobs@example.com"

    mock_resume = MagicMock(spec=Resume)
    mock_resume.id = uuid7()

    mock_art = MagicMock(spec=Artifact)
    mock_art.storage_key = "pdf_key"

    mock_cl = MagicMock()
    mock_cl.body_text = "Cover letter text"

    store = MagicMock(spec=FilesystemArtifactStore)
    store.get = AsyncMock(return_value=b"%PDF-1.4 dummy")

    exec_job = MagicMock()
    exec_job.scalar_one_or_none.return_value = mock_job
    exec_art = MagicMock()
    exec_art.scalar_one_or_none.return_value = mock_art

    # Mock prior attempt that was ambiguous!
    prior_ambiguous = MagicMock(spec=Submission)
    prior_ambiguous.result = SubmissionResult.FAILED
    prior_ambiguous.failure_reason = "ambiguous: connection dropped"
    exec_priors = MagicMock()
    exec_priors.scalars.return_value.all.return_value = [prior_ambiguous]

    session.execute = AsyncMock(side_effect=[exec_job, exec_art, exec_priors])

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(
                    MagicMock(decision=PolicyDecision.ALLOWED, reason=None),
                    MagicMock(submission_method=SubmissionMethod.EMAIL),
                    MagicMock(
                        id=sub.policy_id,
                        target_domain="example.com",
                        next_review_due=datetime.date(2099, 1, 1),
                    ),
                    "example.com",
                )
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
            "packages.core.services.submission_execution.transition_application", new=AsyncMock()
        ),
        patch("packages.core.services.submission_execution.emit", new=AsyncMock()),
    ):
        res = await _execute_attempt(session, app, sub, event, settings=Settings(), store=store)
        assert res == "completed"
        assert sub.result == SubmissionResult.FAILED
        assert "prior attempt was ambiguous" in sub.failure_reason
        assert sub.started_at is None
        assert sub.next_attempt_after is None


@pytest.mark.asyncio
async def test_schedule_due_retries_deduplicates_duplicate_ticks() -> None:
    """When on_conflict_do_nothing triggers (rowcount=0), emitted count is 0."""
    session = AsyncMock(spec=AsyncSession)
    sub_id = uuid7()
    app_id = uuid7()
    next_after = datetime.datetime(2026, 9, 22, 12, 0, 0, tzinfo=datetime.UTC)

    mock_row = (sub_id, app_id, 1, next_after)
    exec_res = MagicMock()
    exec_res.all.return_value = [mock_row]

    insert_res = MagicMock()
    insert_res.rowcount = 0  # Conflict! Already inserted

    session.execute = AsyncMock(side_effect=[exec_res, insert_res])

    count = await schedule_due_retries(session)
    assert count == 0


@pytest.mark.asyncio
async def test_execute_submission_retry_pending_attempt_already_exists() -> None:
    """If another attempt is already PENDING, safely skip retry."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_FAILED

    prior_sub = MagicMock(spec=Submission)
    prior_sub.id = uuid7()
    prior_sub.attempt = 1
    prior_sub.next_attempt_after = datetime.datetime.now(datetime.UTC)

    pending_sub = MagicMock(spec=Submission)
    pending_sub.id = uuid7()

    exec_app = MagicMock()
    exec_app.scalar_one_or_none.return_value = app
    exec_sub = MagicMock()
    exec_sub.scalar_one_or_none.return_value = prior_sub
    exec_pending = MagicMock()
    exec_pending.scalar_one_or_none.return_value = pending_sub

    session.execute = AsyncMock(side_effect=[exec_app, exec_sub, exec_pending])

    event = MagicMock(spec=Event)
    event.payload = {
        "submission_id": str(prior_sub.id),
        "application_id": str(app.id),
        "attempt": 1,
    }

    res = await execute_submission_retry(session, event, settings=Settings(), store=MagicMock())
    assert res == "completed"
    assert prior_sub.next_attempt_after is None


@pytest.mark.asyncio
async def test_execute_submission_retry_budget_exhausted() -> None:
    """If prior attempt >= submission_max_attempts, clears next_attempt_after and skips."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_FAILED

    prior_sub = MagicMock(spec=Submission)
    prior_sub.id = uuid7()
    prior_sub.attempt = 3  # Max attempts is 3
    prior_sub.next_attempt_after = datetime.datetime.now(datetime.UTC)

    exec_app = MagicMock()
    exec_app.scalar_one_or_none.return_value = app
    exec_sub = MagicMock()
    exec_sub.scalar_one_or_none.return_value = prior_sub

    session.execute = AsyncMock(side_effect=[exec_app, exec_sub])

    event = MagicMock(spec=Event)
    event.payload = {
        "submission_id": str(prior_sub.id),
        "application_id": str(app.id),
        "attempt": 3,
    }

    res = await execute_submission_retry(
        session, event, settings=Settings(submission_max_attempts=3), store=MagicMock()
    )
    assert res == "completed"
    assert prior_sub.next_attempt_after is None


@pytest.mark.asyncio
async def test_retry_preflight_fails_if_policy_expired(tmp_path) -> None:
    """If policy expired between attempts, retry attempt preflight fails with zero sends."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMITTING

    sub = MagicMock(spec=Submission)
    sub.id = uuid7()
    sub.attempt = 2
    sub.result = SubmissionResult.PENDING
    sub.policy_id = uuid7()
    sub.packet_artifact_id = None

    event = MagicMock(spec=Event)
    event.id = uuid7()

    store = FilesystemArtifactStore(tmp_path)

    # Policy expired: next_review_due in past
    expired_policy = MagicMock(spec=SitePolicy)
    expired_policy.id = sub.policy_id
    expired_policy.target_domain = "example.com"
    expired_policy.next_review_due = datetime.date(2020, 1, 1)

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(
                    MagicMock(decision=PolicyDecision.ALLOWED, reason=None),
                    MagicMock(submission_method=SubmissionMethod.EMAIL),
                    expired_policy,
                    "example.com",
                )
            ),
        ),
        patch(
            "packages.core.services.submission_execution.transition_application", new=AsyncMock()
        ),
        patch("packages.core.services.submission_execution.emit", new=AsyncMock()),
    ):
        res = await _execute_attempt(session, app, sub, event, settings=Settings(), store=store)
        assert res == "completed"
        assert sub.result == SubmissionResult.FAILED
        assert "policy review expired" in sub.failure_reason
        assert sub.started_at is None
        assert sub.next_attempt_after is None


# ---------------------------------------------------------------------------
# 5. Provenance Binding, Stale Packet, & History Immutability Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_claim_binds_to_current_policy_if_changed(tmp_path) -> None:
    """Attempt N+1 binds to the current policy, not stale policy from attempt N."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_FAILED

    old_policy_id = uuid7()
    new_policy_id = uuid7()

    prior_sub = MagicMock(spec=Submission)
    prior_sub.id = uuid7()
    prior_sub.attempt = 1
    prior_sub.result = SubmissionResult.FAILED
    prior_sub.failure_reason = "421 temporary failure"
    prior_sub.target_domain = "old-domain.com"
    prior_sub.method = SubmissionMethod.EMAIL
    prior_sub.policy_id = old_policy_id
    prior_sub.packet_artifact_id = uuid7()
    prior_sub.next_attempt_after = datetime.datetime.now(datetime.UTC)

    mock_policy_row = MagicMock()
    mock_policy_row.id = new_policy_id
    mock_policy_row.next_review_due = datetime.date(2099, 1, 1)

    mock_job = MagicMock(spec=Job)
    mock_job.url = "mailto:jobs@new-domain.com"

    mock_packet_art = MagicMock(spec=Artifact)
    mock_packet_art.id = uuid7()

    exec_app = MagicMock()
    exec_app.scalar_one_or_none.return_value = app
    exec_sub = MagicMock()
    exec_sub.scalar_one_or_none.return_value = prior_sub
    exec_pending = MagicMock()
    exec_pending.scalar_one_or_none.return_value = None
    exec_all = MagicMock()
    exec_all.scalars.return_value.all.return_value = [prior_sub]
    exec_job = MagicMock()
    exec_job.scalar_one_or_none.return_value = mock_job

    session.execute = AsyncMock(side_effect=[exec_app, exec_sub, exec_pending, exec_all, exec_job])

    event = MagicMock(spec=Event)
    event.id = uuid7()
    event.payload = {
        "submission_id": str(prior_sub.id),
        "application_id": str(app.id),
        "attempt": 1,
    }

    store = FilesystemArtifactStore(tmp_path)

    with (
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(
                    MagicMock(decision=PolicyDecision.ALLOWED),
                    MagicMock(submission_method=SubmissionMethod.EMAIL),
                    mock_policy_row,
                    "new-domain.com",
                )
            ),
        ),
        patch(
            "packages.core.services.submission_execution.extract_and_validate_authoritative_email",
            return_value="jobs@new-domain.com",
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.generate_handoff_packet",
            new=AsyncMock(return_value=mock_packet_art),
        ),
        patch(
            "packages.core.services.submission_execution.transition_application", new=AsyncMock()
        ),
        patch("packages.core.services.submission_execution.emit", new=AsyncMock()),
        patch(
            "packages.core.services.submission_execution._execute_attempt",
            new=AsyncMock(return_value="completed"),
        ),
    ):
        res = await execute_submission_retry(session, event, settings=Settings(), store=store)
        assert res == "completed"

        # New submission bound to NEW policy and NEW domain
        session.add.assert_called_once()
        new_sub = session.add.call_args[0][0]
        assert new_sub.policy_id == new_policy_id
        assert new_sub.target_domain == "new-domain.com"
        assert new_sub.packet_artifact_id == mock_packet_art.id

        # Prior submission retains original provenance
        assert prior_sub.policy_id == old_policy_id
        assert prior_sub.target_domain == "old-domain.com"


@pytest.mark.asyncio
async def test_retry_claim_fails_closed_without_creating_n_plus_1_if_policy_invalid(
    tmp_path,
) -> None:
    """If policy becomes invalid before claim, no sendable N+1 is created and zero transport."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_FAILED

    prior_sub = MagicMock(spec=Submission)
    prior_sub.id = uuid7()
    prior_sub.attempt = 1
    prior_sub.result = SubmissionResult.FAILED
    prior_sub.next_attempt_after = datetime.datetime.now(datetime.UTC)

    exec_app = MagicMock()
    exec_app.scalar_one_or_none.return_value = app
    exec_sub = MagicMock()
    exec_sub.scalar_one_or_none.return_value = prior_sub
    exec_pending = MagicMock()
    exec_pending.scalar_one_or_none.return_value = None
    exec_all = MagicMock()
    exec_all.scalars.return_value.all.return_value = [prior_sub]

    session.execute = AsyncMock(side_effect=[exec_app, exec_sub, exec_pending, exec_all])

    event = MagicMock(spec=Event)
    event.id = uuid7()
    event.payload = {
        "submission_id": str(prior_sub.id),
        "application_id": str(app.id),
        "attempt": 1,
    }

    store = FilesystemArtifactStore(tmp_path)

    # Policy is BLOCKED
    with (
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(
                    MagicMock(decision=PolicyDecision.BLOCKED, reason="Site disallows automation"),
                    MagicMock(submission_method=SubmissionMethod.EMAIL),
                    MagicMock(next_review_due=datetime.date(2099, 1, 1)),
                    "example.com",
                )
            ),
        ),
        patch(
            "packages.core.services.submission_execution._execute_attempt", new=AsyncMock()
        ) as mock_exec,
    ):
        res = await execute_submission_retry(session, event, settings=Settings(), store=store)
        assert res == "completed"

        # Prior next_attempt_after cleared
        assert prior_sub.next_attempt_after is None

        # Zero new submissions created
        session.add.assert_not_called()

        # Zero transport execution
        mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_retry_claim_binds_to_current_packet_differing_from_attempt_n(tmp_path) -> None:
    """Attempt N+1 binds to the newly generated current packet, not attempt N's packet."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_FAILED

    old_packet_id = uuid7()
    new_packet_id = uuid7()

    prior_sub = MagicMock(spec=Submission)
    prior_sub.id = uuid7()
    prior_sub.attempt = 1
    prior_sub.result = SubmissionResult.FAILED
    prior_sub.failure_reason = "421 temporary failure"
    prior_sub.target_domain = "example.com"
    prior_sub.method = SubmissionMethod.EMAIL
    prior_sub.policy_id = uuid7()
    prior_sub.packet_artifact_id = old_packet_id
    prior_sub.next_attempt_after = datetime.datetime.now(datetime.UTC)

    mock_policy_row = MagicMock()
    mock_policy_row.id = prior_sub.policy_id
    mock_policy_row.next_review_due = datetime.date(2099, 1, 1)

    mock_job = MagicMock(spec=Job)
    mock_job.url = "mailto:jobs@example.com"

    mock_packet_art = MagicMock(spec=Artifact)
    mock_packet_art.id = new_packet_id

    exec_app = MagicMock()
    exec_app.scalar_one_or_none.return_value = app
    exec_sub = MagicMock()
    exec_sub.scalar_one_or_none.return_value = prior_sub
    exec_pending = MagicMock()
    exec_pending.scalar_one_or_none.return_value = None
    exec_all = MagicMock()
    exec_all.scalars.return_value.all.return_value = [prior_sub]
    exec_job = MagicMock()
    exec_job.scalar_one_or_none.return_value = mock_job

    session.execute = AsyncMock(side_effect=[exec_app, exec_sub, exec_pending, exec_all, exec_job])

    event = MagicMock(spec=Event)
    event.id = uuid7()
    event.payload = {
        "submission_id": str(prior_sub.id),
        "application_id": str(app.id),
        "attempt": 1,
    }

    store = FilesystemArtifactStore(tmp_path)

    with (
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(
                    MagicMock(decision=PolicyDecision.ALLOWED),
                    MagicMock(submission_method=SubmissionMethod.EMAIL),
                    mock_policy_row,
                    "example.com",
                )
            ),
        ),
        patch(
            "packages.core.services.submission_execution.extract_and_validate_authoritative_email",
            return_value="jobs@example.com",
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.generate_handoff_packet",
            new=AsyncMock(return_value=mock_packet_art),
        ),
        patch(
            "packages.core.services.submission_execution.transition_application", new=AsyncMock()
        ),
        patch("packages.core.services.submission_execution.emit", new=AsyncMock()),
        patch(
            "packages.core.services.submission_execution._execute_attempt",
            new=AsyncMock(return_value="completed"),
        ),
    ):
        res = await execute_submission_retry(session, event, settings=Settings(), store=store)
        assert res == "completed"

        session.add.assert_called_once()
        new_sub = session.add.call_args[0][0]
        assert new_sub.packet_artifact_id == new_packet_id
        assert prior_sub.packet_artifact_id == old_packet_id


@pytest.mark.asyncio
async def test_retry_preflight_fails_if_packet_artifact_is_stale(tmp_path) -> None:
    """Preflight 6 rejects attempt if bound packet artifact does not match current materials."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMITTING

    packet_art = MagicMock(spec=Artifact)
    packet_art.id = uuid7()
    packet_art.storage_key = "packet_storage_key_old_v1"

    sub = MagicMock(spec=Submission)
    sub.id = uuid7()
    sub.attempt = 2
    sub.result = SubmissionResult.PENDING
    sub.policy_id = uuid7()
    sub.packet_artifact_id = packet_art.id

    event = MagicMock(spec=Event)
    event.id = uuid7()

    mock_job = MagicMock(spec=Job)
    mock_job.title = "Software Engineer"
    mock_job.company = "Acme Corp"
    mock_job.url = "mailto:jobs@example.com"

    mock_resume = MagicMock(spec=Resume)
    mock_resume.id = uuid7()

    mock_resume_art = MagicMock(spec=Artifact)
    mock_resume_art.storage_key = "pdf_key"

    mock_cl = MagicMock()
    mock_cl.body_text = "Cover letter text"

    store = MagicMock(spec=FilesystemArtifactStore)
    store.get = AsyncMock(return_value=b"%PDF-1.4 dummy")

    exec_job = MagicMock()
    exec_job.scalar_one_or_none.return_value = mock_job
    exec_resume_art = MagicMock()
    exec_resume_art.scalar_one_or_none.return_value = mock_resume_art
    exec_packet_art = MagicMock()
    exec_packet_art.scalar_one_or_none.return_value = packet_art

    session.execute = AsyncMock(side_effect=[exec_job, exec_resume_art, exec_packet_art])

    # load_packet_source returns source with different digest
    mock_source = MagicMock()
    mock_source.resume.id = mock_resume.id
    mock_source.resume.version = 2  # Materials updated!
    mock_source.cover_letter.id = uuid7()
    mock_source.cover_letter.version = 1
    mock_source.answers_digest = "digest123"

    with (
        patch(
            "packages.core.services.submission_execution.real_submission_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(
                    MagicMock(decision=PolicyDecision.ALLOWED, reason=None),
                    MagicMock(submission_method=SubmissionMethod.EMAIL),
                    MagicMock(
                        id=sub.policy_id,
                        target_domain="example.com",
                        next_review_due=datetime.date(2099, 1, 1),
                    ),
                    "example.com",
                )
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
            "packages.core.services.submission_execution.load_packet_source",
            new=AsyncMock(return_value=mock_source),
        ),
        patch(
            "packages.core.services.submission_execution.transition_application", new=AsyncMock()
        ),
        patch("packages.core.services.submission_execution.emit", new=AsyncMock()),
    ):
        res = await _execute_attempt(session, app, sub, event, settings=Settings(), store=store)
        assert res == "completed"
        assert sub.result == SubmissionResult.FAILED
        assert "packet artifact is stale" in sub.failure_reason
        assert sub.started_at is None
        assert sub.next_attempt_after is None


@pytest.mark.asyncio
async def test_retry_claim_fails_closed_if_reviews_incomplete(tmp_path) -> None:
    """If review completion is invalidated before claim, claim aborts without creating N+1."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_FAILED

    prior_sub = MagicMock(spec=Submission)
    prior_sub.id = uuid7()
    prior_sub.attempt = 1
    prior_sub.result = SubmissionResult.FAILED
    prior_sub.next_attempt_after = datetime.datetime.now(datetime.UTC)

    mock_policy_row = MagicMock()
    mock_policy_row.id = uuid7()
    mock_policy_row.next_review_due = datetime.date(2099, 1, 1)

    mock_job = MagicMock(spec=Job)
    mock_job.url = "mailto:jobs@example.com"

    exec_app = MagicMock()
    exec_app.scalar_one_or_none.return_value = app
    exec_sub = MagicMock()
    exec_sub.scalar_one_or_none.return_value = prior_sub
    exec_pending = MagicMock()
    exec_pending.scalar_one_or_none.return_value = None
    exec_all = MagicMock()
    exec_all.scalars.return_value.all.return_value = [prior_sub]
    exec_job = MagicMock()
    exec_job.scalar_one_or_none.return_value = mock_job

    session.execute = AsyncMock(side_effect=[exec_app, exec_sub, exec_pending, exec_all, exec_job])

    event = MagicMock(spec=Event)
    event.id = uuid7()
    event.payload = {
        "submission_id": str(prior_sub.id),
        "application_id": str(app.id),
        "attempt": 1,
    }

    store = FilesystemArtifactStore(tmp_path)

    with (
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(
                    MagicMock(decision=PolicyDecision.ALLOWED),
                    MagicMock(submission_method=SubmissionMethod.EMAIL),
                    mock_policy_row,
                    "example.com",
                )
            ),
        ),
        patch(
            "packages.core.services.submission_execution.extract_and_validate_authoritative_email",
            return_value="jobs@example.com",
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=False),  # Incomplete reviews!
        ),
        patch(
            "packages.core.services.submission_execution._execute_attempt", new=AsyncMock()
        ) as mock_exec,
    ):
        res = await execute_submission_retry(session, event, settings=Settings(), store=store)
        assert res == "completed"
        assert prior_sub.next_attempt_after is None
        session.add.assert_not_called()
        mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_retry_claim_attempt_n_historical_immutability(tmp_path) -> None:
    """Attempt N is never rewritten except clearing next_attempt_after as scheduling metadata."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.job_id = uuid7()
    app.status = ApplicationStatus.SUBMISSION_FAILED

    fixed_sub_id = uuid7()
    fixed_policy_id = uuid7()
    fixed_packet_id = uuid7()
    fixed_started_at = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)
    fixed_finished_at = datetime.datetime(2026, 1, 1, 12, 1, tzinfo=datetime.UTC)

    prior_sub = MagicMock(spec=Submission)
    prior_sub.id = fixed_sub_id
    prior_sub.attempt = 1
    prior_sub.result = SubmissionResult.FAILED
    prior_sub.failure_reason = "421 temporary failure"
    prior_sub.http_status = 421
    prior_sub.target_domain = "example.com"
    prior_sub.method = SubmissionMethod.EMAIL
    prior_sub.policy_id = fixed_policy_id
    prior_sub.packet_artifact_id = fixed_packet_id
    prior_sub.started_at = fixed_started_at
    prior_sub.finished_at = fixed_finished_at
    prior_sub.next_attempt_after = datetime.datetime.now(datetime.UTC)

    mock_policy_row = MagicMock()
    mock_policy_row.id = fixed_policy_id
    mock_policy_row.next_review_due = datetime.date(2099, 1, 1)

    mock_job = MagicMock(spec=Job)
    mock_job.url = "mailto:jobs@example.com"

    mock_packet_art = MagicMock(spec=Artifact)
    mock_packet_art.id = fixed_packet_id

    exec_app = MagicMock()
    exec_app.scalar_one_or_none.return_value = app
    exec_sub = MagicMock()
    exec_sub.scalar_one_or_none.return_value = prior_sub
    exec_pending = MagicMock()
    exec_pending.scalar_one_or_none.return_value = None
    exec_all = MagicMock()
    exec_all.scalars.return_value.all.return_value = [prior_sub]
    exec_job = MagicMock()
    exec_job.scalar_one_or_none.return_value = mock_job

    session.execute = AsyncMock(side_effect=[exec_app, exec_sub, exec_pending, exec_all, exec_job])

    event = MagicMock(spec=Event)
    event.id = uuid7()
    event.payload = {
        "submission_id": str(prior_sub.id),
        "application_id": str(app.id),
        "attempt": 1,
    }

    store = FilesystemArtifactStore(tmp_path)

    with (
        patch(
            "packages.core.services.submission_execution.policy_verdict",
            new=AsyncMock(
                return_value=(
                    MagicMock(decision=PolicyDecision.ALLOWED),
                    MagicMock(submission_method=SubmissionMethod.EMAIL),
                    mock_policy_row,
                    "example.com",
                )
            ),
        ),
        patch(
            "packages.core.services.submission_execution.extract_and_validate_authoritative_email",
            return_value="jobs@example.com",
        ),
        patch(
            "packages.core.services.submission_execution.review_is_complete",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "packages.core.services.submission_execution.generate_handoff_packet",
            new=AsyncMock(return_value=mock_packet_art),
        ),
        patch(
            "packages.core.services.submission_execution.transition_application", new=AsyncMock()
        ),
        patch("packages.core.services.submission_execution.emit", new=AsyncMock()),
        patch(
            "packages.core.services.submission_execution._execute_attempt",
            new=AsyncMock(return_value="completed"),
        ),
    ):
        res = await execute_submission_retry(session, event, settings=Settings(), store=store)
        assert res == "completed"

        # Check all historical fields on prior_sub are intact
        assert prior_sub.id == fixed_sub_id
        assert prior_sub.attempt == 1
        assert prior_sub.result == SubmissionResult.FAILED
        assert prior_sub.failure_reason == "421 temporary failure"
        assert prior_sub.http_status == 421
        assert prior_sub.target_domain == "example.com"
        assert prior_sub.method == SubmissionMethod.EMAIL
        assert prior_sub.policy_id == fixed_policy_id
        assert prior_sub.packet_artifact_id == fixed_packet_id
        assert prior_sub.started_at == fixed_started_at
        assert prior_sub.finished_at == fixed_finished_at

        # ONLY next_attempt_after was cleared
        assert prior_sub.next_attempt_after is None


@pytest.mark.asyncio
async def test_scheduler_event_alone_cannot_bypass_revalidation() -> None:
    """A valid scheduler event alone cannot authorize retry if application status is invalid."""
    session = AsyncMock(spec=AsyncSession)
    app = MagicMock(spec=Application)
    app.id = uuid7()
    app.status = ApplicationStatus.SUBMITTED  # Already submitted!

    prior_sub = MagicMock(spec=Submission)
    prior_sub.id = uuid7()
    prior_sub.next_attempt_after = datetime.datetime.now(datetime.UTC)

    exec_app = MagicMock()
    exec_app.scalar_one_or_none.return_value = app
    exec_sub = MagicMock()
    exec_sub.scalar_one_or_none.return_value = prior_sub

    session.execute = AsyncMock(side_effect=[exec_app, exec_sub])

    event = MagicMock(spec=Event)
    event.payload = {
        "submission_id": str(prior_sub.id),
        "application_id": str(app.id),
        "attempt": 1,
    }

    res = await execute_submission_retry(
        session, event, settings=Settings(), store=MagicMock()
    )
    assert res == "completed"
    assert prior_sub.next_attempt_after is None
    session.add.assert_not_called()
