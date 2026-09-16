"""Authenticated HTTP C3 integration with real PostgreSQL and replayed model calls."""

import asyncio
import contextlib

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.deps import revision_client
from apps.api.main import create_app
from packages.agents.llm.runtime import StructuredClient
from packages.agents.resume_optimizer.revision import load_revision_request
from packages.core.auth import create_session
from packages.core.cache import get_redis
from packages.core.db import get_session
from packages.core.db.audit_boundary import AuditContext
from packages.core.models import (
    AgentRunStep,
    Application,
    ApplicationStatus,
    ApplicationStatusHistory,
    Approval,
    ApprovalStatus,
    Event,
    Resume,
    ResumeClaim,
    ResumeRevisionAttempt,
    RevisionAttemptStatus,
    RevisionFailureClass,
)
from packages.core.repositories.revision_attempts import RevisionAttemptRepository
from packages.core.services import resume_review, resume_revision, resume_rework
from packages.shared.config import Settings
from packages.shared.ids import uuid7
from tests.integration import test_resume_review as c2
from tests.integration import test_resume_revision as c3
from tests.unit.test_auth_sessions import FakeRedis

inputs = c3.inputs
pytestmark = [pytest.mark.integration, pytest.mark.safety]


@pytest.fixture
async def scenario(
    session, engine, demo_profile, demo_job, demo_application, inputs, tmp_path,
    audit_boundary_engine,
):
    _, _, facts = inputs
    app_id = demo_application["id"]
    await c2._ready(session, demo_profile, demo_job, demo_application, facts)
    actor = await c2._actor(session, demo_application)
    state = await c2._start(session, app_id, actor)
    actor_id, approval_id = actor.id, state.approval_id
    model = StructuredClient(Settings(), cassette_dir=tmp_path)
    # Stage exact requests from persisted decision data, then undo the decision.
    savepoint = await session.begin_nested()
    await resume_review.request_changes(
        session, application_id=app_id, actor=actor, note=c3.TIGHTEN
    )
    revision = await load_revision_request(session, app_id)
    await c3._stage_cassettes(
        session, tmp_path, model, revision, bullet_text="Built infrastructure with AWS."
    )
    await savepoint.rollback()
    await session.commit()

    redis = FakeRedis()
    login = await create_session(redis, user_id=actor_id, idle_minutes=60, absolute_hours=24)
    app = create_app()
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def database():
        async with maker() as db:
            try:
                yield db
            except Exception:
                await db.rollback()
                raise

    app.dependency_overrides[get_session] = database
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[revision_client] = lambda: model
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={Settings().session_cookie_name: login.id},
        headers={"X-CSRF-Token": login.csrf_token},
    ) as http:
        yield http, app_id, approval_id, maker, model, revision


async def post(scenario, **body):
    http, app_id, approval_id, *_ = scenario
    return await http.post(
        f"/api/v1/applications/{app_id}/resume-review/request-changes",
        json={"approval_id": str(approval_id), "note": c3.TIGHTEN, **body},
    )


async def test_http_regenerates_and_retries_without_another_version(scenario):
    first = await post(scenario)
    assert first.status_code == 200, first.text
    state = first.json()
    assert state["application_status"] == "resume_ready"
    assert state["resume_version"] == 2
    assert state["can_start_review"] and not state["can_approve"]
    second = await post(scenario)
    assert second.status_code == 200
    assert second.json()["resume_id"] == state["resume_id"]
    _, app_id, approval_id, maker, *_ = scenario
    async with maker() as db:
        assert len(await c3._resumes(db, app_id)) == 2
        approvals = (
            (await db.execute(select(Approval).where(Approval.application_id == app_id)))
            .scalars()
            .all()
        )
        assert len(approvals) == 1
        assert approvals[0].id == approval_id
        assert approvals[0].status == ApprovalStatus.CHANGES_REQUESTED
        assert len(await c2._events(db, app_id, "approval.changes_requested")) == 1
        assert len(await c2._events(db, app_id, "resume.generation.requested")) == 1
        assert not await c2._events(db, app_id, "approval.granted")
        attempts = await _attempts(db, app_id)
        assert len(attempts) == 1
        attempt = attempts[0]
        # The actual HTTP orchestration injected the successful T2 lease context;
        # the identical replay above created no second B1 stage.
        steps = (await db.scalars(select(AgentRunStep).where(
            AgentRunStep.guardrail_results["c3_execution"]["application_id"].astext == str(app_id)
        ))).all()
        assert len(steps) == 1 and steps[0].step_name == "B1"
        assert steps[0].guardrail_results["c3_execution"] == {
            "attempt_id": str(attempt.id), "lease_epoch": attempt.lease_epoch,
            "application_id": str(app_id), "approval_id": str(approval_id),
        }
    assert (await post(scenario, note="Different instructions")).status_code == 409
    http = scenario[0]
    opened = await http.post(f"/api/v1/applications/{app_id}/resume-review", json={})
    assert opened.status_code == 200
    assert opened.json()["approval_id"] != str(approval_id)
    assert (await post(scenario)).status_code == 409


