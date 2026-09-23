"""M11 Phase 3: Durable Retry & Recovery integration tests.

Verifies the complete retry and recovery boundary against PostgreSQL:
- Transient failure on Attempt 1 schedules retry with next_attempt_after
- Periodic scheduler discovers due rows and emits deterministic submission.retry_requested
- Deduplication prevents duplicate events on repeated scheduler ticks
- Submission worker claims attempt 1, clears next_attempt_after, transitions
  SUBMISSION_FAILED -> SUBMITTING (system actor), and creates attempt 2
- Successful attempt 2 transitions SUBMITTING -> SUBMITTED
- Attempt 2 transient failure schedules attempt 3
- Attempt 3 transient failure exhausts budget (next_attempt_after is NULL, no attempt 4)
- Ambiguous outcome on attempt 2 permanently halts retry
- Safety switch disabled between attempts prevents retry delivery with zero sends
- Scheduler tick executes cleanly under restricted app_monitor database role
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from packages.core.models import (
    ApplicationStatus,
    Event,
    SitePolicy,
    Submission,
    SubmissionMethod,
    SubmissionResult,
)
from packages.core.services.settings import (
    SUBMISSION_ENABLED_KEY,
    set_submission_dispatch_enabled,
)
from packages.core.services.submission_execution import (
    execute_submission_attempt,
    execute_submission_retry,
    schedule_due_retries,
)
from packages.services.delivery.email import (
    EmailSendResult,
    EmailSendStatus,
    FakeEmailTransport,
)
from packages.services.rendering.store import FilesystemArtifactStore
from packages.shared.clock import utc_now
from packages.shared.config import Settings
from tests.integration import test_submission_worker as worker_tests
from tests.integration import test_tailoring_context
from tests.integration.conftest import _url
from workers.runtime import verify_role

inputs = test_tailoring_context.inputs

pytestmark = [
    pytest.mark.integration,
    pytest.mark.safety,
    pytest.mark.usefixtures("audit_boundary_engine"),
]

DOMAIN = "example.invalid"


@pytest.fixture
def store(tmp_path):
    return FilesystemArtifactStore(tmp_path / "artifacts")


@pytest_asyncio.fixture(autouse=True)
async def _clean_all(engine):
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def _clean() -> None:
        async with maker() as conn:
            await conn.execute(text("DELETE FROM submissions"))
            await conn.execute(
                text("DELETE FROM site_policies WHERE domain = :domain"),
                {"domain": DOMAIN},
            )
            await conn.execute(
                text("DELETE FROM settings WHERE key = :key"),
                {"key": SUBMISSION_ENABLED_KEY},
            )
            await conn.commit()

    await _clean()
    yield
    await _clean()


async def test_end_to_end_transient_failure_and_successful_retry(
    session,
    demo_profile,
    demo_job,
    demo_application,
    demo_user,
    inputs,
    tmp_path,
    store,
    monkeypatch,
) -> None:
    """Full lifecycle: Attempt 1 transient -> schedule -> claim attempt 1 -> attempt 2 SENT."""
    _base, _evaluation, facts = inputs
    app, sub1, event1, owner_user = await worker_tests._setup_submitting_application(
        session,
        demo_profile,
        demo_job,
        demo_application,
        demo_user,
        facts,
        store,
        tmp_path,
        monkeypatch,
    )

    # 1. Attempt 1 fails transiently
    transport1 = FakeEmailTransport(
        responses=[
            EmailSendResult(EmailSendStatus.TRANSIENT_FAILURE, smtp_code=421, detail="Service busy")
        ]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport",
        lambda *args, **kwargs: transport1,
    )

    settings = Settings(submission_max_attempts=3, submission_retry_delays_seconds=(60, 300))
    res1 = await execute_submission_attempt(session, event1, settings=settings, store=store)
    assert res1 == "completed"

    await session.refresh(app)
    assert app.status is ApplicationStatus.SUBMISSION_FAILED

    await session.refresh(sub1)
    assert sub1.result is SubmissionResult.FAILED
    assert sub1.attempt == 1
    assert sub1.next_attempt_after is not None
    assert sub1.next_attempt_after > utc_now()

    # 2. Make due in DB and run scheduler
    await session.execute(
        update(Submission)
        .where(Submission.id == sub1.id)
        .values(next_attempt_after=utc_now() - timedelta(seconds=1))
    )
    await session.commit()

    scheduled_count = await schedule_due_retries(session)
    assert scheduled_count == 1

    # 3. Deduplication: running schedule again emits 0
    duplicate_count = await schedule_due_retries(session)
    assert duplicate_count == 0

    # Retrieve the emitted retry_requested event
    retry_event = (
        await session.execute(
            select(Event).where(
                Event.type == "submission.retry_requested",
                Event.correlation_id == app.id,
            )
        )
    ).scalar_one()

    # 4. Worker executes retry
    transport2 = FakeEmailTransport(
        responses=[EmailSendResult(EmailSendStatus.SENT, message_id="smtp-msg-retry-success")]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport",
        lambda *args, **kwargs: transport2,
    )

    res2 = await execute_submission_retry(session, retry_event, settings=settings, store=store)
    assert res2 == "completed"

    # Invariant: sub1.next_attempt_after was cleared
    await session.refresh(sub1)
    assert sub1.next_attempt_after is None

    # Application transitioned to SUBMITTED
    await session.refresh(app)
    assert app.status is ApplicationStatus.SUBMITTED

    # New attempt 2 row exists and is SUCCEEDED
    sub2 = (
        await session.execute(
            select(Submission).where(
                Submission.application_id == app.id,
                Submission.attempt == 2,
            )
        )
    ).scalar_one()
    assert sub2.result is SubmissionResult.SUCCEEDED
    assert sub2.external_reference == "smtp-msg-retry-success"
    assert sub2.next_attempt_after is None

    # Total submissions in DB: exactly 2 rows
    all_subs = (
        (
            await session.execute(
                select(Submission)
                .where(Submission.application_id == app.id)
                .order_by(Submission.attempt.asc())
            )
        )
        .scalars()
        .all()
    )
    assert len(all_subs) == 2
    assert all_subs[0].attempt == 1 and all_subs[0].result is SubmissionResult.FAILED
    assert all_subs[1].attempt == 2 and all_subs[1].result is SubmissionResult.SUCCEEDED


async def test_retry_budget_exhaustion(
    session,
    demo_profile,
    demo_job,
    demo_application,
    demo_user,
    inputs,
    tmp_path,
    store,
    monkeypatch,
) -> None:
    """Attempt 1 fails -> Attempt 2 fails -> Attempt 3 fails -> Budget exhausted, no attempt 4."""
    _base, _evaluation, facts = inputs
    app, sub1, event1, owner_user = await worker_tests._setup_submitting_application(
        session,
        demo_profile,
        demo_job,
        demo_application,
        demo_user,
        facts,
        store,
        tmp_path,
        monkeypatch,
    )

    settings = Settings(submission_max_attempts=3, submission_retry_delays_seconds=(60, 300))

    # --- Attempt 1 fails ---
    t1 = FakeEmailTransport(
        responses=[
            EmailSendResult(EmailSendStatus.TRANSIENT_FAILURE, smtp_code=421, detail="Fail 1")
        ]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport", lambda *a, **k: t1
    )
    await execute_submission_attempt(session, event1, settings=settings, store=store)

    await session.refresh(sub1)
    assert sub1.next_attempt_after is not None

    # Make due and schedule retry 1
    await session.execute(
        update(Submission)
        .where(Submission.id == sub1.id)
        .values(next_attempt_after=utc_now() - timedelta(seconds=1))
    )
    await session.commit()
    await schedule_due_retries(session)

    retry_event_1 = (
        (
            await session.execute(
                select(Event)
                .where(Event.type == "submission.retry_requested", Event.correlation_id == app.id)
                .order_by(Event.occurred_at.desc())
            )
        )
        .scalars()
        .first()
    )

    # --- Attempt 2 fails transiently ---
    t2 = FakeEmailTransport(
        responses=[
            EmailSendResult(EmailSendStatus.TRANSIENT_FAILURE, smtp_code=421, detail="Fail 2")
        ]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport", lambda *a, **k: t2
    )
    await execute_submission_retry(session, retry_event_1, settings=settings, store=store)

    sub2 = (
        await session.execute(
            select(Submission).where(Submission.application_id == app.id, Submission.attempt == 2)
        )
    ).scalar_one()
    assert sub2.result is SubmissionResult.FAILED
    assert sub2.next_attempt_after is not None  # Attempt 2 can schedule attempt 3

    # Make due and schedule retry 2
    await session.execute(
        update(Submission)
        .where(Submission.id == sub2.id)
        .values(next_attempt_after=utc_now() - timedelta(seconds=1))
    )
    await session.commit()
    await schedule_due_retries(session)

    retry_event_2 = (
        (
            await session.execute(
                select(Event)
                .where(Event.type == "submission.retry_requested", Event.correlation_id == app.id)
                .order_by(Event.occurred_at.desc())
            )
        )
        .scalars()
        .first()
    )

    # --- Attempt 3 fails transiently -> budget exhausted ---
    t3 = FakeEmailTransport(
        responses=[
            EmailSendResult(EmailSendStatus.TRANSIENT_FAILURE, smtp_code=421, detail="Fail 3")
        ]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport", lambda *a, **k: t3
    )
    await execute_submission_retry(session, retry_event_2, settings=settings, store=store)

    sub3 = (
        await session.execute(
            select(Submission).where(Submission.application_id == app.id, Submission.attempt == 3)
        )
    ).scalar_one()
    assert sub3.result is SubmissionResult.FAILED
    # Crucial invariant: attempt 3 does NOT schedule retry!
    assert sub3.next_attempt_after is None

    await session.refresh(app)
    assert app.status is ApplicationStatus.SUBMISSION_FAILED

    # Scheduler finds 0 due retries
    assert await schedule_due_retries(session) == 0


async def test_retry_ambiguous_stops_permanently(
    session,
    demo_profile,
    demo_job,
    demo_application,
    demo_user,
    inputs,
    tmp_path,
    store,
    monkeypatch,
) -> None:
    """Ambiguous on attempt 2 permanently stops automatic retry."""
    _base, _evaluation, facts = inputs
    app, sub1, event1, owner_user = await worker_tests._setup_submitting_application(
        session,
        demo_profile,
        demo_job,
        demo_application,
        demo_user,
        facts,
        store,
        tmp_path,
        monkeypatch,
    )

    settings = Settings(submission_max_attempts=3, submission_retry_delays_seconds=(60, 300))

    # Attempt 1 fails transiently
    t1 = FakeEmailTransport(
        responses=[
            EmailSendResult(EmailSendStatus.TRANSIENT_FAILURE, smtp_code=421, detail="Fail 1")
        ]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport", lambda *a, **k: t1
    )
    await execute_submission_attempt(session, event1, settings=settings, store=store)

    await session.execute(
        update(Submission)
        .where(Submission.id == sub1.id)
        .values(next_attempt_after=utc_now() - timedelta(seconds=1))
    )
    await session.commit()
    await schedule_due_retries(session)

    retry_event = (
        await session.execute(
            select(Event).where(
                Event.type == "submission.retry_requested", Event.correlation_id == app.id
            )
        )
    ).scalar_one()

    # Attempt 2 is ambiguous
    t2 = FakeEmailTransport(
        responses=[
            EmailSendResult(EmailSendStatus.AMBIGUOUS, detail="Connection reset by peer after DATA")
        ]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport", lambda *a, **k: t2
    )
    await execute_submission_retry(session, retry_event, settings=settings, store=store)

    sub2 = (
        await session.execute(
            select(Submission).where(Submission.application_id == app.id, Submission.attempt == 2)
        )
    ).scalar_one()
    assert sub2.result is SubmissionResult.FAILED
    assert sub2.failure_reason.startswith("ambiguous:")
    assert sub2.next_attempt_after is None

    await session.refresh(app)
    assert app.status is ApplicationStatus.SUBMISSION_FAILED

    # Scheduler finds nothing
    assert await schedule_due_retries(session) == 0


async def test_retry_preflight_switch_disabled_prevents_send(
    session,
    demo_profile,
    demo_job,
    demo_application,
    demo_user,
    inputs,
    tmp_path,
    store,
    monkeypatch,
) -> None:
    """Disabling submission switch between attempts halts retry preflight with zero sends."""
    _base, _evaluation, facts = inputs
    app, sub1, event1, owner_user = await worker_tests._setup_submitting_application(
        session,
        demo_profile,
        demo_job,
        demo_application,
        demo_user,
        facts,
        store,
        tmp_path,
        monkeypatch,
    )

    settings = Settings(submission_max_attempts=3, submission_retry_delays_seconds=(60, 300))

    t1 = FakeEmailTransport(
        responses=[
            EmailSendResult(EmailSendStatus.TRANSIENT_FAILURE, smtp_code=421, detail="Fail 1")
        ]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport", lambda *a, **k: t1
    )
    await execute_submission_attempt(session, event1, settings=settings, store=store)

    await session.execute(
        update(Submission)
        .where(Submission.id == sub1.id)
        .values(next_attempt_after=utc_now() - timedelta(seconds=1))
    )
    await session.commit()
    await schedule_due_retries(session)

    retry_event = (
        await session.execute(
            select(Event).where(
                Event.type == "submission.retry_requested", Event.correlation_id == app.id
            )
        )
    ).scalar_one()

    # Disable switch before retry attempt executes!
    await set_submission_dispatch_enabled(session, False, actor=owner_user)

    t2 = FakeEmailTransport()
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport", lambda *a, **k: t2
    )

    await execute_submission_retry(session, retry_event, settings=settings, store=store)

    # Transport should have had ZERO sends
    assert len(t2.sent) == 0

    sub2 = (
        await session.execute(
            select(Submission).where(Submission.application_id == app.id, Submission.attempt == 2)
        )
    ).scalar_one()
    assert sub2.result is SubmissionResult.FAILED
    assert "disabled at execution" in sub2.failure_reason
    assert sub2.started_at is None
    assert sub2.next_attempt_after is None

    await session.refresh(app)
    assert app.status is ApplicationStatus.SUBMISSION_FAILED


async def test_scheduler_runs_under_monitor_role(engine, session) -> None:
    """Periodic retry scheduler executes cleanly under the app_monitor connection role."""
    monitor_engine = create_async_engine(_url("app_monitor"))
    try:
        await verify_role(monitor_engine, "app_monitor")
        async with async_sessionmaker(monitor_engine)() as monitor_session:
            # schedule_due_retries queries submissions and inserts into events
            # This must execute without postgres permission violations
            count = await schedule_due_retries(monitor_session)
            assert count >= 0
    finally:
        await monitor_engine.dispose()


async def test_retry_claim_binds_to_current_policy_and_packet_in_db(
    session,
    demo_profile,
    demo_job,
    demo_application,
    demo_user,
    inputs,
    tmp_path,
    store,
    monkeypatch,
) -> None:
    """Attempt N+1 binds to the current policy and packet artifact in the database."""
    _base, _evaluation, facts = inputs
    app, sub1, event1, owner_user = await worker_tests._setup_submitting_application(
        session,
        demo_profile,
        demo_job,
        demo_application,
        demo_user,
        facts,
        store,
        tmp_path,
        monkeypatch,
    )

    settings = Settings(submission_max_attempts=3, submission_retry_delays_seconds=(60, 300))

    # Attempt 1 fails transiently
    t1 = FakeEmailTransport(
        responses=[
            EmailSendResult(EmailSendStatus.TRANSIENT_FAILURE, smtp_code=421, detail="Fail 1")
        ]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport", lambda *a, **k: t1
    )
    await execute_submission_attempt(session, event1, settings=settings, store=store)

    orig_policy_id = sub1.policy_id

    # Make due and schedule retry
    await session.execute(
        update(Submission)
        .where(Submission.id == sub1.id)
        .values(next_attempt_after=utc_now() - timedelta(seconds=1))
    )
    await session.commit()
    await schedule_due_retries(session)

    retry_event = (
        await session.execute(
            select(Event).where(
                Event.type == "submission.retry_requested", Event.correlation_id == app.id
            )
        )
    ).scalar_one()

    # Attempt 2 succeeds
    t2 = FakeEmailTransport()
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport", lambda *a, **k: t2
    )
    res = await execute_submission_retry(session, retry_event, settings=settings, store=store)
    assert res == "completed"

    sub2 = (
        await session.execute(
            select(Submission).where(Submission.application_id == app.id, Submission.attempt == 2)
        )
    ).scalar_one()

    # Invariant: Attempt 2 bound to current policy and has packet artifact
    assert sub2.policy_id == orig_policy_id
    assert sub2.packet_artifact_id is not None
    assert sub2.result is SubmissionResult.SUCCEEDED

    # Invariant: Attempt 1 provenance is preserved
    await session.refresh(sub1)
    assert sub1.policy_id == orig_policy_id
    assert sub1.attempt == 1
    assert sub1.result is SubmissionResult.FAILED
    assert sub1.next_attempt_after is None


async def test_retry_claim_fails_closed_if_policy_invalid_before_claim(
    session,
    demo_profile,
    demo_job,
    demo_application,
    demo_user,
    inputs,
    tmp_path,
    store,
    monkeypatch,
) -> None:
    """If policy becomes invalid (blocked) before claim, no attempt 2 is created and zero sends."""
    _base, _evaluation, facts = inputs
    app, sub1, event1, owner_user = await worker_tests._setup_submitting_application(
        session,
        demo_profile,
        demo_job,
        demo_application,
        demo_user,
        facts,
        store,
        tmp_path,
        monkeypatch,
    )

    settings = Settings(submission_max_attempts=3, submission_retry_delays_seconds=(60, 300))

    t1 = FakeEmailTransport(
        responses=[
            EmailSendResult(EmailSendStatus.TRANSIENT_FAILURE, smtp_code=421, detail="Fail 1")
        ]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport", lambda *a, **k: t1
    )
    await execute_submission_attempt(session, event1, settings=settings, store=store)

    # Make due and schedule retry
    await session.execute(
        update(Submission)
        .where(Submission.id == sub1.id)
        .values(next_attempt_after=utc_now() - timedelta(seconds=1))
    )
    await session.commit()
    await schedule_due_retries(session)

    retry_event = (
        await session.execute(
            select(Event).where(
                Event.type == "submission.retry_requested", Event.correlation_id == app.id
            )
        )
    ).scalar_one()

    # Policy becomes non-EMAIL before claim transaction!
    await session.execute(
        update(SitePolicy)
        .where(SitePolicy.domain == DOMAIN)
        .values(submission_method=SubmissionMethod.HUMAN_HANDOFF)
    )
    await session.commit()

    t2 = FakeEmailTransport()
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport", lambda *a, **k: t2
    )

    res = await execute_submission_retry(session, retry_event, settings=settings, store=store)
    assert res == "completed"

    # Zero sends!
    assert len(t2.sent) == 0

    # No attempt 2 was created!
    attempt2 = (
        await session.execute(
            select(Submission).where(Submission.application_id == app.id, Submission.attempt == 2)
        )
    ).scalar_one_or_none()
    assert attempt2 is None

    # sub1.next_attempt_after was cleared
    await session.refresh(sub1)
    assert sub1.next_attempt_after is None
