"""M12 durable incident, inference, delivery, and human-resolution invariants."""

import asyncio
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from packages.agents.llm.runtime import Request, StructuredClient
from packages.core.events.guardrail_alerts import open_guardrail_alert
from packages.core.events.outbox import emit
from packages.core.events.runtime import DeliveryRuntime, stream_for
from packages.core.generation_safety import generation_is_paused, resolve_alert
from packages.core.models import (
    Alert,
    Application,
    Approval,
    CoverLetter,
    CoverLetterClaim,
    Event,
    ProcessedEvent,
    Resume,
    ResumeRevisionAttempt,
    User,
)
from packages.core.services.cover_letters import CoverLetterClient, generate_cover_letter
from packages.shared.config import Settings
from packages.shared.ids import uuid7
from tests.integration import test_first_draft as first
from tests.integration import test_resume_rework_api as rework
from tests.integration.conftest import _url

pytestmark = [
    pytest.mark.integration,
    pytest.mark.safety,
    pytest.mark.usefixtures("audit_boundary_engine"),
]
committed_job = first.committed_job
candidate = first.candidate
source = first.source
inputs = rework.inputs
scenario = rework.scenario


@pytest.fixture
async def incidents(engine):
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    actor_id = uuid7()
    async with sessions.begin() as db:
        db.add(User(id=actor_id, email=f"{actor_id}@example.com", role="owner"))
    ids = []

    async def create(kind="fabrication", severity="critical"):
        async with sessions.begin() as db:
            event = await emit(
                db,
                type="guardrail.violation.detected",
                aggregate_type="job",
                aggregate_id=uuid7(),
                correlation_id=uuid7(),
                actor_type="agent",
                payload={
                    "kind": kind,
                    "severity": severity,
                    "detail": "PRIVATE GENERATED TEXT",
                    "api_key": "synthetic-secret-marker",  # pragma: allowlist secret
                    "agent": "resume_optimizer",
                },
            )
            event_id = event.id
        runtime = DeliveryRuntime(
            sessions,
            "cg.monitor",
            {
                "guardrail.violation.detected": open_guardrail_alert,
            },
        )
        assert await runtime.process(event_id) == "completed"
        alert_id = uuid5(NAMESPACE_URL, f"jobforge:guardrail-alert:{event_id}")
        ids.append(alert_id)
        return alert_id, event_id

    yield create, sessions, actor_id
    # Retain synthetic incident history; do not leave the suite globally paused.
    async with sessions.begin() as db:
        actor = await db.get(User, actor_id)
        for alert_id in ids:
            if await db.get(Alert, alert_id):
                await resolve_alert(db, alert_id, actor=actor)


async def test_pause_persists_until_final_resolution_and_duplicate_cannot_reopen(incidents):
    create, sessions, actor_id = incidents
    async with sessions() as db:
        assert not await generation_is_paused(db)
    a, event_id = await create()
    b, _ = await create("numeral_invented")
    async with sessions.begin() as db:
        assert await generation_is_paused(db)
        actor = await db.get(User, actor_id)
        await resolve_alert(db, a, actor=actor)
    async with sessions.begin() as db:
        assert await generation_is_paused(db)
        await resolve_alert(db, b, actor=await db.get(User, actor_id))
    async with sessions.begin() as db:
        assert not await generation_is_paused(db)
        alert = await db.get(Alert, a)
        original_time = alert.resolved_at
        await resolve_alert(db, a, actor=await db.get(User, actor_id))
        assert alert.resolved_at == original_time
        # Even a direct duplicate bypassing ProcessedEvent does not reopen it.
        await open_guardrail_alert(db, await db.get(Event, event_id))
    runtime = DeliveryRuntime(
        sessions,
        "cg.monitor",
        {
            "guardrail.violation.detected": open_guardrail_alert,
        },
    )
    assert await runtime.process(event_id) == "duplicate"
    async with sessions() as db:
        assert not await generation_is_paused(db)
        alert = await db.get(Alert, a)
        assert alert.status == "resolved" and alert.acknowledged_by == actor_id
        assert alert.auto_paused
        assert "PRIVATE" not in str(alert.detail)
        assert "secret" not in str(alert.detail)
        assert alert.detail["source_event_id"] == str(event_id)


