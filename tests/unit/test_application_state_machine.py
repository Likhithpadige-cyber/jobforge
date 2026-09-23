"""Application lifecycle transitions - the deliberately partial graph.

The point of these tests is the *closed* half: only nine edges exist today,
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
        # C3 recovery (PO-1). A durable attempt can fail after the application
        # has entered generation; this is how it is returned to a state a human
        # can act from, explicitly and in the history, rather than by the
        # absence of a commit.
        (ApplicationStatus.RESUME_GENERATING, ApplicationStatus.CHANGES_REQUESTED),
        # Question extraction (M9). Entered and left within one worker
        # execution - see packages/core/services/applications.py - so both
        # edges are always exercised together, never one without the other.
        (ApplicationStatus.RESUME_READY, ApplicationStatus.QUESTIONS_PENDING),
        (ApplicationStatus.QUESTIONS_PENDING, ApplicationStatus.AWAITING_APPROVAL),
        # Human review (C2).
        (ApplicationStatus.AWAITING_APPROVAL, ApplicationStatus.APPROVED),
        (ApplicationStatus.AWAITING_APPROVAL, ApplicationStatus.CHANGES_REQUESTED),
        # Rework (C3). The only exit from changes_requested, and it returns to
        # the start of generation: a revision earns resume_ready by passing
        # verification, never by inheriting the previous attempt's.
        (ApplicationStatus.CHANGES_REQUESTED, ApplicationStatus.RESUME_GENERATING),
        # Submission readiness (M10 Phase 2). One deterministic worker step
        # takes `approved -> submission_ready` unconditionally once RESUME,
        # COVER_LETTER, and (if any were prepared) ANSWERS are each approved
        # for their current version, then immediately the policy fan-out
        # decides whether a second edge follows - see
        # packages/core/services/submission_readiness.py.
        (ApplicationStatus.APPROVED, ApplicationStatus.SUBMISSION_READY),
        (ApplicationStatus.SUBMISSION_READY, ApplicationStatus.MANUAL_HANDOFF),
        (ApplicationStatus.SUBMISSION_READY, ApplicationStatus.POLICY_BLOCKED),
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
        (ApplicationStatus.SUBMISSION_READY, ApplicationStatus.SUBMITTING),
        (ApplicationStatus.SUBMITTING, ApplicationStatus.SUBMITTED),
        (ApplicationStatus.SUBMITTING, ApplicationStatus.SUBMISSION_FAILED),
        (ApplicationStatus.SUBMISSION_FAILED, ApplicationStatus.SUBMITTING),
        (ApplicationStatus.VERIFICATION_FAILED, ApplicationStatus.RESUME_GENERATING),
    ],
)
def test_unimplemented_lifecycle_paths_rejected(current, target) -> None:
    """Unimplemented submission and failure-retry paths stay closed - M11's,
    not guessed at here. `approved -> submission_ready` is M10 Phase 2's own
    edge now (see IMPLEMENTED); everything past `submission_ready` is not."""
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


def test_questions_pending_leads_only_to_awaiting_approval() -> None:
    """M9's sole exit from `questions_pending`.

    No path from `questions_pending` to a decision (`approved` /
    `changes_requested`) or back into generation: extraction results are
    reviewed, never used to skip review.
    """
    assert ALLOWED_TRANSITIONS[ApplicationStatus.QUESTIONS_PENDING] == frozenset(
        {ApplicationStatus.AWAITING_APPROVAL}
    )
    for target in (
        ApplicationStatus.APPROVED,
        ApplicationStatus.CHANGES_REQUESTED,
        ApplicationStatus.RESUME_GENERATING,
        ApplicationStatus.RESUME_READY,
    ):
        assert not can_transition(ApplicationStatus.QUESTIONS_PENDING, target)


def test_rework_must_restart_generation() -> None:
    """C3 permits rework only through generation and fresh verification."""
    assert ALLOWED_TRANSITIONS[ApplicationStatus.CHANGES_REQUESTED] == frozenset(
        {ApplicationStatus.RESUME_GENERATING}
    )


def test_recovery_returns_a_stranded_generation_to_changes_requested() -> None:
    """C3 PO-1, pinned on its own rather than only through IMPLEMENTED.

    A durable attempt can fail after the application has entered generation.
    Without this edge the recovery path is impossible by construction - T5
    raises `IllegalTransitionError`, the service swallows it so the original
    generation error survives, and the application is stranded in
    `resume_generating` with nothing working on it.
    """
    assert can_transition(
        ApplicationStatus.RESUME_GENERATING, ApplicationStatus.CHANGES_REQUESTED
    )
    assert ALLOWED_TRANSITIONS[ApplicationStatus.RESUME_GENERATING] == frozenset(
        {
            ApplicationStatus.RESUME_READY,
            ApplicationStatus.VERIFICATION_FAILED,
            ApplicationStatus.CHANGES_REQUESTED,
        }
    )


def test_recovery_does_not_open_a_route_around_verification() -> None:
    """The new edge recovers; it must not also become a shortcut.

    Recovery lands in `changes_requested`, whose only exit is still
    `resume_generating`. So a recovered application has to re-acquire its
    attempt and generate again - it cannot reach review or approval carrying
    the verdicts of an attempt that never finished.
    """
    assert ALLOWED_TRANSITIONS[ApplicationStatus.CHANGES_REQUESTED] == frozenset(
        {ApplicationStatus.RESUME_GENERATING}
    )
    for target in (
        ApplicationStatus.RESUME_READY,
        ApplicationStatus.AWAITING_APPROVAL,
        ApplicationStatus.APPROVED,
    ):
        assert not can_transition(ApplicationStatus.CHANGES_REQUESTED, target)


def test_approved_leads_only_to_submission_ready() -> None:
    """M10 Phase 2's sole exit from `approved` - readiness, not a decision
    reopening review. `submission_ready`/`manual_handoff`/`policy_blocked`
    stay terminal in this graph: `submitting` and everything past it is
    M11's."""
    assert ALLOWED_TRANSITIONS[ApplicationStatus.APPROVED] == frozenset(
        {ApplicationStatus.SUBMISSION_READY}
    )
    assert ALLOWED_TRANSITIONS[ApplicationStatus.SUBMISSION_READY] == frozenset(
        {ApplicationStatus.MANUAL_HANDOFF, ApplicationStatus.POLICY_BLOCKED}
    )
    assert ALLOWED_TRANSITIONS[ApplicationStatus.MANUAL_HANDOFF] == frozenset()
    assert ALLOWED_TRANSITIONS[ApplicationStatus.POLICY_BLOCKED] == frozenset()


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