async def _attempts(db, application_id):
    """Every attempt row for this application, oldest first.

    Deliberately unfiltered by status: a test that asks "did the retry reuse
    the attempt" has to be able to see a second row if one was wrongly created,
    and filtering to the live one would hide exactly the failure it checks for.
    """
    return list(
        (
            await db.execute(
                select(ResumeRevisionAttempt)
                .where(ResumeRevisionAttempt.application_id == application_id)
                .order_by(ResumeRevisionAttempt.id)
            )
        )
        .scalars()
        .all()
    )


#: The one transition that means "T5 ran". Stated once, because it is checked
#: twice - against `application_status_history` and against the
#: `application.status.changed` event - and those two must never drift. They
#: already did: the history check named both ends of the edge while the event
#: check named only the destination, and `changes_requested` is the
#: destination of TWO legitimate transitions in this flow (the T1 human
#: decision from `awaiting_approval`, and this recovery from
#: `resume_generating`). A destination-only predicate cannot tell them apart.
RECOVERY_EDGE = (ApplicationStatus.RESUME_GENERATING, ApplicationStatus.CHANGES_REQUESTED)

#: The human decision in T1. Same destination, different origin.
DECISION_EDGE = (ApplicationStatus.AWAITING_APPROVAL, ApplicationStatus.CHANGES_REQUESTED)


async def _history(db, application_id, edge):
    """Status-history rows for one exact edge.

    Both ends, always. This is the assertion that proves T5 *ran*, not merely
    that the status happens to be right: `_t5_recover` deliberately swallows
    exceptions so a recovery failure cannot replace the original generation
    error, which means a broken T5 is silent - and for most of this batch it
    was. The state machine had no such edge, `transition_application` raised
    before mutating, and the only evidence was a log line nothing asserted on.
    A history row cannot be produced by an early return.
    """
    source, target = edge
    return list(
        (
            await db.execute(
                select(ApplicationStatusHistory)
                .where(
                    ApplicationStatusHistory.application_id == application_id,
                    ApplicationStatusHistory.from_status == source,
                    ApplicationStatusHistory.to_status == target,
                )
                .order_by(ApplicationStatusHistory.id)
            )
        )
        .scalars()
        .all()
    )