@pytest.mark.parametrize(
    ("kind", "severity", "paused"),
    [
        ("fabrication", "critical", True),
        ("numeral_invented", "critical", True),
        ("sensitive_auto_answer_attempt", "critical", True),
        ("prompt_injection", "high", False),
        ("overreach", "high", False),
        ("fabrication", "high", False),
    ],
)
async def test_exact_policy(incidents, kind, severity, paused):
    create, sessions, _ = incidents
    alert_id, _ = await create(kind, severity)
    async with sessions() as db:
        assert (await db.get(Alert, alert_id)).auto_paused is paused
        assert await generation_is_paused(db) is paused


async def test_concurrent_incident_insert_and_resolution(incidents):
    create, sessions, actor_id = incidents
    alert_id, event_id = await create()

    async def duplicate():
        async with sessions.begin() as db:
            await open_guardrail_alert(db, await db.get(Event, event_id))

    await asyncio.gather(duplicate(), duplicate())

    async def resolve():
        async with sessions.begin() as db:
            alert = await resolve_alert(db, alert_id, actor=await db.get(User, actor_id))
            return alert.resolved_at

    times = await asyncio.gather(resolve(), resolve())
    assert times[0] == times[1]
    async with sessions() as db:
        assert (
            await db.scalar(select(func.count()).select_from(Alert).where(Alert.id == alert_id))
            == 1
        )
        assert not await generation_is_paused(db)


async def test_real_call_boundary_observes_committed_pause_in_new_session(incidents):
    from packages.core.generation_safety import GenerationPausedError

    create, sessions, _ = incidents
    await create()
    transport = AsyncMock(side_effect=AssertionError("provider called"))
    client = StructuredClient(Settings(llm_mode="live"), transport=transport)
    async with sessions() as db:
        with pytest.raises(GenerationPausedError):
            await client.complete(Request(client.model, "Safe", "{}", {}, "test"), session=db)
    transport.assert_not_called()


async def test_first_draft_pause_is_durable_block_not_poison(source, incidents, tmp_path):
    create, _, _ = incidents
    request = await first.enqueue(source)
    client = await first.cassettes(source, tmp_path)
    client.transport = AsyncMock(side_effect=AssertionError("provider called"))
    await create()
    runtime = first.worker(source, client)
    assert await runtime.process(request.event_id) == "completed"
    assert await runtime.process(request.event_id) == "duplicate"
    client.transport.assert_not_called()
    async with source["sessions"]() as db:
        assert (await db.get(Application, request.application_id)).status == "created"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Resume)
                .where(Resume.application_id == request.application_id)
            )
            == 0
        )
        claim = await db.get(ProcessedEvent, (request.event_id, "cg.first-draft"))
        assert claim.result == "blocked:generation_paused:1"
        assert not (
            await db.scalars(
                select(Event).where(
                    Event.causation_id == request.event_id,
                    Event.type.in_(["resume.generated", "agent.run.failed"]),
                )
            )
        ).all()


async def test_revision_pause_preserves_approval_and_attempt_state(scenario, incidents):
    create, _, _ = incidents
    await create()
    _, app_id, approval_id, sessions, client, _ = scenario
    client.transport = AsyncMock(side_effect=AssertionError("provider called"))
    response = await rework.post(scenario)
    assert response.status_code == 409, response.text
    client.transport.assert_not_called()
    async with sessions() as db:
        assert (await db.get(Application, app_id)).status == "awaiting_approval"
        assert (await db.get(Approval, approval_id)).status == "pending"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ResumeRevisionAttempt)
                .where(ResumeRevisionAttempt.application_id == app_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count()).select_from(Resume).where(Resume.application_id == app_id)
            )
            == 1
        )


