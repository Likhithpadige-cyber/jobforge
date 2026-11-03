"""M4 crash, rollback and duplicate-delivery behavior against real stores."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.events import EventEnvelope, emit, record_event
from packages.core.events.monitoring import snapshot_jobs
from packages.core.events.runtime import (
    DeliveryRuntime,
    RetryPolicy,
    StreamConsumer,
    publish_event,
    stream_for,
)
from packages.core.models import Event, Job, MetricsSnapshot, ProcessedEvent
from packages.shared.config import get_settings
from tests.integration import test_idempotent_claim as claims
from workers.runtime import verify_role

pytestmark = [pytest.mark.integration, pytest.mark.safety]
committed_job = claims.committed_job


@pytest.fixture
async def bus(redis_available):
    if not redis_available:
        pytest.fail("M4 verification requires real Redis")
    client = Redis.from_url(str(get_settings().redis_url), decode_responses=True)
    prefix = f"test-m4-{uuid4().hex}"
    try:
        yield client, prefix
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}.*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


@pytest.fixture
async def delivery(engine, committed_job):
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as db:
        event = await emit(
            db,
            type="job.evaluation.requested",
            aggregate_type="job",
            aggregate_id=committed_job,
            correlation_id=committed_job,
            actor_type="system",
            payload={"job_id": str(committed_job)},
        )
    group = f"test.m4.{uuid4().hex}"
    return sessions, event.id, committed_job, group


async def handler(db, event):
    job = await db.get(Job, event.aggregate_id)
    job.title += " handled"
    await emit(
        db,
        type="job.evaluated",
        aggregate_type="job",
        aggregate_id=job.id,
        correlation_id=event.correlation_id,
        causation_id=event.id,
        actor_type="agent",
        payload={"job_id": str(job.id)},
    )
    return "done"


async def state(delivery):
    sessions, event_id, job_id, group = delivery
    async with sessions() as db:
        job = await db.get(Job, job_id)
        processed = await db.get(ProcessedEvent, (event_id, group))
        outputs = await db.scalar(
            select(func.count())
            .select_from(Event)
            .where(
                Event.causation_id == event_id,
                Event.type == "job.evaluated",
            )
        )
        failures = list(
            (
                await db.scalars(
                    select(Event)
                    .where(
                        Event.causation_id == event_id,
                        Event.type == "agent.run.failed",
                    )
                    .order_by(Event.occurred_at, Event.id)
                )
            ).all()
        )
        return job.title, processed.result if processed else None, outputs, failures


def runtime(delivery, action=handler, **kwargs):
    sessions, _, _, group = delivery
    return DeliveryRuntime(sessions, group, {"job.evaluation.requested": action}, **kwargs)


async def test_duplicate_delivery_changes_domain_and_emits_once(delivery):
    worker = runtime(delivery)
    assert await worker.process(delivery[1]) == "completed"
    assert await worker.process(delivery[1]) == "duplicate"
    title, result, outputs, failures = await state(delivery)
    assert title.count("handled") == 1
    assert (result, outputs, failures) == ("done", 1, [])


async def test_concurrent_delivery_never_runs_twice(delivery):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(db, event):
        started.set()
        await release.wait()
        return await handler(db, event)

    worker = runtime(delivery, slow)
    first = asyncio.create_task(worker.process(delivery[1]))
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        assert await worker.process(delivery[1]) == "busy"
    finally:
        release.set()
    assert await first == "completed"
    assert await worker.process(delivery[1]) == "duplicate"
    assert (await state(delivery))[2] == 1


async def test_failure_rolls_back_domain_outbox_and_claim_but_records_retry(delivery):
    async def fail(db, event):
        await handler(db, event)
        raise RuntimeError("secret fixture detail must not enter the event")

    worker = runtime(delivery, fail, retry=RetryPolicy(delays=(0, 0)))
    assert await worker.process(delivery[1]) == "retry"
    title, result, outputs, failures = await state(delivery)
    assert "handled" not in title and result is None and outputs == 0
    assert len(failures) == 1
    assert failures[0].payload["will_retry"] is True
    assert "secret fixture detail" not in str(failures[0].payload)
    restarted = runtime(delivery, retry=RetryPolicy(delays=(0, 0)))
    assert await restarted.process(delivery[1]) == "completed"
    assert (await state(delivery))[2] == 1


async def test_retry_backoff_survives_worker_recreation(delivery):
    async def fail(db, event):
        raise RuntimeError("fixture failure")

    assert await runtime(delivery, fail).process(delivery[1]) == "retry"
    assert await runtime(delivery, fail).process(delivery[1]) == "deferred"
    assert len((await state(delivery))[3]) == 1


async def test_poison_failure_is_durable_and_published_to_dlq(delivery, bus):
    redis, prefix = bus

    async def fail(db, event):
        raise RuntimeError("fixture failure")

    worker = runtime(delivery, fail, retry=RetryPolicy(delays=(0, 0, 0, 0)))
    assert [await worker.process(delivery[1]) for _ in range(3)] == ["retry", "retry", "dead"]
    assert await worker.process(delivery[1]) == "duplicate"
    title, result, outputs, failures = await state(delivery)
    assert "handled" not in title and result == "dead_letter" and outputs == 0
    assert [f.payload["attempt"] for f in failures] == [1, 2, 3]
    assert failures[-1].payload["will_retry"] is False
    async with delivery[0].begin() as db:
        await publish_event(redis, await db.get(Event, failures[-1].id), prefix)
    messages = await redis.xrange(f"{prefix}.dlq")
    assert messages[0][1] == {"event_id": str(failures[-1].id)}


async def test_changing_errors_still_exhausts_total_attempt_budget(delivery):
    exceptions = iter([ValueError, RuntimeError, LookupError, ArithmeticError, OSError])

    async def fail(db, event):
        raise next(exceptions)("fixture failure")

    worker = runtime(delivery, fail, retry=RetryPolicy(delays=(0, 0, 0, 0)))
    assert [await worker.process(delivery[1]) for _ in range(5)] == ["retry"] * 4 + ["dead"]
    assert len((await state(delivery))[3]) == 5


async def test_cancelled_handler_rolls_back_and_can_be_reclaimed(delivery):
    started = asyncio.Event()

    async def wait(db, event):
        await handler(db, event)
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    task = asyncio.create_task(runtime(delivery, wait).process(delivery[1]))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await state(delivery))[1:3] == (None, 0)
    restarted = runtime(delivery, retry=RetryPolicy(delays=(0, 0, 0, 0)))
    assert await restarted.process(delivery[1]) == "retry"
    assert (await state(delivery))[3][-1].payload["error_type"] == "WorkerInterruptedError"
    assert await restarted.process(delivery[1]) == "completed"


async def test_relay_crash_after_xadd_creates_safe_duplicate(delivery, bus):
    redis, prefix = bus
    sessions, event_id, _, _ = delivery
    with pytest.raises(RuntimeError):
        async with sessions.begin() as db:
            await publish_event(redis, await db.get(Event, event_id), prefix)
            raise RuntimeError("crash before publication commit")
    async with sessions.begin() as db:
        event = await db.get(Event, event_id)
        assert event.published is False
        await publish_event(redis, event, prefix)
    assert await redis.xlen(stream_for("job.evaluation.requested", prefix)) == 2
    consumer = StreamConsumer(redis, runtime(delivery), "test-consumer", prefix=prefix)
    await consumer.initialize(recover=False)
    await consumer.step()
    assert (await state(delivery))[2] == 1
    pending = await redis.xpending(consumer.streams[0], delivery[3])
    assert pending["pending"] == 0


async def test_postgres_reconciliation_recovers_lost_redis_stream(delivery, bus):
    redis, prefix = bus
    async with delivery[0].begin() as db:
        await publish_event(redis, await db.get(Event, delivery[1]), prefix)
    await redis.delete(stream_for("job.evaluation.requested", prefix))
    consumer = StreamConsumer(redis, runtime(delivery), "restarted", prefix=prefix)
    await consumer.initialize(recover=False)
    assert await consumer.runtime.reconcile(redis, prefix, event_ids=[delivery[1]]) == 1
    await consumer.step()
    assert (await state(delivery))[1:3] == ("done", 1)


async def test_abandoned_pending_entry_is_reclaimed(delivery, bus):
    redis, prefix = bus
    async with delivery[0].begin() as db:
        await publish_event(redis, await db.get(Event, delivery[1]), prefix)
    old = StreamConsumer(redis, runtime(delivery), "crashed", prefix=prefix)
    await old.initialize(recover=False)
    await redis.xreadgroup(delivery[3], "crashed", {old.streams[0]: ">"})
    new = StreamConsumer(redis, runtime(delivery), "replacement", prefix=prefix, reclaim_ms=0)
    await new.step()
    assert (await state(delivery))[2] == 1
    assert (await redis.xpending(old.streams[0], delivery[3]))["pending"] == 0


async def test_bad_reference_is_quarantined_before_ack(delivery, bus):
    redis, prefix = bus
    consumer = StreamConsumer(redis, runtime(delivery), "consumer", prefix=prefix)
    await consumer.initialize(recover=False)
    stream = consumer.streams[0]
    await redis.xadd(stream, {"event_id": "untrusted secret content"})
    await consumer.step()
    assert (await redis.xpending(stream, delivery[3]))["pending"] == 0
    async with delivery[0]() as db:
        failures = list(
            (
                await db.scalars(
                    select(Event).where(
                        Event.payload["consumer_group"].as_string() == delivery[3],
                        Event.type == "agent.run.failed",
                    )
                )
            ).all()
        )
    assert len(failures) == 1
    assert failures[0].payload["invalid_reference"] is True
    assert "untrusted secret content" not in str(failures[0].payload)


async def test_real_monitor_handler_persists_snapshot_and_outbox(session):
    event = await record_event(
        session,
        EventEnvelope(
            type="schedule.tick",
            aggregate_type="system",
            actor_type="system",
            correlation_id=uuid4(),
            payload={},
            occurred_at=datetime(2099, 1, 1, tzinfo=UTC),
        ),
    )
    result = await snapshot_jobs(session, event)
    assert result.startswith("snapshot:")
    assert (
        await session.scalar(
            select(func.count())
            .select_from(MetricsSnapshot)
            .where(
                MetricsSnapshot.bucket_start == event.occurred_at,
            )
        )
        == 1
    )
    assert (
        await session.scalar(
            select(func.count())
            .select_from(Event)
            .where(
                Event.causation_id == event.id,
                Event.type == "metrics.snapshot.created",
            )
        )
        == 1
    )


async def test_worker_role_check_rejects_owner_and_accepts_agent(engine, agent_engine):
    with pytest.raises(RuntimeError, match="unprivileged app_agent"):
        await verify_role(engine, "app_agent")
    await verify_role(agent_engine, "app_agent")


async def test_repeated_error_budget_counts_nonconsecutive_failures(delivery):
    exceptions = iter([ValueError, RuntimeError, ValueError, ValueError])

    async def fail(db, event):
        raise next(exceptions)("fixture failure")

    worker = runtime(delivery, fail, retry=RetryPolicy(delays=(0, 0, 0, 0)))
    assert [await worker.process(delivery[1]) for _ in range(4)] == ["retry"] * 3 + ["dead"]


async def test_agent_role_can_commit_handler_and_completion_claim(delivery, agent_engine):
    sessions = async_sessionmaker(agent_engine, expire_on_commit=False)
    worker = DeliveryRuntime(sessions, delivery[3], {"job.evaluation.requested": handler})
    assert await worker.process(delivery[1]) == "completed"
    assert (await state(delivery))[1:3] == ("done", 1)


async def test_redis_failure_never_marks_outbox_published(delivery, bus, monkeypatch):
    redis, prefix = bus

    async def fail(*args, **kwargs):
        raise ConnectionError("fixture offline")

    monkeypatch.setattr(redis, "xadd", fail)
    with pytest.raises(ConnectionError):
        async with delivery[0].begin() as db:
            await publish_event(redis, await db.get(Event, delivery[1]), prefix)
    async with delivery[0]() as db:
        event = await db.get(Event, delivery[1])
        assert event.published is False and event.published_at is None


async def test_unsupported_version_never_calls_handler(delivery):
    sessions, _, job_id, group = delivery
    async with sessions.begin() as db:
        event = await record_event(
            db,
            EventEnvelope(
                type="job.evaluation.requested",
                version=2,
                aggregate_type="job",
                aggregate_id=job_id,
                actor_type="system",
                correlation_id=job_id,
                payload={},
            ),
        )
    assert await runtime(delivery).process(event.id) == "dead"
    async with sessions() as db:
        claim = await db.get(ProcessedEvent, (event.id, group))
        assert claim.result == "dead_letter"
        assert "handled" not in (await db.get(Job, job_id)).title


async def test_agent_cannot_forge_human_event_for_consumer(delivery):
    sessions, _, job_id, group = delivery
    async with sessions.begin() as db:
        event = Event(
            id=uuid4(),
            type="approval.granted",
            version=1,
            aggregate_type="application",
            aggregate_id=uuid4(),
            correlation_id=job_id,
            actor_type="agent",
            payload={},
        )
        db.add(event)
    called = False

    async def forbidden(db, event):
        nonlocal called
        called = True
        return "bad"

    worker = DeliveryRuntime(sessions, group, {"approval.granted": forbidden})
    assert await worker.process(event.id) == "dead"
    assert called is False


async def test_scheduled_tick_and_monitoring_work_under_monitor_role(engine):
    from sqlalchemy.ext.asyncio import create_async_engine

    from packages.core.events.monitoring import schedule_snapshot
    from tests.integration.conftest import _url

    monitor_engine = create_async_engine(_url("app_monitor"))
    try:
        await verify_role(monitor_engine, "app_monitor")
        # Roll back the scheduler and snapshot together; do not create a live tick
        # in the user's outbox while verifying the permission boundary.
        async with async_sessionmaker(monitor_engine)() as db:
            transaction = await db.begin()
            await schedule_snapshot(db)
            await schedule_snapshot(db)
            event = await db.scalar(
                select(Event)
                .where(Event.type == "schedule.tick")
                .order_by(
                    Event.occurred_at.desc(),
                )
                .limit(1)
            )
            assert event is not None
            assert (await snapshot_jobs(db, event)).startswith("snapshot:")
            await transaction.rollback()
    finally:
        await monitor_engine.dispose()


async def test_repeated_worker_interruptions_reach_terminal_budget(delivery):
    for expected in ("retry", "retry", "dead"):
        started = asyncio.Event()

        async def interrupted(db, event, *, started=started):
            started.set()
            await asyncio.Event().wait()
            return "unreachable"

        policy = RetryPolicy(delays=(0, 0, 0, 0))
        worker = runtime(delivery, interrupted, retry=policy)
        task = asyncio.create_task(worker.process(delivery[1]))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await runtime(delivery, retry=policy).process(delivery[1]) == expected
    assert (await state(delivery))[1:3] == ("dead_letter", 0)
    assert len((await state(delivery))[3]) == 3


async def test_producer_rollback_removes_source_outbox_event(engine):
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    with pytest.raises(RuntimeError):
        async with sessions.begin() as db:
            event = await emit(
                db,
                type="schedule.tick",
                aggregate_type="system",
                actor_type="system",
                correlation_id=uuid4(),
                payload={},
            )
            event_id = event.id
            raise RuntimeError("producer transaction failed")
    async with sessions() as db:
        assert await db.get(Event, event_id) is None


async def test_relay_polling_batches_real_rows_without_touching_live_outbox(engine, bus):
    from sqlalchemy import text
    from sqlalchemy.schema import CreateSchema

    from packages.core.events.runtime import OutboxRelay

    redis, prefix = bus
    # A transaction-local schema makes the actual polling query testable without
    # publishing or changing any unrelated event in the user's database.
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            schema = f"m4_test_{uuid4().hex}"
            await conn.execute(CreateSchema(schema))
            await conn.execute(
                text("SELECT set_config('search_path', :schema, true)"), {"schema": schema}
            )
            await conn.run_sync(lambda sync: Event.__table__.create(sync))
            sessions = async_sessionmaker(
                conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            async with sessions.begin() as db:
                for _ in range(3):
                    await emit(
                        db,
                        type="job.normalized",
                        aggregate_type="job",
                        actor_type="system",
                        correlation_id=uuid4(),
                        payload={},
                    )
            relay = OutboxRelay(sessions, redis, prefix=prefix, batch_size=2)
            assert [await relay.step(), await relay.step(), await relay.step()] == [2, 1, 0]
            async with sessions() as db:
                assert (
                    await db.scalar(
                        select(func.count())
                        .select_from(Event)
                        .where(
                            Event.published.is_(True),
                            Event.published_at.is_not(None),
                        )
                    )
                    == 3
                )
            assert await redis.xlen(f"{prefix}.jobs") == 3
        finally:
            # Drops the temporary schema and rows by rolling back its creation.
            await transaction.rollback()


@pytest.mark.parametrize("version", [1, 2, 3])
async def test_rendered_versions_through_stream_dispatch_are_idempotent(delivery, bus, version):
    """Real PostgreSQL/Redis delivery; the handler only probes dispatch, not A2/A3."""
    from packages.core.events.rendered import ResumeRenderedV2

    sessions, _, application_id, group = delivery
    resume_id, artifact_id, approval_id = uuid4(), uuid4(), uuid4()
    payload = {
        "resume_id": str(resume_id),
        "resume_version": 1,
        "approval_id": str(approval_id),
        "template_version": "synthetic-v1",
        "artifacts": [{"artifact_id": str(artifact_id), "kind": "resume_pdf", "sha256": "a" * 64}],
    }
    if version != 1:
        payload = ResumeRenderedV2.model_validate(
            {
                "resume_id": str(resume_id),
                "application_id": str(application_id),
                "resume_version": 1,
                "approval_id": str(approval_id),
                "artifacts": [
                    {
                        "artifact_id": str(artifact_id),
                        "kind": "resume_pdf",
                        "template_version": "synthetic-v1",
                        "size_bytes": 100,
                        "sha256": "a" * 64,
                    }
                ],
            }
        ).model_dump(mode="json")
    async with sessions.begin() as db:
        event = await record_event(
            db,
            EventEnvelope(
                type="resume.rendered",
                version=version,
                aggregate_type="resume",
                aggregate_id=resume_id,
                correlation_id=application_id,
                actor_type="agent",
                payload=payload,
            ),
        )
    calls = []

    async def probe(db, delivered):
        calls.append((delivered.version, delivered.payload))
        return "version-probe"

    redis, prefix = bus
    worker = DeliveryRuntime(sessions, group, {"resume.rendered": probe})
    consumer = StreamConsumer(redis, worker, "version-probe", prefix=prefix)
    await consumer.initialize(recover=False)
    async with sessions.begin() as db:
        persisted = await db.get(Event, event.id)
        await publish_event(redis, persisted, prefix)
        await publish_event(redis, persisted, prefix)
    await consumer.step()
    assert calls == ([(version, payload)] if version in (1, 2) else [])
    assert await worker.process(event.id) == "duplicate"
    async with sessions() as db:
        claim = await db.get(ProcessedEvent, (event.id, group))
        assert claim.result == ("version-probe" if version in (1, 2) else "dead_letter")
        persisted = await db.get(Event, event.id)
        assert persisted.version == version and persisted.payload == payload
        failures = list(
            (
                await db.scalars(
                    select(Event).where(
                        Event.causation_id == event.id,
                        Event.type == "agent.run.failed",
                    )
                )
            ).all()
        )
        assert len(failures) == (0 if version in (1, 2) else 1)
        if failures:
            assert failures[0].payload["error_type"] == "InvalidEventError"
            assert failures[0].payload["will_retry"] is False
    pending = await redis.xpending(stream_for("resume.rendered", prefix), group)
    assert pending["pending"] == 0


async def test_unsubscribed_rendered_v2_stays_available_for_future_subscriber(delivery):
    sessions, _, application_id, group = delivery
    async with sessions.begin() as db:
        event = await record_event(
            db,
            EventEnvelope(
                type="resume.rendered",
                version=2,
                aggregate_type="resume",
                aggregate_id=uuid4(),
                correlation_id=application_id,
                actor_type="agent",
                payload={},
            ),
        )
    assert await runtime(delivery).process(event.id) == "unsubscribed"
    async with sessions() as db:
        assert await db.get(ProcessedEvent, (event.id, group)) is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.causation_id == event.id,
                    Event.type.in_(["worker.delivery.started", "agent.run.failed"]),
                )
            )
            == 0
        )
