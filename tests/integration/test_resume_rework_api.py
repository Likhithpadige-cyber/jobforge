"""Authenticated HTTP C3 integration with real PostgreSQL and replayed model calls."""

import asyncio

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
from packages.core.models import (
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
async def scenario(session, engine, demo_profile, demo_job, demo_application, inputs, tmp_path):
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
                epoch = await resume_rework._acquire_or_wait(
                    worker, attempts=RevisionAttemptRepository(worker),
                    attempt_id=attempt_id, application_id=app_id,
                    owner="winner", settings=Settings(),
                )
                assert epoch == 1
                await resume_revision.begin_generation(
                    worker, application_id=app_id, request=request, actor_id=actor_id
                )
                await worker.commit()
            else:
                await resume_rework._t5_recover(
                    worker, attempt_id=attempt_id, application_id=app_id, actor_id=actor_id
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