async def test_cover_letter_pause_has_no_domain_writes_or_redelivery(scenario, incidents):
    create, _, _ = incidents
    _, app_id, approval_id, sessions, _, _ = scenario
    await create()
    async with sessions.begin() as db:
        event = await emit(
            db,
            type="cover_letter.generation.requested",
            aggregate_type="application",
            aggregate_id=app_id,
            correlation_id=app_id,
            actor_type="system",
            payload={"application_id": str(app_id)},
        )
        event_id = event.id
    client = CoverLetterClient(Settings(llm_mode="replay"), transport=AsyncMock())

    async def handle(db, event):
        await generate_cover_letter(db, application_id=app_id, event_id=event.id, client=client)
        return "completed"

    runtime = DeliveryRuntime(
        sessions,
        "cg.cover-letter",
        {
            "cover_letter.generation.requested": handle,
        },
    )
    assert await runtime.process(event_id) == "completed"
    assert await runtime.process(event_id) == "duplicate"
    client.transport.assert_not_called()
    async with sessions() as db:
        assert (await db.get(Application, app_id)).status == "awaiting_approval"
        assert (await db.get(Approval, approval_id)).status == "pending"
        assert not (
            await db.scalars(select(CoverLetter).where(CoverLetter.application_id == app_id))
        ).all()
        assert not (
            await db.scalars(
                select(CoverLetterClaim)
                .join(CoverLetter)
                .where(CoverLetter.application_id == app_id)
            )
        ).all()
        assert not (
            await db.scalars(
                select(Event).where(
                    Event.type == "cover_letter.generated", Event.causation_id == event_id
                )
            )
        ).all()
        assert (
            await db.get(ProcessedEvent, (event_id, "cg.cover-letter"))
        ).result == "blocked:generation_paused:1"


async def test_first_draft_pause_mid_pipeline_blocks_then_resumes(source, incidents, tmp_path):
    create, sessions, actor_id = incidents
    request = await first.enqueue(source)
    client = await first.cassettes(source, tmp_path)
    original = client.complete
    calls, paused = [], []

    async def pause_after_b1(*args, **kwargs):
        # B1 completes; the pause then lands between the handler-entry check
        # and the next provider call.
        calls.append(1)
        if len(calls) == 2 and not paused:
            paused.append(await create())
        return await original(*args, **kwargs)

    client.complete = pause_after_b1
    runtime = first.worker(source, client)
    assert await runtime.process(request.event_id) == "completed"
    assert len(paused) == 1
    async with sessions() as db:
        assert (await db.get(Application, request.application_id)).status == "created"
        assert not await db.scalar(
            select(func.count())
            .select_from(Resume)
            .where(Resume.application_id == request.application_id)
        )
        claim = await db.get(ProcessedEvent, (request.event_id, "cg.first-draft"))
        assert claim.result == "blocked:generation_paused:1"
    async with sessions.begin() as db:
        await resolve_alert(db, paused[0][0], actor=await db.get(User, actor_id))
    assert await first.worker(source, client).process(request.event_id) == "completed"
    async with sessions() as db:
        assert (await db.get(Application, request.application_id)).status == "resume_ready"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Resume)
                .where(Resume.application_id == request.application_id)
            )
            == 1
        )


async def test_cover_letter_pause_after_entry_check_writes_nothing(scenario, incidents):
    create, _, _ = incidents
    _, app_id, approval_id, sessions, _, _ = scenario
    async with sessions.begin() as db:
        event = await emit(
            db,
            type="cover_letter.generation.requested",
            aggregate_type="application",
            aggregate_id=app_id,
            correlation_id=app_id,
            actor_type="system",
            payload={"application_id": str(app_id)},
        )
        event_id = event.id
    client = CoverLetterClient(Settings(llm_mode="replay"), transport=AsyncMock())
    original = client.complete

    async def pause_before_first_call(*args, **kwargs):
        await create()
        return await original(*args, **kwargs)

    client.complete = pause_before_first_call

    async def handle(db, event):
        await generate_cover_letter(db, application_id=app_id, event_id=event.id, client=client)
        return "completed"

    runtime = DeliveryRuntime(
        sessions,
        "cg.cover-letter",
        {
            "cover_letter.generation.requested": handle,
        },
    )
    assert await runtime.process(event_id) == "completed"
    client.transport.assert_not_called()
    async with sessions() as db:
        assert (await db.get(Application, app_id)).status == "awaiting_approval"
        assert (await db.get(Approval, approval_id)).status == "pending"
        assert not (
            await db.scalars(select(CoverLetter).where(CoverLetter.application_id == app_id))
        ).all()
        assert (
            await db.get(ProcessedEvent, (event_id, "cg.cover-letter"))
        ).result == "blocked:generation_paused:1"


