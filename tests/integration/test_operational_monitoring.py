"""M12 Phase 2 operational rules against real PostgreSQL.

Every test runs inside the rolled-back `session` fixture and evaluates a tick
in 2099, so shared-database residue can never make an assertion flaky: rows
the test adds are the newest, and assertions are scoped to the test's own
subjects. Redis-backed observations come from a stub probe.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from packages.core.events.guardrail_alerts import open_guardrail_alert
from packages.core.events.operational_monitoring import (
    APPLICATION_STALLED,
    APPROVAL_BACKLOG,
    CONSUMER_LAG,
    DEAD_LETTER,
    SUBMISSION_AMBIGUOUS,
    SUBMISSION_FAILURE_SPIKE,
    WORKER_MISSING,
    Thresholds,
    _auto_resolve,
    evaluate_operations,
    monitor_tick_handler,
)
from packages.core.generation_safety import generation_is_paused, resolve_alert
from packages.core.heartbeats import ConsumerLag, Heartbeat
from packages.core.models import Alert, Event, MetricsSnapshot, ProcessedEvent, Submission, User
from packages.shared.ids import uuid7
from tests.integration.conftest import _url

pytestmark = [pytest.mark.integration, pytest.mark.safety]

TICK = datetime(2099, 1, 1, 12, 0, tzinfo=UTC)
LAX = Thresholds(
    heartbeat_missing_seconds=90,
    failure_window=10,
    failure_rate=0.30,
    stall_seconds=10**10,  # rules under test opt in explicitly
    approval_backlog=1_000_000,
    consumer_lag=500,
    consumer_lag_sustained_seconds=600,
    stale_tick_seconds=900,
)


class StubProbe:
    def __init__(self, beats=(), lags=(), fail=False):
        self.beats, self.lags, self.fail, self.forgotten = list(beats), list(lags), fail, []

    async def heartbeats(self):
        if self.fail:
            raise RedisConnectionError("down")
        return self.beats

    async def forget(self, instances):
        self.forgotten.extend(instances)

    async def consumer_lags(self):
        return self.lags


def _tick(at=TICK):
    tick_id = uuid7()
    return Event(
        id=tick_id,
        type="schedule.tick",
        version=1,
        aggregate_type="system",
        actor_type="system",
        correlation_id=tick_id,
        occurred_at=at,
        payload={
            "tick_id": str(tick_id),
            "schedule_name": "monitor-snapshot",
            "scheduled_for": at.isoformat(),
        },
    )


async def _evaluate(session, *, at=TICK, probe=None, thresholds=LAX):
    return await evaluate_operations(
        session, _tick(at), probe=probe or StubProbe(), thresholds=thresholds, now=at
    )


async def _alerts(session, kind, subject):
    rows = await session.scalars(
        select(Alert)
        .where(Alert.kind == kind, Alert.detail["subject"].astext == subject)
        .order_by(Alert.created_at)
        .execution_options(populate_existing=True)
    )
    return list(rows)


# ------------------------------------------------------------- heartbeats
async def test_registered_worker_is_reported_only_after_missing_and_recovers(session):
    instance = f"host-{uuid7()}"
    subject = f"worker:{instance}"
    fresh = Heartbeat(instance, "submission", TICK - timedelta(seconds=90))
    await _evaluate(session, probe=StubProbe([fresh]))
    assert await _alerts(session, WORKER_MISSING, subject) == []

    stale = Heartbeat(instance, "submission", TICK - timedelta(seconds=91))
    await _evaluate(session, probe=StubProbe([stale]))
    await _evaluate(session, probe=StubProbe([stale]))  # idempotent
    later = TICK + timedelta(minutes=5)
    await _evaluate(session, at=later, probe=StubProbe([stale]))
    [alert] = await _alerts(session, WORKER_MISSING, subject)
    assert (alert.status, alert.auto_paused, alert.severity) == ("open", False, "high")

    # Recovery is the same instance beating again, and only that.
    await _evaluate(session, at=later, probe=StubProbe([]))
    assert (await _alerts(session, WORKER_MISSING, subject))[0].status == "open"
    back = Heartbeat(instance, "submission", later - timedelta(seconds=5))
    await _evaluate(session, at=later, probe=StubProbe([back]))
    [alert] = await _alerts(session, WORKER_MISSING, subject)
    assert alert.status == "resolved" and alert.detail["resolved_by"] == "monitor:recovered"
    assert alert.acknowledged_by is None


async def test_unregistered_worker_is_never_reported(session):
    before = await session.scalar(
        select(func.count()).select_from(Alert).where(Alert.kind == WORKER_MISSING)
    )
    await _evaluate(session, probe=StubProbe([]))
    after = await session.scalar(
        select(func.count()).select_from(Alert).where(Alert.kind == WORKER_MISSING)
    )
    assert after == before


async def test_redis_outage_is_neither_missing_nor_recovered(session):
    instance = f"host-{uuid7()}"
    stale = Heartbeat(instance, "relay", TICK - timedelta(hours=1))
    await _evaluate(session, probe=StubProbe([stale]))
    result = await _evaluate(
        session, at=TICK + timedelta(minutes=5), probe=StubProbe([stale], fail=True)
    )
    assert result.startswith("operations:")
    [alert] = await _alerts(session, WORKER_MISSING, f"worker:{instance}")
    assert alert.status == "open"


async def test_expired_registrations_are_forgotten_after_alerting(session):
    instance = f"host-{uuid7()}"
    ancient = Heartbeat(instance, "relay", TICK - timedelta(days=8))
    probe = StubProbe([ancient])
    await _evaluate(session, probe=probe)
    assert probe.forgotten == [instance]
    assert len(await _alerts(session, WORKER_MISSING, f"worker:{instance}")) == 1


# ------------------------------------------------------- human-only kinds
async def test_dead_letter_alert_is_human_only_and_never_reopened(session, demo_user):
    event_id = uuid7()
    session.add(ProcessedEvent(event_id=event_id, consumer_group="cg.test", result="dead_letter"))
    await session.flush()
    subject = f"cg.test:{event_id}"
    await _evaluate(session)
    await _evaluate(session, at=TICK + timedelta(minutes=5))
    [alert] = await _alerts(session, DEAD_LETTER, subject)
    assert (alert.status, alert.severity, alert.auto_paused) == ("open", "high", False)

    # The monitor cannot resolve it, even when asked directly.
    assert await _auto_resolve(session, [alert.id], now=TICK) == 0
    owner = await session.get(User, demo_user)
    owner.role = "owner"
    await resolve_alert(session, alert.id, actor=owner)
    await _evaluate(session, at=TICK + timedelta(minutes=10))
    [alert] = await _alerts(session, DEAD_LETTER, subject)
    assert alert.status == "resolved" and alert.detail["resolved_by"] == str(demo_user)


async def test_ambiguous_submission_needs_a_human(session, demo_application):
    failed = Event(
        id=uuid7(),
        type="submission.failed",
        version=1,
        aggregate_type="submission",
        aggregate_id=uuid7(),
        actor_type="system",
        correlation_id=demo_application["id"],
        payload={
            "submission_id": str(uuid7()),
            "application_id": str(demo_application["id"]),
            "attempt": 1,
            "failure_reason": "ambiguous: disconnected during DATA",
            "will_retry": False,
            "ambiguous": True,
        },
    )
    clear = Event(
        id=uuid7(),
        type="submission.failed",
        version=1,
        aggregate_type="submission",
        aggregate_id=uuid7(),
        actor_type="system",
        correlation_id=demo_application["id"],
        payload={"failure_reason": "ambiguous: lookalike text only", "ambiguous": False},
    )
    session.add_all([failed, clear])
    await session.flush()
    await _evaluate(session)
    [alert] = await _alerts(session, SUBMISSION_AMBIGUOUS, f"submission_event:{failed.id}")
    assert alert.aggregate_type == "application"
    assert alert.aggregate_id == demo_application["id"]
    assert "disconnected" not in str(alert.detail) and alert.status == "open"
    # The structured flag, not the failure text, is the authority.
    assert await _alerts(session, SUBMISSION_AMBIGUOUS, f"submission_event:{clear.id}") == []
    assert await _auto_resolve(session, [alert.id], now=TICK) == 0


# -------------------------------------------------- submission failure spike
async def _attempts(session, application_id, outcomes, start):
    for offset, failed in enumerate(outcomes):
        session.add(
            Submission(
                application_id=application_id,
                attempt=start + offset,
                method="email",
                target_domain="example.com",
                result="failed" if failed else "succeeded",
                started_at=TICK + timedelta(seconds=start + offset),
                finished_at=TICK + timedelta(seconds=start + offset, milliseconds=500),
            )
        )
    await session.flush()


async def test_failure_spike_needs_ten_attempts_and_four_failures(session, demo_application):
    app_id = demo_application["id"]
    # Oldest first: S F F F S S S S S  (9 attempts, 3 failures)
    await _attempts(session, app_id, [False, True, True, True] + [False] * 5, start=1)
    later = TICK + timedelta(hours=1)
    await _evaluate(session, at=later)
    assert await _alerts(session, SUBMISSION_FAILURE_SPIKE, "submissions") == []
    await _attempts(session, app_id, [False], start=10)  # 10 attempts, 3 failures
    await _evaluate(session, at=later)
    assert await _alerts(session, SUBMISSION_FAILURE_SPIKE, "submissions") == []
    await _attempts(session, app_id, [True], start=11)  # newest 10 = 2..11: 4 failures
    await _evaluate(session, at=later)
    await _evaluate(session, at=later)
    [alert] = await _alerts(session, SUBMISSION_FAILURE_SPIKE, "submissions")
    assert alert.status == "open" and not alert.auto_paused

    # Preflight refusals (no started_at) are not send attempts.
    session.add(
        Submission(
            application_id=app_id,
            attempt=50,
            method="email",
            target_domain="example.com",
            result="failed",
            started_at=None,
            finished_at=later,
        )
    )
    await _attempts(session, app_id, [False] * 7, start=12)  # newest 10 = 9..18: 1 failure
    await _evaluate(session, at=later)
    [alert] = await _alerts(session, SUBMISSION_FAILURE_SPIKE, "submissions")
    assert alert.status == "resolved"


# ------------------------------------------------------ stalled application
async def test_stalled_application_opens_once_and_resolves_on_status_change(
    session, demo_application
):
    app_id = demo_application["id"]
    subject = f"application:{app_id}:created"
    # Created "now"; 24h + 1s later in tick time it is stalled.
    created = await session.scalar(
        text("SELECT created_at FROM applications WHERE id = :id"), {"id": app_id}
    )
    at = created + timedelta(seconds=86_401)
    day = replace(LAX, stall_seconds=86_400)
    await _evaluate(session, at=created + timedelta(seconds=86_400), thresholds=day)
    assert await _alerts(session, APPLICATION_STALLED, subject) == []
    await _evaluate(session, at=at, thresholds=day)
    await _evaluate(session, at=at + timedelta(minutes=5), thresholds=day)
    [alert] = await _alerts(session, APPLICATION_STALLED, subject)
    assert (alert.aggregate_id, alert.severity, alert.status) == (app_id, "medium", "open")

    await session.execute(
        text("UPDATE applications SET status = 'resume_generating' WHERE id = :id"),
        {"id": app_id},
    )
    await _evaluate(session, at=at + timedelta(minutes=10), thresholds=day)
    [alert] = await _alerts(session, APPLICATION_STALLED, subject)
    assert alert.status == "resolved"


async def test_human_wait_states_are_not_stalls(session, demo_application):
    app_id = demo_application["id"]
    await session.execute(
        text("UPDATE applications SET status = 'changes_requested' WHERE id = :id"),
        {"id": app_id},
    )
    await _evaluate(session, thresholds=replace(LAX, stall_seconds=60))
    for status in ("changes_requested", "created"):
        assert await _alerts(session, APPLICATION_STALLED, f"application:{app_id}:{status}") == []


# --------------------------------------------------------- approval backlog
async def test_approval_backlog_threshold_and_recovery(session, pending_approval):
    pending = await session.scalar(text("SELECT count(*) FROM approvals WHERE status = 'pending'"))
    at_threshold = replace(LAX, approval_backlog=pending)
    await _evaluate(session, thresholds=at_threshold)
    assert [
        a for a in await _alerts(session, APPROVAL_BACKLOG, "approvals") if a.status != "resolved"
    ] == []
    above = replace(LAX, approval_backlog=pending - 1)
    await _evaluate(session, thresholds=above)
    await _evaluate(session, at=TICK + timedelta(minutes=5), thresholds=above)
    opened = [
        a for a in await _alerts(session, APPROVAL_BACKLOG, "approvals") if a.status != "resolved"
    ]
    assert len(opened) == 1 and opened[0].severity == "low"
    await _evaluate(session, at=TICK + timedelta(minutes=10), thresholds=at_threshold)
    assert [
        a for a in await _alerts(session, APPROVAL_BACKLOG, "approvals") if a.status != "resolved"
    ] == []


# ------------------------------------------------------------ consumer lag
async def test_consumer_lag_must_be_sustained_and_recovers(session):
    group = f"cg.test-{uuid7()}"
    key = f"evt.jobs/{group}"
    earlier = TICK - timedelta(seconds=600)
    session.add(
        MetricsSnapshot(
            grain="5min",
            bucket_start=earlier,
            metrics={"operations": {"consumer_lag": {key: 400}}},
        )
    )
    await session.flush()
    high = StubProbe(lags=[ConsumerLag("evt.jobs", group, 900)])
    await _evaluate(session, probe=high)
    assert await _alerts(session, CONSUMER_LAG, key) == []  # not sustained

    await session.execute(
        text(
            "UPDATE metrics_snapshots SET metrics = "
            "jsonb_build_object('operations', jsonb_build_object('consumer_lag', "
            "jsonb_build_object(CAST(:key AS text), 600))) "
            "WHERE grain = '5min' AND bucket_start = :at"
        ),
        {"key": key, "at": earlier},
    )
    await _evaluate(session, probe=high)
    [alert] = await _alerts(session, CONSUMER_LAG, key)
    assert alert.status == "open"
    unknown = StubProbe(lags=[ConsumerLag("evt.jobs", group, None)])
    await _evaluate(session, at=TICK + timedelta(minutes=5), probe=unknown)
    assert (await _alerts(session, CONSUMER_LAG, key))[0].status == "open"
    ok = StubProbe(lags=[ConsumerLag("evt.jobs", group, 500)])
    await _evaluate(session, at=TICK + timedelta(minutes=5), probe=ok)
    assert (await _alerts(session, CONSUMER_LAG, key))[0].status == "resolved"


# --------------------------------------------------------------- safeguards
async def test_operational_alerts_never_pause_generation(session, demo_application):
    assert not await generation_is_paused(session)
    instance = f"host-{uuid7()}"
    session.add(ProcessedEvent(event_id=uuid7(), consumer_group="cg.test", result="dead_letter"))
    await session.flush()
    probe = StubProbe(
        [Heartbeat(instance, "relay", TICK - timedelta(hours=2))],
    )
    await _evaluate(session, probe=probe, thresholds=replace(LAX, approval_backlog=-1))
    opened = (
        await session.scalars(
            select(Alert).where(Alert.created_at.is_not(None), Alert.status != "resolved")
        )
    ).all()
    assert {a.kind for a in opened} >= {WORKER_MISSING, DEAD_LETTER, APPROVAL_BACKLOG}
    assert not any(a.auto_paused for a in opened if a.kind != "fabrication")
    assert not await generation_is_paused(session)


async def test_monitor_never_resolves_a_generation_pause(session):
    event = Event(
        id=uuid7(),
        type="guardrail.violation.detected",
        version=1,
        aggregate_type="system",
        actor_type="agent",
        correlation_id=uuid7(),
        payload={"kind": "fabrication", "severity": "critical"},
    )
    session.add(event)
    await session.flush()
    await open_guardrail_alert(session, event)
    assert await generation_is_paused(session)
    pause = await session.scalar(
        select(Alert).where(Alert.detail["source_event_id"].astext == str(event.id))
    )
    # Even mislabelled as an auto-resolvable kind, an auto_paused row is untouchable.
    await session.execute(
        text("UPDATE alerts SET kind = :kind WHERE id = :id"),
        {"kind": APPROVAL_BACKLOG, "id": pause.id},
    )
    assert await _auto_resolve(session, [pause.id], now=TICK) == 0
    await _evaluate(session, thresholds=replace(LAX, approval_backlog=1_000_000_000))
    assert await generation_is_paused(session)


async def test_evaluation_leaves_domain_rows_untouched(session, demo_application):
    def state():
        return session.execute(
            text(
                "SELECT (SELECT md5(string_agg(a::text, ',' ORDER BY a.id)) FROM applications a),"
                " (SELECT md5(coalesce(string_agg(s::text, ',' ORDER BY s.id), ''))"
                "  FROM submissions s),"
                " (SELECT md5(coalesce(string_agg(p::text, ',' ORDER BY p.id), ''))"
                "  FROM approvals p)"
            )
        )

    before = (await state()).one()
    await _evaluate(session, thresholds=replace(LAX, approval_backlog=-1))
    assert (await state()).one() == before


async def test_stale_tick_does_not_judge_the_present(session):
    instance = f"host-{uuid7()}"
    probe = StubProbe([Heartbeat(instance, "relay", TICK - timedelta(hours=2))])
    result = await evaluate_operations(
        session, _tick(TICK), probe=probe, thresholds=LAX, now=TICK + timedelta(seconds=901)
    )
    assert result == "operations:skipped_stale_tick"
    assert await _alerts(session, WORKER_MISSING, f"worker:{instance}") == []


async def test_tick_handler_records_operations_in_the_snapshot(session):
    tick = _tick(datetime.now(UTC))
    session.add(tick)
    await session.flush()
    handler = monitor_tick_handler(StubProbe(), LAX)
    result = await handler(session, tick)
    assert result.startswith("snapshot:") and ";operations:" in result
    bucket = tick.occurred_at.replace(
        minute=(tick.occurred_at.minute // 5) * 5, second=0, microsecond=0
    )
    metrics = await session.scalar(
        select(MetricsSnapshot.metrics).where(
            MetricsSnapshot.grain == "5min", MetricsSnapshot.bucket_start == bucket
        )
    )
    assert "job_counts" in metrics
    operations = metrics["operations"]
    assert operations["redis_available"] is True
    assert {"dead_letters", "submission_window", "pending_approvals", "open_alerts"} <= set(
        operations
    )


async def test_evaluation_runs_with_existing_app_monitor_grants_only(engine):
    monitor = create_async_engine(_url("app_monitor"))
    sessions = async_sessionmaker(monitor, expire_on_commit=False)
    instance = f"host-{uuid7()}"
    try:
        async with sessions() as db, db.begin():
            probe = StubProbe([Heartbeat(instance, "relay", TICK - timedelta(hours=2))])
            result = await evaluate_operations(
                db,
                _tick(TICK),
                probe=probe,
                thresholds=replace(LAX, approval_backlog=-1),
                now=TICK,
            )
            assert result.startswith("operations:opened=")
            assert len(await _alerts(db, WORKER_MISSING, f"worker:{instance}")) == 1
            for table in ("applications", "submissions", "approvals", "resumes"):
                assert not await db.scalar(
                    text("SELECT has_table_privilege(current_user, :t, 'INSERT, UPDATE')"),
                    {"t": table},
                )
            await db.rollback()
    finally:
        await monitor.dispose()


# --------------------------------------------------------------------- API
async def test_monitoring_api_is_owner_only_and_typed(api_sessions, app_client):
    owner = await api_sessions("owner")
    reviewer = await api_sessions("reviewer")
    summary = await owner.get("/api/v1/monitoring/summary")
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert set(body) == {"generation_paused", "open_alerts", "latest_snapshot"}
    assert set(body["open_alerts"]) == {"total", "by_severity", "by_kind"}
    snapshots = await owner.get("/api/v1/monitoring/snapshots", params={"limit": 3})
    assert snapshots.status_code == 200 and len(snapshots.json()) <= 3
    for row in snapshots.json():
        assert set(row) == {"bucket_start", "grain", "job_counts", "operations"}
    assert (await owner.get("/api/v1/monitoring/snapshots", params={"limit": 0})).status_code == 422
    for path in ("/api/v1/monitoring/summary", "/api/v1/monitoring/snapshots"):
        assert (await reviewer.get(path)).status_code == 403
        assert (await app_client.get(path)).status_code == 401
    assert "host-" not in summary.text and "subject" not in summary.text
