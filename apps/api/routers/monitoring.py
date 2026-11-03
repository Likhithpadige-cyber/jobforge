"""M12 Phase 2 read-only operational monitoring for the owner.

Typed projections of persisted state only: alert counts, the generation-pause
flag and the monitor's 5-minute snapshots. Nothing here writes, and no
response carries alert detail, worker instance names or entity identifiers.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from apps.api.deps import DbSession, authenticated_api, require_owner
from packages.core.generation_safety import generation_is_paused
from packages.core.models import Alert, MetricsSnapshot

router = APIRouter(
    prefix="/monitoring",
    tags=["monitoring"],
    dependencies=[
        Depends(authenticated_api),
        Depends(require_owner),
    ],
)


class _Read(BaseModel):
    model_config = ConfigDict(extra="ignore")


class WorkerCounts(_Read):
    live: int = 0
    missing: int = 0


class SubmissionWindow(_Read):
    attempts: int = 0
    failures: int = 0


class OperationsRead(_Read):
    evaluated_at: datetime | None = None
    redis_available: bool | None = None
    workers: dict[str, WorkerCounts] = Field(default_factory=dict)
    consumer_lag: dict[str, int | None] = Field(default_factory=dict)
    dead_letters: int = 0
    submission_window: SubmissionWindow = Field(default_factory=SubmissionWindow)
    stalled_applications: dict[str, int] = Field(default_factory=dict)
    pending_approvals: int = 0
    open_alerts: dict[str, int] = Field(default_factory=dict)


class SnapshotRead(_Read):
    bucket_start: datetime
    grain: str
    job_counts: dict[str, int] = Field(default_factory=dict)
    operations: OperationsRead | None = None


class AlertCounts(_Read):
    total: int
    by_severity: dict[str, int]
    by_kind: dict[str, int]


class MonitoringSummary(_Read):
    generation_paused: bool
    open_alerts: AlertCounts
    latest_snapshot: SnapshotRead | None


def _snapshot(row: MetricsSnapshot) -> SnapshotRead:
    metrics: dict[str, Any] = row.metrics or {}
    return SnapshotRead(
        bucket_start=row.bucket_start,
        grain=row.grain,
        job_counts=metrics.get("job_counts") or {},
        operations=metrics.get("operations"),
    )


@router.get("/summary", response_model=MonitoringSummary)
async def summary(session: DbSession) -> MonitoringSummary:
    unresolved = Alert.status != "resolved"
    by_severity = (
        await session.execute(
            select(Alert.severity, func.count()).where(unresolved).group_by(Alert.severity)
        )
    ).all()
    by_kind = (
        await session.execute(
            select(Alert.kind, func.count()).where(unresolved).group_by(Alert.kind)
        )
    ).all()
    latest = await session.scalar(
        select(MetricsSnapshot)
        .where(MetricsSnapshot.grain == "5min")
        .order_by(MetricsSnapshot.bucket_start.desc())
        .limit(1)
    )
    return MonitoringSummary(
        generation_paused=await generation_is_paused(session),
        open_alerts=AlertCounts(
            total=sum(int(count) for _, count in by_severity),
            by_severity={str(severity): int(count) for severity, count in by_severity},
            by_kind={str(kind): int(count) for kind, count in by_kind},
        ),
        latest_snapshot=_snapshot(latest) if latest is not None else None,
    )


@router.get("/snapshots", response_model=list[SnapshotRead])
async def snapshots(
    session: DbSession,
    limit: int = Query(default=12, ge=1, le=288),
) -> list[SnapshotRead]:
    rows = await session.scalars(
        select(MetricsSnapshot)
        .where(MetricsSnapshot.grain == "5min")
        .order_by(MetricsSnapshot.bucket_start.desc())
        .limit(limit)
    )
    return [_snapshot(row) for row in rows]