async def test_real_b2_fabrication_reaches_monitor_and_pauses(source, incidents, tmp_path):
    from packages.core.events.runtime import publish_event

    _, sessions, actor_id = incidents
    request = await first.enqueue(source)
    client = await first.cassettes(source, tmp_path, bullet="Reduced AWS costs by 99%.")
    assert await first.worker(source, client).process(request.event_id) == "dead"
    async with sessions() as db:
        guardrail = await db.scalar(
            select(Event)
            .where(
                Event.type == "guardrail.violation.detected",
                Event.aggregate_id == source["job_id"],
                Event.payload["step"].as_string() == "B2",
            )
            .order_by(Event.occurred_at.desc())
            .limit(1)
        )
        assert guardrail is not None and not guardrail.published
        assert (guardrail.payload["kind"], guardrail.payload["severity"]) == (
            "fabrication",
            "critical",
        )
        event_id = guardrail.id
    alert_id = uuid5(NAMESPACE_URL, f"jobforge:guardrail-alert:{event_id}")
    try:
        # The relay's per-row publish: an id-only reference on the monitor stream.
        redis = AsyncMock()
        async with sessions.begin() as db:
            await publish_event(redis, await db.get(Event, event_id))
        redis.xadd.assert_awaited_once_with("evt.system", {"event_id": str(event_id)})
        monitor = DeliveryRuntime(
            sessions,
            "cg.monitor",
            {
                "guardrail.violation.detected": open_guardrail_alert,
            },
        )
        assert await monitor.process(event_id) == "completed"
        async with sessions() as db:
            alert = await db.get(Alert, alert_id)
            assert alert.auto_paused and alert.status == "open"
            assert alert.detail["source_event_id"] == str(event_id)
            assert await generation_is_paused(db)
    finally:
        async with sessions.begin() as db:
            if await db.get(Alert, alert_id):
                await resolve_alert(db, alert_id, actor=await db.get(User, actor_id))


async def test_owner_api_safe_inspection_and_idempotent_resolution(scenario, incidents):
    create, _, _ = incidents
    alert_id, _ = await create()
    http = scenario[0]
    assert (await http.get("/api/v1/alerts/generation-pause")).json() == {
        "generation_paused": True,
        "unresolved_auto_pause_count": 1,
    }
    listed = await http.get("/api/v1/alerts")
    assert listed.status_code == 200
    assert str(alert_id) in listed.text
    assert "PRIVATE" not in listed.text and "secret" not in listed.text
    assert "detail" not in listed.json()[0]
    first_response = await http.post(f"/api/v1/alerts/{alert_id}/resolve")
    assert first_response.status_code == 200, first_response.text
    assert first_response.json()["status"] == "resolved"
    assert (await http.post(f"/api/v1/alerts/{alert_id}/resolve")).json() == first_response.json()
    assert (await http.get("/api/v1/alerts/generation-pause")).json() == {
        "generation_paused": False,
        "unresolved_auto_pause_count": 0,
    }
    assert (await http.get(f"/api/v1/alerts/{alert_id}")).status_code == 200


async def test_monitor_privileges_are_narrow_and_sufficient(engine):
    monitor = create_async_engine(_url("app_monitor"))
    sessions = async_sessionmaker(monitor, expire_on_commit=False)
    try:
        async with sessions.begin() as db:
            event = Event(
                id=uuid7(),
                type="guardrail.violation.detected",
                version=1,
                aggregate_type="system",
                payload={"kind": "fabrication", "severity": "critical"},
                actor_type="agent",
                correlation_id=uuid7(),
            )
            db.add(event)
            await db.flush()
            await open_guardrail_alert(db, event)
            assert await generation_is_paused(db)
            for table in [
                "applications",
                "approvals",
                "submissions",
                "resumes",
                "cover_letters",
                "profile_facts",
            ]:
                assert not await db.scalar(
                    text("SELECT has_table_privilege(current_user, :t, :p)"),
                    {"t": table, "p": "INSERT, UPDATE, DELETE"},
                )
            await db.rollback()
    finally:
        await monitor.dispose()


def test_guardrails_route_to_existing_monitor_stream():
    assert stream_for("guardrail.violation.detected") == "evt.system"


async def test_existing_revision_attempt_is_untouched_while_paused(scenario, incidents):
    from packages.core.services import resume_rework
    from tests.integration.test_resume_revision import TIGHTEN

    create, _, _ = incidents
    _, app_id, approval_id, sessions, _, _ = scenario
    async with sessions() as db:
        approval = await db.get(Approval, approval_id)
        from packages.core.models import Profile

        application = await db.get(Application, app_id)
        profile = await db.get(Profile, application.profile_id)
        actor = await db.get(User, profile.user_id)
        attempt, _ = await resume_rework._t1_decide_and_record(
            db,
            application_id=app_id,
            approval_id=approval.id,
            actor=actor,
            instructions=TIGHTEN,
        )
        attempt_id = attempt.id
        before = (attempt.status, attempt.lease_epoch, attempt.attempt_count)
    await create()
    assert (await rework.post(scenario)).status_code == 409
    async with sessions() as db:
        attempt = await db.get(ResumeRevisionAttempt, attempt_id)
        assert (attempt.status, attempt.lease_epoch, attempt.attempt_count) == before
        assert (await db.get(Application, app_id)).status == "changes_requested"
        assert (await db.get(Approval, approval_id)).changes_requested == TIGHTEN