async def _status_events(db, application_id, edge):
    """`application.status.changed` events for the same exact edge.

    Filtered on the identical pair as `_history`, from the same constant, so
    the two cannot disagree about what they are counting. The payload keys are
    written by `transition_application` (`from_status` / `to_status`, both
    `str(ApplicationStatus)`).
    """
    source, target = edge
    rows = (
        (
            await db.execute(
                select(Event)
                .where(
                    Event.aggregate_id == application_id,
                    Event.type == "application.status.changed",
                )
                .order_by(Event.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        e
        for e in rows
        if e.payload.get("from_status") == str(source)
        and e.payload.get("to_status") == str(target)
    ]


async def _audit_events(db, application_id, event_type=None):
    """Diagnostics the audit boundary wrote for this application.

    Read from the DOMAIN connection on purpose. These rows were written on a
    different connection in a different transaction, so seeing them here is
    itself the assertion: an uncommitted or same-transaction write would be
    invisible from this side.
    """
    rows = (
        (
            await db.execute(
                select(Event)
                .where(
                    Event.aggregate_id == application_id,
                    Event.type.startswith("revision.attempt."),
                )
                .order_by(Event.id)
            )
        )
        .scalars()
        .all()
    )
    return [e for e in rows if event_type is None or e.type == event_type]


async def _claim_count(db, application_id) -> int:
    """Claims across every resume of this application.

    Counted at the application level rather than per resume, because the thing
    under test is whether a rolled-back T3 left claim rows behind - and a claim
    orphaned from a discarded resume would not be found by looking under the
    resumes that survived.
    """
    return len(
        (
            await db.execute(
                select(ResumeClaim.id)
                .join(Resume, Resume.id == ResumeClaim.resume_id)
                .where(Resume.application_id == application_id)
            )
        )
        .scalars()
        .all()
    )


async def test_http_failure_keeps_decision_and_attempt_then_retry_reuses_it(
    scenario, monkeypatch
):
    """PO-4: a failed generation preserves evidence instead of erasing it.

    This test replaces an earlier one that asserted the opposite - that a
    pipeline failure rolled the human decision back out of existence. That
    behaviour was not a weaker version of this one; it was the bug. An attempt
    can cost real money and real time, and a system whose only record of it is
    an absence cannot answer "what happened" or "should this be retried".

    So the assertions below come in pairs: what survives (decision, attempt,
    failure metadata) and what must not (a half-written resume, orphan claims).
    Durable evidence is worth nothing if it is durable *partial* domain data.
    """
    from packages.core.services import resume_revision
    from packages.shared.errors import DependencyUnavailableError

    _, app_id, approval_id, maker, *_ = scenario
    async with maker() as db:
        claims_before = await _claim_count(db, app_id)

    original = resume_revision.persist_resume

    async def fail_after_persistence(*args, **kwargs):
        # After persistence, not instead of it: this is the T3 window where
        # domain rows exist in the transaction and must not reach disk.
        await original(*args, **kwargs)
        raise DependencyUnavailableError("Simulated persistence boundary failure")

    monkeypatch.setattr(resume_revision, "persist_resume", fail_after_persistence)
    response = await post(scenario)
    assert response.status_code == 503, response.text

    async with maker() as db:
        # --- what survives -------------------------------------------------
        approval = await db.get(Approval, approval_id)
        assert approval.status == ApprovalStatus.CHANGES_REQUESTED
        assert approval.changes_requested == c3.TIGHTEN
        assert len(await c2._events(db, app_id, "approval.changes_requested")) == 1

        attempts = await _attempts(db, app_id)
        assert len(attempts) == 1
        attempt = attempts[0]
        attempt_id = attempt.id
        assert attempt.source_approval_id == approval_id
        assert attempt.attempt_count == 1
        assert attempt.status is RevisionAttemptStatus.ACCEPTED
        assert attempt.failure_class is RevisionFailureClass.PERSISTENCE_FAILED
        # Retryable, so the attempt is reusable rather than closed: the lease
        # is clear, and `recovered_at` stays NULL because Batch 1 reserves that
        # stamp for terminal failures. The application still has to move - the
        # two are separate concerns (see resume_rework._t5_recover).
        assert attempt.lease_owner is None
        assert attempt.lease_expires_at is None
        assert attempt.recovered_at is None
        assert attempt.finished_at is None
        assert not attempt.retry_exhausted
        assert attempt.failure_detail
        # The detail names the exception type and nothing else - no model
        # prose, no reviewer text. See resume_rework.classify.
        assert "DependencyUnavailableError" in attempt.failure_detail
        assert c3.TIGHTEN not in attempt.failure_detail

        # --- what must not survive ----------------------------------------
        assert len(await c3._resumes(db, app_id)) == 1
        assert await _claim_count(db, app_id) == claims_before

        # --- and the application is actionable again -----------------------
        assert (await db.get(Application, app_id)).status == "changes_requested"
        # Both transitions into `changes_requested` exist, and the test can
        # tell them apart. The T1 decision is asserted on its own so that a
        # predicate which quietly collapsed the two would fail here rather
        # than pass by counting the wrong row.
        assert len(await _history(db, app_id, DECISION_EDGE)) == 1
        assert len(await _status_events(db, app_id, DECISION_EDGE)) == 1

        recovery = await _history(db, app_id, RECOVERY_EDGE)
        assert len(recovery) == 1
        assert recovery[0].actor_type == "human"
        # The actor id is written by code that runs AFTER the rollback, from a
        # primitive snapshotted before it. `decided_by` was written in T1,
        # before the rollback, by the same request. They must agree: if the
        # actor were still an ORM instance at T5 it would be expired, and
        # reading `.id` off it raises rather than producing this value.
        assert recovery[0].actor_id == str(approval.decided_by)

        recovered_event = await _status_events(db, app_id, RECOVERY_EDGE)
        assert len(recovered_event) == 1
        assert recovered_event[0].actor_type == "human"
        assert recovered_event[0].actor_id == str(approval.decided_by)

    # --- the retry continues the same attempt, it does not open a new one ---
    monkeypatch.setattr(resume_revision, "persist_resume", original)
    retry = await post(scenario)
    assert retry.status_code == 200, retry.text
    assert retry.json()["resume_version"] == 2

    async with maker() as db:
        attempts = await _attempts(db, app_id)
        assert len(attempts) == 1
        assert attempts[0].id == attempt_id
        assert attempts[0].status is RevisionAttemptStatus.SUCCEEDED
        # Two acquisitions of one attempt, not two attempts. This is the
        # single assertion that distinguishes "retry" from "start over", and
        # it is what the three-cycle limit will later be counted against.
        assert attempts[0].attempt_count == 2
        assert attempts[0].result_version == 2
        # Never stamped: this attempt was never terminal.
        assert attempts[0].recovered_at is None
        assert len(await c3._resumes(db, app_id)) == 2


async def test_http_terminal_failure_recovers_and_refuses_an_automatic_retry(
    scenario, monkeypatch
):
    """The other half of T5, and it must NOT look like the retryable half.

    An unretryable class closes the attempt for good. The application is still
    returned to `changes_requested` - a human has to be able to act - but the
    attempt is stamped `recovered_at` and a further request is refused rather
    than silently starting a second attempt against the same approval.
    Releasing and replacing it is an explicit human action (PO-3), not
    something a retry quietly does.
    """
    from packages.agents.llm.runtime import ModelOutputError
    from packages.core.services import resume_revision

    async def fail_unretryably(*args, **kwargs):
        raise ModelOutputError("Simulated unusable model output")

    monkeypatch.setattr(resume_revision, "persist_resume", fail_unretryably)
    response = await post(scenario)
    assert response.status_code >= 400, response.text

    _, app_id, approval_id, maker, *_ = scenario
    async with maker() as db:
        attempts = await _attempts(db, app_id)
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt.status is RevisionAttemptStatus.FAILED
        assert attempt.failure_class is RevisionFailureClass.MODEL_OUTPUT_INVALID
        assert attempt.lease_owner is None
        assert attempt.recovered_at is not None
        assert attempt.finished_at is not None
        # The decision survives and the application is actionable again.
        assert (await db.get(Approval, approval_id)).status == (
            ApprovalStatus.CHANGES_REQUESTED
        )
        assert (await db.get(Application, app_id)).status == "changes_requested"
        assert len(await _history(db, app_id, RECOVERY_EDGE)) == 1
        assert len(await _status_events(db, app_id, RECOVERY_EDGE)) == 1
        assert len(await _history(db, app_id, DECISION_EDGE)) == 1
        assert len(await c3._resumes(db, app_id)) == 1

    # A terminal attempt is not re-driven, even with the fault removed.
    monkeypatch.undo()
    assert (await post(scenario)).status_code == 409
    async with maker() as db:
        assert len(await _attempts(db, app_id)) == 1


async def test_a_fenced_out_failure_report_is_durable_not_just_logged(
    scenario, monkeypatch, audit_boundary_engine
):
    """Batch 3A's reason for existing, end to end.

    The attempt is fenced out of recording its own failure - the exact state
    that used to leave nothing behind but a `log.warning` nobody queried. The
    domain transaction that discovered it is rolled back; the diagnostic must
    still be on disk, and it must name the attempt, the epoch and the owner
    that could not write.

    Note what is NOT asserted differently from before: the application, the
    approval and the attempt all end exactly where the Batch 2 tests already
    say they do. 3A adds evidence; it changes no T1-T5 outcome.
    """
    from packages.core.repositories.revision_attempts import RevisionAttemptRepository
    from packages.core.services import resume_revision
    from packages.shared.errors import DependencyUnavailableError

    _, app_id, approval_id, maker, *_ = scenario

    original_persist = resume_revision.persist_resume

    async def fail_after_persistence(*args, **kwargs):
        await original_persist(*args, **kwargs)
        raise DependencyUnavailableError("Simulated persistence boundary failure")

    async def fenced_out(*args, **kwargs):
        # What a real takeover looks like from this worker: the guarded
        # UPDATE matches no row and the repository reports it honestly.
        return None

    monkeypatch.setattr(resume_revision, "persist_resume", fail_after_persistence)
    monkeypatch.setattr(RevisionAttemptRepository, "fail", fenced_out)

    response = await post(scenario)
    assert response.status_code == 503, response.text

    async with maker() as db:
        fenced = await _audit_events(db, app_id, "revision.attempt.fenced_out_before_failure")
        assert len(fenced) == 1
        assert fenced[0].actor_type == "system"
        assert fenced[0].payload["attempt_id"]
        assert fenced[0].payload["application_id"] == str(app_id)
        assert fenced[0].payload["approval_id"] == str(approval_id)
        assert fenced[0].payload["lease_epoch"] == 1
        assert fenced[0].payload["detail"]["original_cause"] == "DependencyUnavailableError"
        assert fenced[0].published is False

        # The caller-side companion: PO-4 promises evidence, and this says the
        # promise was not kept for the attempt row itself.
        not_recorded = await _audit_events(db, app_id, "revision.attempt.failure_not_recorded")
        assert len(not_recorded) == 1

        # And the human decision is untouched by any of it.
        assert (await db.get(Approval, approval_id)).status == (
            ApprovalStatus.CHANGES_REQUESTED
        )


# =====================================================================
# Replay while a lease is live (Codex review, _already_decided)
#
# The invariant: an idempotent replay of an already-recorded human decision
# must not change application state while another worker owns a live revision
# lease. A review traced the path and found no production bug - `_t1` skips
# `request_changes` when the decision matches, and `_decidable_approval`
# refuses outright from `resume_generating`. What was missing was any test
# that would NOTICE if that stopped being true, which matters more than usual
# here: adding `resume_generating -> changes_requested` for T5 made the
# transition these paths decline to perform a legal one. The state machine no
# longer refuses it on their behalf; only they do.
#
# So these tests pin the absence of an effect. No production code changes.
# =====================================================================
async def _drain(task, *, timeout: float = 60) -> None:
    """Let an in-flight request FINISH. Cancel only if it refuses to.

    **Completion first, cancellation as a last resort, and the order is the
    whole point.** An earlier version cancelled any task that was not already
    done, which in the `finally` of these tests meant cancelling a request that
    had only just been unparked and still had to finish generating, commit and
    return. Cancelling an httpx/ASGI request mid-flight tears the app out from
    under AnyIO's memory object streams without closing them; the stream is
    collected later and raises from `__del__`, which pytest reports as a
    `PytestUnraisableExceptionWarning` against whichever test happened to
    trigger the GC. The symptom therefore lands on an innocent test in a
    different part of the file, which is a genuinely awful thing to debug.

    So: wait for the task, and only cancel if it is still running after
    `timeout` - at which point something is actually wrong and a cancelled
    stream is the lesser problem. Exceptions are retrieved and swallowed
    either way, because if we are here in a `finally` an assertion has usually
    already failed and that assertion is the failure worth seeing.
    """
    if task is None:
        return
    if not task.done():
        try:
            await asyncio.wait_for(task, timeout=timeout)
            return
        except TimeoutError:
            # Genuinely stuck. Now cancellation is the right call.
            task.cancel()
        except Exception:
            # The request itself raised. Retrieved, so no "exception was never
            # retrieved" warning, and not re-raised over the primary failure.
            return
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def test_a_duplicate_request_during_generation_changes_no_state(
    scenario, monkeypatch, audit_boundary_engine
):
    """A second identical HTTP request arrives while the winner is generating.

    It must reach the bounded wait and leave everything exactly as it found
    it: the application still `resume_generating`, the lease still the
    winner's at the same epoch, no recovery history, no recovery event, no
    diagnostic. Asserted against a snapshot taken before the duplicate, so
    the test cannot pass by comparing a value with itself.

    **Both overlaps are synchronised on events, never on elapsed time.** An
    earlier version slept 0.75s and hoped the duplicate had got far enough.
    That is the shape of a test that passes on a developer laptop and fails in
    CI once a year, and the failure would look like a concurrency bug in the
    code under test rather than a bug in the test - the most expensive kind of
    false signal this suite could produce.
    """
    from packages.core.repositories.revision_attempts import RevisionAttemptRepository
    from packages.core.services import resume_revision

    _, app_id, approval_id, maker, *_ = scenario
    released = asyncio.Event()
    started = asyncio.Event()
    waiting = asyncio.Event()
    original_generate = resume_revision.generate_revision
    original_get = RevisionAttemptRepository.get

    async def pause_inside_generation(*args, **kwargs):
        # T2 has committed by now: the application is in `resume_generating`
        # and the lease is held. This is the window the replay must not
        # disturb.
        started.set()
        await released.wait()
        return await original_generate(*args, **kwargs)

    async def signal_once_waiting(self, entity_id, include_deleted=False):
        # `_acquire_or_wait` calls this exactly once per poll tick, and only
        # AFTER `acquire()` returned None and `session.rollback()` ran - it is
        # the single call site of `.get()` in the whole rework module. So
        # reaching here means the duplicate has genuinely failed acquisition
        # and settled into the wait path, which is the state the snapshot
        # below needs to be true. The winner never gets here: it acquires on
        # its first tick and returns.
        row = await original_get(self, entity_id, include_deleted)
        waiting.set()
        return row

    monkeypatch.setattr(resume_revision, "generate_revision", pause_inside_generation)
    monkeypatch.setattr(RevisionAttemptRepository, "get", signal_once_waiting)

    winner = asyncio.create_task(post(scenario))
    duplicate = None
    try:
        await asyncio.wait_for(started.wait(), timeout=30)

        async with maker() as db:
            before = (await _attempts(db, app_id))[0]
            snapshot = (
                (await db.get(Application, app_id)).status,
                before.id,
                before.lease_owner,
                before.lease_epoch,
                before.attempt_count,
                len(await _history(db, app_id, RECOVERY_EDGE)),
                len(await _audit_events(db, app_id)),
            )
        assert snapshot[0] == "resume_generating"
        assert snapshot[2] is not None, "the winner should hold the lease here"

        # The replay. It cannot acquire, so it waits; the point is what it
        # does NOT do while waiting.
        duplicate = asyncio.create_task(post(scenario))
        await asyncio.wait_for(waiting.wait(), timeout=30)

        async with maker() as db:
            during = (await _attempts(db, app_id))[0]
            assert (
                (await db.get(Application, app_id)).status,
                during.id,
                during.lease_owner,
                during.lease_epoch,
                during.attempt_count,
                len(await _history(db, app_id, RECOVERY_EDGE)),
                len(await _audit_events(db, app_id)),
            ) == snapshot

        released.set()
        assert (await asyncio.wait_for(winner, timeout=60)).status_code == 200
        assert (await asyncio.wait_for(duplicate, timeout=60)).status_code == 200

        async with maker() as db:
            # One attempt, one new resume, and the replay adopted the winner's
            # result rather than recovering or regenerating.
            attempts = await _attempts(db, app_id)
            assert len(attempts) == 1 and attempts[0].id == snapshot[1]
            assert len(await c3._resumes(db, app_id)) == 2
            assert await _history(db, app_id, RECOVERY_EDGE) == []
            assert await _audit_events(db, app_id) == []
            assert (await db.get(Approval, approval_id)).status == (
                ApprovalStatus.CHANGES_REQUESTED
            )
    finally:
        # Idempotent, and the reason this is a `finally`: an assertion above
        # leaves the winner parked on `released.wait()` for ever, and a test
        # that hangs teaches nobody anything.
        released.set()
        await _drain(winner)
        await _drain(duplicate)


async def test_the_c2_service_refuses_a_decision_during_generation(
    scenario, monkeypatch, demo_application
):
    """The same window, one layer down, called directly rather than over HTTP.

    `resume_review.request_changes` is C2's entry point and has no knowledge
    of leases. Its protection is `_decidable_approval`, which requires
    `awaiting_approval`. This asserts the refusal AND that nothing moved -
    `transition_application` raises before mutating, so a leaked write here
    would mean the guard ran in the wrong order.
    """
    from packages.core.services import resume_revision

    _, app_id, approval_id, maker, *_ = scenario
    released = asyncio.Event()
    started = asyncio.Event()
    original = resume_revision.generate_revision

    async def pause_inside_generation(*args, **kwargs):
        started.set()
        await released.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(resume_revision, "generate_revision", pause_inside_generation)
    winner = asyncio.create_task(post(scenario))
    await asyncio.wait_for(started.wait(), timeout=10)

    try:
        async with maker() as db:
            application = await db.get(Application, app_id)
            assert application.status == "resume_generating"
            # The fixture dict, not the ORM row: `c2._actor` reads
            # `["user_id"]`, which only the fixture carries. Passing a
            # hand-built `{"id": ...}` was inventing a shape the helper never
            # had - `scenario` already depends on `demo_application`, so this
            # is the same object every neighbouring test uses.
            actor = await c2._actor(db, demo_application)
            before = (
                (await db.get(Approval, approval_id)).status,
                len(await c2._events(db, app_id, "approval.changes_requested")),
                len(await _history(db, app_id, RECOVERY_EDGE)),
            )
            with pytest.raises(resume_review.ReviewStateError):
                await resume_review.request_changes(
                    db, application_id=app_id, actor=actor, note="a different note"
                )
            await db.rollback()

        async with maker() as db:
            assert (await db.get(Application, app_id)).status == "resume_generating"
            assert (
                (await db.get(Approval, approval_id)).status,
                len(await c2._events(db, app_id, "approval.changes_requested")),
                len(await _history(db, app_id, RECOVERY_EDGE)),
            ) == before

        # Let the winner finish and assert it did, rather than leaving that to
        # cleanup. A refused replay must not have damaged the request that was
        # legitimately in flight, and a swallowed result could not tell us so.
        released.set()
        assert (await asyncio.wait_for(winner, timeout=60)).status_code == 200
    finally:
        # Release first, then let the winner run to completion: it is a live
        # HTTP request, and tearing it down here is what leaked an AnyIO
        # stream into the next test.
        released.set()
        await _drain(winner)


async def test_a_clean_run_writes_no_diagnostics(scenario, audit_boundary_engine):
    """The other half, and the one that keeps the first honest.

    Diagnostics that fire on the happy path are noise, and noise is how a real
    one gets ignored. A successful revision must leave none.
    """
    assert (await post(scenario)).status_code == 200
    _, app_id, _, maker, *_ = scenario
    async with maker() as db:
        assert await _audit_events(db, app_id) == []


async def test_http_verification_failure_stops_and_retry_reuses(scenario, tmp_path):
    _, app_id, _, maker, model, revision = scenario
    async with maker() as db:
        await c3._stage_cassettes(
            db,
            tmp_path,
            model,
            revision,
            bullet_text="Built infrastructure with AWS.",
            verdict="unsupported",
        )
    first = await post(scenario)
    assert first.status_code == 200, first.text
    assert first.json()["application_status"] == "verification_failed"
    assert not first.json()["can_approve"]
    assert not first.json()["can_start_review"]
    second = await post(scenario)
    assert second.status_code == 200
    assert second.json()["resume_id"] == first.json()["resume_id"]


@pytest.mark.parametrize("case", ["csrf", "auth", "stale", "empty", "identity", "missing_id"])
async def test_http_guards_do_not_start_generation(scenario, case):
    http, app_id, _, maker, *_ = scenario
    kwargs = {}
    expected = 422
    if case == "csrf":
        http.headers["X-CSRF-Token"] = "invalid"
        expected = 403
    elif case == "auth":
        http.cookies.clear()
        expected = 401
    elif case == "stale":
        kwargs["approval_id"] = str(uuid7())
        expected = 409
    elif case == "empty":
        kwargs["note"] = "   "
    elif case == "identity":
        kwargs["decided_by"] = str(uuid7())
    elif case == "missing_id":
        response = await http.post(
            f"/api/v1/applications/{app_id}/resume-review/request-changes",
            json={"note": c3.TIGHTEN},
        )
    if case != "missing_id":
        response = await post(scenario, **kwargs)
    assert response.status_code == expected, response.text
    async with maker() as db:
        assert (await db.get(Application, app_id)).status == "awaiting_approval"
        assert not await c2._events(db, app_id, "resume.generation.requested")


async def test_concurrent_http_retries_create_one_revision(scenario, monkeypatch):
    generating = asyncio.Event()
    release = asyncio.Event()
    loser_polling = asyncio.Event()
    resume_polling = asyncio.Event()
    calls = {"begin": 0, "generate": 0}
    original_begin = resume_revision.begin_generation
    original_generate = resume_revision.generate_revision
    original_get = RevisionAttemptRepository.get

    async def begin(*args, **kwargs):
        calls["begin"] += 1
        return await original_begin(*args, **kwargs)

    async def generate(*args, **kwargs):
        calls["generate"] += 1
        generating.set()
        await release.wait()
        return await original_generate(*args, **kwargs)

    async def get(repo, *args, **kwargs):
        result = await original_get(repo, *args, **kwargs)
        loser_polling.set()
        await resume_polling.wait()
        return result

    monkeypatch.setattr(resume_revision, "begin_generation", begin)
    monkeypatch.setattr(resume_revision, "generate_revision", generate)
    monkeypatch.setattr(RevisionAttemptRepository, "get", get)
    _, app_id, _, maker, *_ = scenario
    winner = asyncio.create_task(post(scenario))
    tasks = [winner]
    try:
        async with asyncio.timeout(10):
            await generating.wait()
            tasks.append(asyncio.create_task(post(scenario)))
            await loser_polling.wait()
            assert calls == {"begin": 1, "generate": 1}
            # The winner is outside T2 and the loser has rolled back its
            # failed acquisition. Neither may hold the application lock.
            async with maker() as db:
                await db.execute(
                    select(Application).where(Application.id == app_id)
                    .with_for_update(nowait=True)
                )
                await db.execute(
                    select(ResumeRevisionAttempt)
                    .where(ResumeRevisionAttempt.application_id == app_id)
                    .with_for_update(nowait=True)
                )
                await db.rollback()
            resume_polling.set()
            release.set()
            responses = await asyncio.gather(*tasks)
    finally:
        resume_polling.set()
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json()["resume_id"] == responses[1].json()["resume_id"]
    assert responses[0].json()["resume_version"] == responses[1].json()["resume_version"] == 2
    assert calls == {"begin": 1, "generate": 1}
    async with maker() as db:
        attempts = await _attempts(db, app_id)
        assert len(attempts) == 1
        assert attempts[0].attempt_count == 1
        assert attempts[0].status is RevisionAttemptStatus.SUCCEEDED
        assert len(await c3._resumes(db, app_id)) == 2
        assert len(await c2._events(db, app_id, "resume.generation.requested")) == 1


@pytest.mark.parametrize("phase", ["acquire", "terminal", "retryable"])
async def test_application_lock_precedes_attempt_access(
    scenario, demo_application, monkeypatch, phase
):
    """Force a real application-lock wait before T2 or T5 can touch the attempt."""
    _, app_id, approval_id, maker, *_ = scenario
    async with maker() as setup:
        actor = await c2._actor(setup, demo_application)
        actor_id = actor.id
        attempt, request = await resume_rework._t1_decide_and_record(
            setup, application_id=app_id, approval_id=approval_id,
            actor=actor, instructions=c3.TIGHTEN,
        )
        attempt_id = attempt.id

    async with maker() as blocker, maker() as worker, maker() as observer:
        # Keep a strong reference to stale ORM state across the locking load.
        stale = await worker.get(Application, app_id)
        assert stale.status is ApplicationStatus.CHANGES_REQUESTED
        await worker.commit()
        await resume_review._load_application(blocker, app_id, lock=True)
        repo = RevisionAttemptRepository(blocker)
        if phase != "acquire":
            epoch = await repo.acquire(
                attempt_id, owner="failed-worker", lease_seconds=300, max_attempts=3
            )
            await resume_revision.begin_generation(
                blocker, application_id=app_id, request=request, actor_id=actor_id
            )
            await repo.fail(
                attempt_id, owner="failed-worker", epoch=epoch,
                failure_class=(RevisionFailureClass.BUDGET_EXCEEDED
                               if phase == "terminal"
                               else RevisionFailureClass.PERSISTENCE_FAILED),
                detail="lock-order regression", max_attempts=3, evidence_stale_budget=1,
            )

        touched = []
        method = "acquire" if phase == "acquire" else "recovery_state"
        original = getattr(RevisionAttemptRepository, method)

        async def inspect(repo, *args, **kwargs):
            if repo.session is worker:
                touched.append(method)
            return await original(repo, *args, **kwargs)

        monkeypatch.setattr(RevisionAttemptRepository, method, inspect)
        pid = await worker.scalar(text("SELECT pg_backend_pid()"))

        async def run():
            if phase == "acquire":
                acquired_epoch = await resume_rework._acquire_or_wait(
                    worker, attempts=RevisionAttemptRepository(worker),
                    attempt_id=attempt_id, application_id=app_id,
                    approval_id=approval_id, owner="winner", settings=Settings(),
                )
                assert acquired_epoch == 1
                await resume_revision.begin_generation(
                    worker, application_id=app_id, request=request, actor_id=actor_id
                )
                await worker.commit()
            else:
                await resume_rework._t5_recover(
                    worker,
                    context=AuditContext(
                        attempt_id=attempt_id, application_id=app_id,
                        approval_id=approval_id, lease_epoch=epoch,
                    ),
                    actor_id=actor_id,
                )

        task = asyncio.create_task(run())
        try:
            async with asyncio.timeout(10):
                # Observe PostgreSQL's actual wait, rather than assume that a
                # sleep gave the other task enough time to reach the lock.
                while not await observer.scalar(
                    text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
                ):
                    assert not task.done(), "worker bypassed the application lock"
                    await asyncio.sleep(0.01)
                assert touched == [], "attempt accessed before application lock"
                if phase == "retryable":
                    # A new owner wins while recovery waits. T5 must inspect
                    # eligibility after the lock and leave that owner alone.
                    assert await repo.acquire(
                        attempt_id, owner="new-owner", lease_seconds=300, max_attempts=3
                    ) == 2
                else:
                    # Duplicate T1's unique-conflict check must finish while
                    # the other session waits for this application.
                    duplicate = await repo.resolve_or_create(
                        request, requested_by=actor_id,
                        instructions_digest=resume_rework.instructions_digest(c3.TIGHTEN),
                    )
                    assert duplicate.id == attempt_id
                await blocker.commit()
                await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await blocker.rollback()
            await worker.rollback()

    async with maker() as db:
        application = await db.get(Application, app_id)
        attempts = await _attempts(db, app_id)
        assert len(attempts) == 1
        if phase == "terminal":
            assert application.status is ApplicationStatus.CHANGES_REQUESTED
            assert attempts[0].recovered_at is not None
            assert len(await _history(db, app_id, RECOVERY_EDGE)) == 1
        else:
            assert application.status is ApplicationStatus.RESUME_GENERATING
            assert attempts[0].recovered_at is None
            if phase == "retryable":
                assert attempts[0].lease_owner == "new-owner"
                assert attempts[0].attempt_count == 2
                assert not await _history(db, app_id, RECOVERY_EDGE)


async def test_preexisting_committed_change_request_can_resume(scenario):
    _, app_id, approval_id, maker, *_ = scenario
    async with maker() as db:
        from packages.core.models import User

        application = await db.get(Application, app_id)
        from packages.core.models import Profile

        profile = await db.get(Profile, application.profile_id)
        actor = await db.get(User, profile.user_id)
        await resume_review.request_changes(db, application_id=app_id, actor=actor, note=c3.TIGHTEN)
        await db.commit()
    assert (await post(scenario)).status_code == 200
