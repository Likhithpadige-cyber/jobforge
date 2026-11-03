"""M11 Phase 2: Submission worker and execution attempt integration tests.

Exercises the real execution boundary against PostgreSQL:
- Successful email dispatch (SENT) -> application SUBMITTED, submission SUCCEEDED
- Outbound email composition (recipient, subject, cover letter body, resume PDF attachment)
- Idempotency & replay safety (second attempt does not re-send)
- Rejection handling (REJECTED) -> application SUBMISSION_FAILED, submission FAILED
- Ambiguous outcome handling (AMBIGUOUS) -> application SUBMISSION_FAILED,
  failure_reason prefixed "ambiguous:"
- Transient failure handling (TRANSIENT_FAILURE) -> application SUBMISSION_FAILED,
  no auto-retry in Phase 2
- Switch disabled at execution time -> application SUBMISSION_FAILED, zero sends
- Policy expired at execution time -> application SUBMISSION_FAILED, zero sends
"""

from __future__ import annotations

import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.models import (
    Application,
    ApplicationStatus,
    Event,
    Job,
    SitePolicy,
    Submission,
    SubmissionResult,
    User,
)
from packages.core.services.settings import (
    SUBMISSION_ENABLED_KEY,
    set_submission_dispatch_enabled,
)
from packages.core.services.submission_execution import (
    execute_submission_attempt,
    request_submission,
)
from packages.services.delivery.email import (
    EmailSendResult,
    EmailSendStatus,
    FakeEmailTransport,
)
from packages.services.rendering.store import FilesystemArtifactStore
from packages.shared.config import Settings
from tests.integration import test_submission_readiness as sr_tests
from tests.integration import test_tailoring_context

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
            await conn.execute(
                text("DELETE FROM submissions"),
            )
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


async def _setup_submitting_application(
    session,
    demo_profile,
    demo_job,
    demo_application,
    demo_user,
    facts,
    store,
    tmp_path,
    monkeypatch,
    destination_email: str = "hiring@example.invalid",
) -> tuple[Application, Submission, Event, User]:
    monkeypatch.setenv("SUBMISSION_MODE", "enabled")

    owner_user = await session.get(User, demo_user)
    assert owner_user is not None
    await set_submission_dispatch_enabled(session, True, actor=owner_user)

    await session.execute(
        update(Job).where(Job.id == demo_job).values(url=f"mailto:{destination_email}")
    )

    app_id = demo_application["id"]
    await sr_tests._allow_via_email(session)
    await sr_tests._approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    await sr_tests._trigger_readiness(session, app_id, store=store)

    sub = await request_submission(
        session,
        app_id,
        Settings(),
        actor=owner_user,
    )
    assert sub.started_at is None

    event = (
        await session.execute(
            select(Event).where(
                Event.type == "submission.requested",
                Event.correlation_id == app_id,
            )
        )
    ).scalar_one()

    app = await session.get(Application, app_id)
    assert app is not None
    return app, sub, event, owner_user


# ---------------------------------------------------------------------------
# Worker execution tests
# ---------------------------------------------------------------------------