async def test_pause_during_revision_releases_attempt_and_resumes_after_resolution(
    scenario, incidents, monkeypatch
):
    create, _, actor_id = incidents
    _, app_id, approval_id, sessions, model, _ = scenario
    original = model.complete
    paused = []

    async def pause_during_t3(*args, **kwargs):
        # T2 has committed the lease and resume_generating; the pause lands
        # asynchronously, and the real shared boundary must refuse the call.
        if not paused:
            paused.append(await create())
        return await original(*args, **kwargs)

    monkeypatch.setattr(model, "complete", pause_during_t3)

    async def resumes():
        async with sessions() as db:
            return await db.scalar(
                select(func.count()).select_from(Resume).where(Resume.application_id == app_id)
            )

    async def attempt_state():
        async with sessions() as db:
            attempt = await db.scalar(
                select(ResumeRevisionAttempt).where(ResumeRevisionAttempt.application_id == app_id)
            )
            return (
                attempt.status,
                attempt.lease_owner,
                attempt.lease_expires_at,
                attempt.lease_epoch,
                attempt.attempt_count,
                attempt.failure_class,
                attempt.result_resume_id,
            )

    async def outcomes():
        async with sessions() as db:
            return await db.scalar(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.correlation_id == app_id,
                    Event.type.in_(["resume.generated", "resume.verification.failed"]),
                )
            )

    before = await outcomes()
    response = await rework.post(scenario)
    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith("/generation-paused")
    assert len(paused) == 1
    assert await resumes() == 1
    # Recoverable, not failed: no lease, no failure class, no result.
    assert await attempt_state() == ("accepted", None, None, 1, 1, None, None)
    async with sessions() as db:
        assert (await db.get(Application, app_id)).status == "changes_requested"
        approval = await db.get(Approval, approval_id)
        assert approval.status == "changes_requested"
        assert approval.changes_requested == rework.c3.TIGHTEN
    assert await outcomes() == before

    # Still paused: refused before T1, nothing moves.
    assert (await rework.post(scenario)).status_code == 409
    assert await attempt_state() == ("accepted", None, None, 1, 1, None, None)

    async with sessions.begin() as db:
        await resolve_alert(db, paused[0][0], actor=await db.get(User, actor_id))
    response = await rework.post(scenario)
    assert response.status_code == 200, response.text
    assert await resumes() == 2
    assert await outcomes() == before + 1
    status, owner, _, epoch, count, failure, result = await attempt_state()
    assert (status, owner, epoch, count, failure) == ("succeeded", None, 2, 1, None)
    assert result is not None
    # Replaying the same decision after success creates nothing further.
    assert (await rework.post(scenario)).status_code == 200
    assert await resumes() == 2


async def test_pause_between_lease_and_generation_rolls_back_t2(scenario, incidents, monkeypatch):
    from packages.core.services import resume_rework

    create, _, _ = incidents
    _, app_id, approval_id, sessions, model, _ = scenario
    model.transport = AsyncMock(side_effect=AssertionError("provider called"))
    acquire = resume_rework._acquire_or_wait

    async def pause_after_lease(*args, **kwargs):
        epoch = await acquire(*args, **kwargs)
        await create()
        return epoch

    monkeypatch.setattr(resume_rework, "_acquire_or_wait", pause_after_lease)
    assert (await rework.post(scenario)).status_code == 409
    model.transport.assert_not_called()
    async with sessions() as db:
        attempt = await db.scalar(
            select(ResumeRevisionAttempt).where(ResumeRevisionAttempt.application_id == app_id)
        )
        # The uncommitted acquisition was rolled back: no lease, no count.
        assert (attempt.status, attempt.lease_owner, attempt.lease_epoch) == (
            "accepted",
            None,
            0,
        )
        assert attempt.attempt_count == 0 and attempt.failure_class is None
        assert (await db.get(Application, app_id)).status == "changes_requested"
        assert (await db.get(Approval, approval_id)).status == "changes_requested"


