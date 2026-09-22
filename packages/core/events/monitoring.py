"""The first M4 consumer: persist non-PII job counts on a schedule tick."""

from __future__ import annotations

from typing import cast
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import Table, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.events.envelope import EventEnvelope
from packages.core.events.outbox import emit
from packages.core.models import Event, Job, MetricsSnapshot
from packages.shared.clock import utc_now
from packages.shared.ids import uuid7


async def snapshot_jobs(session: AsyncSession, event: Event) -> str:
    bucket = event.occurred_at.replace(
        minute=(event.occurred_at.minute // 5) * 5, second=0, microsecond=0,
    )
    counts = (await session.execute(select(Job.status, func.count()).group_by(Job.status))).all()
    metrics = {"job_counts": {str(status): count for status, count in counts}}
    # Existing table metadata is a concrete Table; keep the Core INSERT contract.
    stmt = pg_insert(cast(Table, MetricsSnapshot.__table__)).values(
        id=uuid7(), grain="5min", bucket_start=bucket, metrics=metrics,
    ).on_conflict_do_update(
        index_elements=["grain", "bucket_start"], set_={"metrics": metrics},
    ).returning(MetricsSnapshot.id)
    snapshot_id = (await session.execute(stmt)).scalar_one()
    await emit(
        session, type="metrics.snapshot.created", aggregate_type="system",
        actor_type="agent", correlation_id=event.correlation_id, causation_id=event.id,
        payload={"snapshot_id": str(snapshot_id), "grain": "5min",
                 "bucket_start": bucket.isoformat()},
    )
    return f"snapshot:{snapshot_id}"


async def schedule_snapshot(session: AsyncSession) -> None:
    """One durable tick per five-minute bucket, even with several schedulers."""
    now = utc_now()
    bucket = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    tick_id = uuid5(NAMESPACE_URL, f"jobforge:monitor-snapshot:{bucket.isoformat()}")
    envelope = EventEnvelope(
        id=tick_id, type="schedule.tick", aggregate_type="system", actor_type="system",
        correlation_id=tick_id, occurred_at=bucket,
        payload={"tick_id": str(tick_id), "schedule_name": "monitor-snapshot",
                 "scheduled_for": bucket.isoformat()},
    )
    await session.execute(
        pg_insert(cast(Table, Event.__table__)).values(**envelope.as_row())
        .on_conflict_do_nothing(index_elements=["id"])
    )
