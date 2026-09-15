"""Application lifecycle transitions - the deliberately partial graph.

The point of these tests is the *closed* half: only seven edges exist today,
so every one of the remaining pairs must be rejected. They are enumerated
exhaustively rather than sampled, because an accidentally-permitted edge is
exactly the failure this module exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from packages.core.models import ApplicationStatus, ApplicationStatusHistory
from packages.core.state_machines.application import (
    ALLOWED_TRANSITIONS,
    can_transition,
    transition_application,
)
from packages.core.state_machines.job import IllegalTransitionError

pytestmark = [pytest.mark.unit, pytest.mark.safety]

#: The only edges the reviews settled. Everything else fails closed.
IMPLEMENTED: frozenset[tuple[ApplicationStatus, ApplicationStatus]] = frozenset(
    {
        # Generation (B6).
        (ApplicationStatus.CREATED, ApplicationStatus.RESUME_GENERATING),
        (ApplicationStatus.RESUME_GENERATING, ApplicationStatus.RESUME_READY),
        (ApplicationStatus.RESUME_GENERATING, ApplicationStatus.VERIFICATION_FAILED),
        # Human review (C2). The first is a temporary bridge around
        # questions_pending while question extraction is unimplemented.
        (ApplicationStatus.RESUME_READY, ApplicationStatus.AWAITING_APPROVAL),
        (ApplicationStatus.AWAITING_APPROVAL, ApplicationStatus.APPROVED),
        (ApplicationStatus.AWAITING_APPROVAL, ApplicationStatus.CHANGES_REQUESTED),
        # Rework (C3). The only exit from changes_requested, and it returns to
        # the start of generation: a revision earns resume_ready by passing
        # verification, never by inheriting the previous attempt's.
        (ApplicationStatus.CHANGES_REQUESTED, ApplicationStatus.RESUME_GENERATING),
    }
)


@dataclass
class FakeApplication:
    id: UUID
    status: ApplicationStatus
    approved_at: object = None
    approved_by: object = None
    outcome: str = "pending"
    extras: dict[str, object] = field(default_factory=dict)


def application(status: ApplicationStatus) -> FakeApplication:
    return FakeApplication(id=uuid4(), status=status)


@pytest.fixture
def session():
    value = Mock()
    value.flush = AsyncMock()
    return value


def added(session, kind):
    return [
        c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], kind)
    ]


def events(session):
    from packages.core.models import Event

    return added(session, Event)


# --- the graph itself -------------------------------------------------------


def test_every_status_has_an_entry() -> None:
    """A status missing from the map is unreachable and un-exitable."""
    assert set(ALLOWED_TRANSITIONS) == set(ApplicationStatus)


def test_only_the_reviewed_edges_exist() -> None:
    edges = {
        (current, target)
        for current, targets in ALLOWED_TRANSITIONS.items()
        for target in targets
    }
    assert edges == set(IMPLEMENTED)


@pytest.mark.parametrize(("current", "target"), sorted(IMPLEMENTED))
def test_reviewed_edges_are_allowed(current, target) -> None:
    assert can_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    sorted(
        (a, b)
        for a in ApplicationStatus
        for b in ApplicationStatus
        if (a, b) not in IMPLEMENTED
    ),
)
def test_every_other_pair_is_rejected(current, target) -> None:
    """Includes self transitions and every skipped/undocumented move."""
    assert not can_transition(current, target)


@pytest.mark.parametrize("status", list(ApplicationStatus))
def test_self_transitions_rejected(status) -> None:
    assert not can_transition(status, status)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ApplicationStatus.CREATED, ApplicationStatus.RESUME_READY),
        (ApplicationStatus.CREATED, ApplicationStatus.VERIFICATION_FAILED),
        (ApplicationStatus.CREATED, ApplicationStatus.APPROVED),
        (ApplicationStatus.RESUME_GENERATING, ApplicationStatus.SUBMITTED),
    ],
)
def test_skipped_states_rejected(current, target) -> None:
    assert not can_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ApplicationStatus.RESUME_READY, ApplicationStatus.QUESTIONS_PENDING),
        (ApplicationStatus.APPROVED, ApplicationStatus.SUBMISSION_READY),
        (ApplicationStatus.SUBMISSION_FAILED, ApplicationStatus.SUBMITTING),
        (ApplicationStatus.VERIFICATION_FAILED, ApplicationStatus.RESUME_GENERATING),
    ],
)
def test_unimplemented_lifecycle_paths_rejected(current, target) -> None:
    """Unimplemented question, submission and failure-retry paths stay closed."""
    assert not can_transition(current, target)


def test_review_is_only_reachable_through_awaiting_approval() -> None:
    """No shortcut from resume_ready straight to a decision.

    Entering review must stay a distinct, recorded step, so a decision cannot
    be made on a resume nobody opened for review.
    """
    assert not can_transition(
        ApplicationStatus.RESUME_READY, ApplicationStatus.APPROVED
    )
    assert not can_transition(
        ApplicationStatus.RESUME_READY, ApplicationStatus.CHANGES_REQUESTED
    )


def test_questions_pending_remains_the_documented_long_term_entry() -> None:
    """The bridge does not delete the intended path.

    `questions_pending` keeps its place in the graph with no exits, so when
    question extraction lands the route can be restored rather than rediscovered.
    """
    assert ApplicationStatus.QUESTIONS_PENDING in ALLOWED_TRANSITIONS
    assert ALLOWED_TRANSITIONS[ApplicationStatus.QUESTIONS_PENDING] == frozenset()


def test_rework_must_restart_generation() -> None:
    """C3 permits rework only through generation and fresh verification."""
    assert ALLOWED_TRANSITIONS[ApplicationStatus.CHANGES_REQUESTED] == frozenset(
        {ApplicationStatus.RESUME_GENERATING}
    )


def test_decisions_are_terminal_in_the_current_graph() -> None:
    assert ALLOWED_TRANSITIONS[ApplicationStatus.APPROVED] == frozenset()


# --- the write path ---------------------------------------------------------


@pytest.mark.parametrize(("current", "target"), sorted(IMPLEMENTED))
async def test_successful_transition_writes_status_history_and_event(
    session, current, target
) -> None:
    app = application(current)
    previous = await transition_application(
        session, app, target, actor_type="agent", actor_id="resume_optimizer"
    )
    assert previous is current
    assert app.status is target
    assert len(added(session, ApplicationStatusHistory)) == 1
    assert len(events(session)) == 1


async def test_successful_transition_emits_exactly_one_status_changed_event(
    session,
) -> None:
    app = application(ApplicationStatus.CREATED)
    await transition_application(
        session, app, ApplicationStatus.RESUME_GENERATING, actor_id="resume_optimizer"
    )
    emitted = events(session)
    assert len(emitted) == 1
    assert emitted[0].type == "application.status.changed"
    assert emitted[0].payload["from_status"] == "created"
    assert emitted[0].payload["to_status"] == "resume_generating"


async def test_actor_and_reason_preserved_in_history_and_event(session) -> None:
    app = application(ApplicationStatus.RESUME_GENERATING)
    await transition_application(
        session,
        app,
        ApplicationStatus.VERIFICATION_FAILED,
        actor_type="human",
        actor_id="reviewer-7",
        reason="claim overreach on bullet 2",
    )
    row = added(session, ApplicationStatusHistory)[0]
    assert row.from_status is ApplicationStatus.RESUME_GENERATING
    assert row.to_status is ApplicationStatus.VERIFICATION_FAILED
    assert row.actor_type == "human"
    assert row.actor_id == "reviewer-7"
    assert row.reason == "claim overreach on bullet 2"
    payload = events(session)[0].payload
    assert payload["actor_type"] == "human"
    assert payload["actor_id"] == "reviewer-7"
    assert payload["reason"] == "claim overreach on bullet 2"


async def test_reason_omitted_from_payload_when_absent(session) -> None:
    app = application(ApplicationStatus.CREATED)
    await transition_application(session, app, ApplicationStatus.RESUME_GENERATING)
    assert "reason" not in events(session)[0].payload


async def test_event_id_is_not_invented(session) -> None:
    """Docs define no meaning for the nullable column, so it stays null."""
    app = application(ApplicationStatus.CREATED)
    await transition_application(session, app, ApplicationStatus.RESUME_GENERATING)
    assert added(session, ApplicationStatusHistory)[0].event_id is None


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ApplicationStatus.CREATED, ApplicationStatus.RESUME_READY),
        (ApplicationStatus.CREATED, ApplicationStatus.CREATED),
        (ApplicationStatus.RESUME_READY, ApplicationStatus.SUBMITTED),
        (ApplicationStatus.APPROVED, ApplicationStatus.SUBMITTING),
    ],
)
async def test_failed_transition_mutates_nothing(session, current, target) -> None:
    app = application(current)
    with pytest.raises(IllegalTransitionError):
        await transition_application(session, app, target)
    assert app.status is current  # no status mutation
    assert added(session, ApplicationStatusHistory) == []  # no history row
    assert events(session) == []  # no event
    session.flush.assert_not_awaited()


async def test_stale_repeat_of_a_completed_transition_is_refused(session) -> None:
    """A redelivered move finds the application already advanced.

    `resume_generating -> resume_generating` is not an edge, so the second
    attempt raises instead of writing a duplicate history row and a duplicate
    event. Full event-level idempotency stays where it already lives
    (`claim_event`); this only proves the machine adds no duplicates of its own.
    """
    app = application(ApplicationStatus.CREATED)
    await transition_application(session, app, ApplicationStatus.RESUME_GENERATING)
    with pytest.raises(IllegalTransitionError):
        await transition_application(session, app, ApplicationStatus.RESUME_GENERATING)
    assert len(added(session, ApplicationStatusHistory)) == 1
    assert len(events(session)) == 1


async def test_never_creates_resume_or_claim_rows(session) -> None:
    from packages.core.models import Resume, ResumeClaim

    app = application(ApplicationStatus.RESUME_GENERATING)
    await transition_application(session, app, ApplicationStatus.RESUME_READY)
    assert added(session, Resume | ResumeClaim) == []


async def test_does_not_touch_approval_metadata(session) -> None:
    """The machine moves status only; approval fields are not its business."""
    app = application(ApplicationStatus.RESUME_GENERATING)
    await transition_application(session, app, ApplicationStatus.RESUME_READY)
    assert app.approved_at is None and app.approved_by is None


async def test_does_not_commit(session) -> None:
    app = application(ApplicationStatus.CREATED)
    await transition_application(session, app, ApplicationStatus.RESUME_GENERATING)
    session.commit.assert_not_called()