async def test_reviewer_cannot_enumerate_or_resolve_existing_incidents(api_sessions, incidents):
    create, _, _ = incidents
    alert_id, _ = await create()
    reviewer = await api_sessions("reviewer")
    for action in ["get", "post"]:
        suffix = "/resolve" if action == "post" else ""
        for target in [alert_id, uuid7()]:
            response = await reviewer.request(action, f"/api/v1/alerts/{target}{suffix}")
            assert response.status_code == 403


async def test_first_draft_resumes_same_event_only_after_final_resolution(
    source, incidents, tmp_path
):
    create, sessions, actor_id = incidents
    request = await first.enqueue(source)
    client = await first.cassettes(source, tmp_path)
    a, _ = await create()
    b, _ = await create()
    runtime = first.worker(source, client)
    assert await runtime.process(request.event_id) == "completed"
    async with sessions.begin() as db:
        await resolve_alert(db, a, actor=await db.get(User, actor_id))
    assert await runtime.process(request.event_id) == "duplicate"
    async with sessions.begin() as db:
        await resolve_alert(db, b, actor=await db.get(User, actor_id))
    # New runtime instance, same durable ledger: no process-local resume signal.
    assert await first.worker(source, client).process(request.event_id) == "completed"
    assert await first.worker(source, client).process(request.event_id) == "duplicate"
    async with sessions() as db:
        assert (await db.get(Application, request.application_id)).status == "resume_ready"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Resume)
                .where(Resume.application_id == request.application_id)
            )
            == 1
        )


async def test_reconciliation_does_not_requeue_while_paused(source, incidents, tmp_path):
    create, sessions, actor_id = incidents
    request = await first.enqueue(source)
    client = await first.cassettes(source, tmp_path)
    alert_id, _ = await create()
    runtime = first.worker(source, client)
    assert await runtime.process(request.event_id) == "completed"
    async with sessions.begin() as db:
        (await db.get(Event, request.event_id)).published = True
    redis = AsyncMock()
    assert await runtime.reconcile(redis, event_ids=[request.event_id]) == 0
    redis.xadd.assert_not_called()
    async with sessions.begin() as db:
        await resolve_alert(db, alert_id, actor=await db.get(User, actor_id))
    assert await runtime.reconcile(redis, event_ids=[request.event_id]) == 1
    redis.xadd.assert_awaited_once()


async def test_crash_during_pause_recovery_keeps_existing_delivery_budget(
    source, incidents, tmp_path
):
    from packages.core.events.runtime import RetryPolicy

    create, sessions, actor_id = incidents
    request = await first.enqueue(source)
    client = await first.cassettes(source, tmp_path)
    alert_id, _ = await create()
    assert await first.worker(source, client).process(request.event_id) == "completed"
    async with sessions.begin() as db:
        await resolve_alert(db, alert_id, actor=await db.get(User, actor_id))

    async def crash(db, event):
        raise asyncio.CancelledError()

    runtime = DeliveryRuntime(
        sessions,
        "cg.first-draft",
        {
            "resume.first_draft.requested": crash,
        },
        retry=RetryPolicy(delays=()),
    )
    with pytest.raises(asyncio.CancelledError):
        await runtime.process(request.event_id)
    # Durable attempt 2 exists, but only attempt 1 was paused: interrupted
    # inference must never be treated as another harmless operational pause.
    assert await first.worker(source, client).process(request.event_id) == "dead"
    assert await first.worker(source, client).process(request.event_id) == "duplicate"
    async with sessions() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Resume)
                .where(Resume.application_id == request.application_id)
            )
            == 0
        )


async def test_acknowledged_incident_still_pauses_until_resolved(incidents):
    create, sessions, actor_id = incidents
    alert_id, _ = await create()
    async with sessions.begin() as db:
        alert = await db.get(Alert, alert_id)
        alert.status = "acknowledged"
        assert await generation_is_paused(db)
    async with sessions.begin() as db:
        assert await generation_is_paused(db)
        await resolve_alert(db, alert_id, actor=await db.get(User, actor_id))
    async with sessions() as db:
        assert not await generation_is_paused(db)
        assert (await db.get(Alert, alert_id)).detail["resolved_by"] == str(actor_id)