async def test_submission_worker_success_sent(
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
    _base, _evaluation, facts = inputs
    app, sub, event, _owner = await _setup_submitting_application(
        session,
        demo_profile,
        demo_job,
        demo_application,
        demo_user,
        facts,
        store,
        tmp_path,
        monkeypatch,
        destination_email="careers@example.invalid",
    )

    fake_transport = FakeEmailTransport(
        responses=[EmailSendResult(EmailSendStatus.SENT, message_id="msg-e2e-123")]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport",
        lambda *args, **kwargs: fake_transport,
    )

    outcome = await execute_submission_attempt(session, event, settings=Settings(), store=store)
    assert outcome == "completed"

    # Assert application state
    await session.refresh(app)
    assert app.status is ApplicationStatus.SUBMITTED
    assert app.submitted_at is not None

    # Assert submission record state
    await session.refresh(sub)
    assert sub.result is SubmissionResult.SUCCEEDED
    assert sub.external_reference == "msg-e2e-123"
    assert sub.started_at is not None
    assert sub.finished_at is not None
    assert sub.started_at <= sub.finished_at
    assert sub.next_attempt_after is None

    # Assert outbound email
    assert len(fake_transport.sent) == 1
    sent_msg = fake_transport.sent[0]
    assert sent_msg.to == "careers@example.invalid"
    assert "Software Engineer" in sent_msg.subject or "Application for" in sent_msg.subject
    assert sent_msg.body  # Non-empty cover letter text
    assert len(sent_msg.attachments) == 1
    assert sent_msg.attachments[0].filename == f"Resume_{app.id}.pdf"
    assert sent_msg.attachments[0].content.startswith(b"%PDF")

    # Assert emitted events
    events = (
        (
            await session.execute(
                select(Event)
                .where(Event.correlation_id == app.id)
                .order_by(Event.occurred_at.asc(), Event.id.asc())
            )
        )
        .scalars()
        .all()
    )
    event_by_type = {e.type: e for e in events}
    assert "submission.started" in event_by_type
    assert event_by_type["submission.started"].actor_type == "system"
    assert "submission.succeeded" in event_by_type
    assert event_by_type["submission.succeeded"].actor_type == "system"


async def test_submission_worker_idempotent_replay(
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
    _base, _evaluation, facts = inputs
    app, sub, event, _owner = await _setup_submitting_application(
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

    fake_transport = FakeEmailTransport(
        responses=[EmailSendResult(EmailSendStatus.SENT, message_id="msg-e2e-456")]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport",
        lambda *args, **kwargs: fake_transport,
    )

    # First attempt
    outcome1 = await execute_submission_attempt(session, event, settings=Settings(), store=store)
    assert outcome1 == "completed"
    assert len(fake_transport.sent) == 1

    # Second attempt (replay of same event)
    outcome2 = await execute_submission_attempt(session, event, settings=Settings(), store=store)
    assert outcome2 == "completed"
    # Proves no double send:
    assert len(fake_transport.sent) == 1


async def test_submission_worker_rejection(
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
    _base, _evaluation, facts = inputs
    app, sub, event, _owner = await _setup_submitting_application(
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

    fake_transport = FakeEmailTransport(
        responses=[
            EmailSendResult(
                EmailSendStatus.REJECTED,
                smtp_code=550,
                detail="5.1.1 User unknown",
            )
        ]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport",
        lambda *args, **kwargs: fake_transport,
    )

    outcome = await execute_submission_attempt(session, event, settings=Settings(), store=store)
    assert outcome == "completed"

    await session.refresh(app)
    assert app.status is ApplicationStatus.SUBMISSION_FAILED
    assert "rejected" in app.error_reason.lower() or "failed" in app.error_reason.lower()

    await session.refresh(sub)
    assert sub.result is SubmissionResult.FAILED
    assert sub.http_status == 550
    assert sub.failure_reason == "5.1.1 User unknown"
    assert sub.started_at is not None
    assert sub.finished_at is not None
    assert sub.started_at <= sub.finished_at
    assert sub.next_attempt_after is None

    # Assert emitted events
    events = (
        (
            await session.execute(
                select(Event).where(
                    Event.correlation_id == app.id, Event.type == "submission.failed"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].actor_type == "system"
    assert events[0].payload["will_retry"] is False
    assert events[0].payload["ambiguous"] is False


async def test_submission_worker_ambiguous(
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
    _base, _evaluation, facts = inputs
    app, sub, event, _owner = await _setup_submitting_application(
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

    fake_transport = FakeEmailTransport(
        responses=[
            EmailSendResult(
                EmailSendStatus.AMBIGUOUS,
                detail="read timeout while waiting for DATA response",
            )
        ]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport",
        lambda *args, **kwargs: fake_transport,
    )

    outcome = await execute_submission_attempt(session, event, settings=Settings(), store=store)
    assert outcome == "completed"

    await session.refresh(app)
    assert app.status is ApplicationStatus.SUBMISSION_FAILED

    await session.refresh(sub)
    assert sub.result is SubmissionResult.FAILED
    assert sub.failure_reason.startswith("ambiguous:")
    assert "timeout" in sub.failure_reason
    assert sub.started_at is not None
    assert sub.finished_at is not None
    assert sub.started_at <= sub.finished_at
    assert sub.next_attempt_after is None

    events = (
        (
            await session.execute(
                select(Event).where(
                    Event.correlation_id == app.id, Event.type == "submission.failed"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].actor_type == "system"
    assert events[0].payload["will_retry"] is False
    assert events[0].payload["ambiguous"] is True


async def test_submission_worker_transient_failure_no_auto_retry(
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
    _base, _evaluation, facts = inputs
    app, sub, event, _owner = await _setup_submitting_application(
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

    fake_transport = FakeEmailTransport(
        responses=[
            EmailSendResult(
                EmailSendStatus.TRANSIENT_FAILURE,
                smtp_code=421,
                detail="4.3.2 Service temporarily unavailable",
            )
        ]
    )
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport",
        lambda *args, **kwargs: fake_transport,
    )

    outcome = await execute_submission_attempt(session, event, settings=Settings(), store=store)
    assert outcome == "completed"

    await session.refresh(app)
    assert app.status is ApplicationStatus.SUBMISSION_FAILED

    await session.refresh(sub)
    assert sub.result is SubmissionResult.FAILED
    assert sub.http_status == 421
    assert sub.started_at is not None
    assert sub.finished_at is not None
    assert sub.next_attempt_after is not None  # Phase 3: transient failure schedules retry
    assert sub.next_attempt_after > sub.finished_at


async def test_submission_worker_switches_disabled_at_execution(
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
    _base, _evaluation, facts = inputs
    app, sub, event, owner_user = await _setup_submitting_application(
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

    # Disable the persisted switch before worker executes
    await set_submission_dispatch_enabled(session, False, actor=owner_user)

    fake_transport = FakeEmailTransport()
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport",
        lambda *args, **kwargs: fake_transport,
    )

    outcome = await execute_submission_attempt(session, event, settings=Settings(), store=store)
    assert outcome == "completed"

    # Zero emails sent
    assert len(fake_transport.sent) == 0

    await session.refresh(app)
    assert app.status is ApplicationStatus.SUBMISSION_FAILED
    assert "disabled" in app.error_reason.lower()

    await session.refresh(sub)
    assert sub.result is SubmissionResult.FAILED
    assert sub.started_at is None
    assert "disabled" in sub.failure_reason.lower()

    events = (
        (
            await session.execute(
                select(Event).where(
                    Event.correlation_id == app.id, Event.type == "submission.failed"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].actor_type == "system"


async def test_submission_worker_policy_expired_at_execution(
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
    _base, _evaluation, facts = inputs
    app, sub, event, _owner = await _setup_submitting_application(
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

    # Expire policy before execution
    await session.execute(
        update(SitePolicy)
        .where(SitePolicy.domain == DOMAIN)
        .values(next_review_due=datetime.date(2020, 1, 1))
    )

    fake_transport = FakeEmailTransport()
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport",
        lambda *args, **kwargs: fake_transport,
    )

    outcome = await execute_submission_attempt(session, event, settings=Settings(), store=store)
    assert outcome == "completed"

    assert len(fake_transport.sent) == 0

    await session.refresh(app)
    assert app.status is ApplicationStatus.SUBMISSION_FAILED
    assert "expired" in app.error_reason.lower() or "disallowed" in app.error_reason.lower()

    await session.refresh(sub)
    assert sub.result is SubmissionResult.FAILED
    assert sub.started_at is None
    assert "expired" in sub.failure_reason.lower() or "disallowed" in sub.failure_reason.lower()

    events = (
        (
            await session.execute(
                select(Event).where(
                    Event.correlation_id == app.id, Event.type == "submission.failed"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].actor_type == "system"


async def test_submission_worker_destination_tampered_to_non_mailto_at_execution(
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
    """If job.url is altered to non-mailto before worker executes, fails closed."""
    _base, _evaluation, facts = inputs
    app, sub, event, _owner = await _setup_submitting_application(
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

    # Tamper job.url to non-mailto
    await session.execute(
        update(Job).where(Job.id == demo_job).values(url="https://example.invalid/job")
    )
    await session.commit()

    fake_transport = FakeEmailTransport()
    monkeypatch.setattr(
        "packages.core.services.submission_execution.build_email_transport",
        lambda *args, **kwargs: fake_transport,
    )

    outcome = await execute_submission_attempt(session, event, settings=Settings(), store=store)
    assert outcome == "completed"

    assert len(fake_transport.sent) == 0

    await session.refresh(app)
    assert app.status is ApplicationStatus.SUBMISSION_FAILED
    assert "mailto" in app.error_reason.lower()

    await session.refresh(sub)
    assert sub.result is SubmissionResult.FAILED
    assert sub.started_at is None
    assert "mailto" in sub.failure_reason.lower()

    failed_event = (
        await session.execute(
            select(Event).where(
                Event.correlation_id == app.id,
                Event.type == "submission.failed",
            )
        )
    ).scalar_one()
    assert failed_event.actor_type == "system"
    assert failed_event.payload["will_retry"] is False
