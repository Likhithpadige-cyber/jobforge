"""Application transitions against a real database.

The unit tests prove the graph; these prove the three writes actually land
together in one transaction, and that a rejected move leaves the database
exactly as it found it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from packages.core.models import (
    Application,
    ApplicationStatus,
    ApplicationStatusHistory,
    Event,
    Resume,
    ResumeClaim,
)
from packages.core.state_machines.application import transition_application
from packages.core.state_machines.job import IllegalTransitionError

pytestmark = pytest.mark.integration


async def _load(session, application_id) -> Application:
    return (
        await session.execute(
            select(Application).where(Application.id == application_id)
        )
    ).scalar_one()


async def _counts(session, application_id) -> tuple[int, int]:
    history = (
        await session.execute(
            select(func.count(ApplicationStatusHistory.id)).where(
                ApplicationStatusHistory.application_id == application_id
            )
        )
    ).scalar_one()
    changed = (
        await session.execute(
            select(func.count(Event.id)).where(
                Event.aggregate_id == application_id,
                Event.type == "application.status.changed",
            )
        )
    ).scalar_one()
    return history, changed


async def test_created_to_resume_generating_writes_all_three(
    session, demo_application
) -> None:
    application_id = demo_application["id"]
    app = await _load(session, application_id)
    assert app.status is ApplicationStatus.CREATED

    previous = await transition_application(
        session,
        app,
        ApplicationStatus.RESUME_GENERATING,
        actor_type="agent",
        actor_id="resume_optimizer",
        reason="initial generation",
    )
    await session.flush()

    assert previous is ApplicationStatus.CREATED
    assert (await _load(session, application_id)).status is (
        ApplicationStatus.RESUME_GENERATING
    )
    assert await _counts(session, application_id) == (1, 1)

    row = (
        await session.execute(
            select(ApplicationStatusHistory).where(
                ApplicationStatusHistory.application_id == application_id
            )
        )
    ).scalar_one()
    assert row.from_status is ApplicationStatus.CREATED
    assert row.to_status is ApplicationStatus.RESUME_GENERATING
    assert row.actor_type == "agent"
    assert row.actor_id == "resume_optimizer"
    assert row.reason == "initial generation"
    assert row.created_at is not None


@pytest.mark.parametrize(
    "target",
    [ApplicationStatus.RESUME_READY, ApplicationStatus.VERIFICATION_FAILED],
)
async def test_resume_generating_to_both_outcomes(
    session, demo_application, target
) -> None:
    application_id = demo_application["id"]
    app = await _load(session, application_id)
    await transition_application(session, app, ApplicationStatus.RESUME_GENERATING)
    await transition_application(session, app, target, actor_id="resume_optimizer")
    await session.flush()

    assert (await _load(session, application_id)).status is target
    assert await _counts(session, application_id) == (2, 2)


async def test_rejected_transition_leaves_database_untouched(
    session, demo_application
) -> None:
    application_id = demo_application["id"]
    app = await _load(session, application_id)

    with pytest.raises(IllegalTransitionError):
        await transition_application(session, app, ApplicationStatus.APPROVED)
    await session.flush()

    assert (await _load(session, application_id)).status is ApplicationStatus.CREATED
    assert await _counts(session, application_id) == (0, 0)


async def test_rollback_removes_status_history_and_event_together(
    session, demo_application
) -> None:
    """The three writes share the caller's transaction, so they undo together.

    The savepoint is rolled back through its own handle, not through
    `session.rollback()`. The `session` fixture holds one outer transaction
    that the baseline application row lives in, and rolling the session back
    would discard that row too - taking the test's own subject with it and
    proving nothing about the transition.
    """
    application_id = demo_application["id"]
    app = await _load(session, application_id)

    savepoint = await session.begin_nested()
    await transition_application(session, app, ApplicationStatus.RESUME_GENERATING)
    await session.flush()
    assert await _counts(session, application_id) == (1, 1)
    await savepoint.rollback()

    # Read the real post-rollback state rather than the identity map's copy,
    # so the status assertion comes from the database.
    session.expire_all()
    assert (await _load(session, application_id)).status is ApplicationStatus.CREATED
    assert await _counts(session, application_id) == (0, 0)


async def test_transition_writes_no_resume_or_claim_rows(
    session, demo_application
) -> None:
    application_id = demo_application["id"]
    resumes = (await session.execute(select(func.count(Resume.id)))).scalar_one()
    claims = (await session.execute(select(func.count(ResumeClaim.id)))).scalar_one()

    app = await _load(session, application_id)
    await transition_application(session, app, ApplicationStatus.RESUME_GENERATING)
    await session.flush()

    assert (
        await session.execute(select(func.count(Resume.id)))
    ).scalar_one() == resumes
    assert (
        await session.execute(select(func.count(ResumeClaim.id)))
    ).scalar_one() == claims


async def test_approval_metadata_untouched(session, demo_application) -> None:
    application_id = demo_application["id"]
    app = await _load(session, application_id)
    await transition_application(session, app, ApplicationStatus.RESUME_GENERATING)
    await session.flush()

    refreshed = await _load(session, application_id)
    assert refreshed.approved_at is None
    assert refreshed.approved_by is None
