"""M12 Phase 2: deterministic operational monitoring on the monitor tick.

Every rule is a pure function of data read here, and every alert it opens is
**non-pausing** (`auto_paused = false`): an operational alert can never pause
generation, start or retry a submission, or touch an application. The
generation auto-pause of M12 Phase 1 (`guardrail_alerts`) is unchanged.

Alert lifecycle, per rule:

* One unresolved alert per (kind, subject). A repeated evaluation of the same
  condition is a no-op; the alert id is a uuid5 of (kind, subject, anchor)
  inserted with ON CONFLICT DO NOTHING, so a resolved alert is never reopened
  by the evaluation that produced it.
* `AUTO_RESOLVABLE` kinds are resolved by the monitor only when recovery is
  positively observed (the same worker beats again, the application left the
  stalled status, a full failure window is back under the threshold, ...).
  Missing data is never treated as recovery.
* `HUMAN_ONLY` kinds (dead letters, ambiguous submissions) are only ever
  resolved by an owner through the alerts API. Resolution keeps the row.

Runs as `app_monitor`, which already holds INSERT/UPDATE on `alerts` and
`metrics_snapshots` and nothing on domain tables.
"""

from __future__ import annotations

import contextlib
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from redis.exceptions import RedisError
from sqlalchemy import Table, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.events.monitoring import snapshot_jobs
from packages.core.heartbeats import HEARTBEAT_RETENTION_SECONDS, ConsumerLag, Heartbeat
from packages.core.models import (
    Alert,
    Application,
    ApplicationStatus,
    ApplicationStatusHistory,
    Approval,
    ApprovalStatus,
    Event,
    MetricsSnapshot,
    ProcessedEvent,
    Submission,
    SubmissionResult,
)
from packages.shared.clock import utc_now
from packages.shared.config import Settings
from packages.shared.logging import get_logger

log = get_logger(__name__)

WORKER_MISSING = "worker_heartbeat_missing"
DEAD_LETTER = "dead_letter"
SUBMISSION_AMBIGUOUS = "submission_ambiguous"
SUBMISSION_FAILURE_SPIKE = "submission_failure_spike"
APPLICATION_STALLED = "application_stalled"
APPROVAL_BACKLOG = "approval_backlog"
CONSUMER_LAG = "consumer_lag"

AUTO_RESOLVABLE = frozenset(
    {WORKER_MISSING, SUBMISSION_FAILURE_SPIKE, APPLICATION_STALLED, APPROVAL_BACKLOG, CONSUMER_LAG}
)
HUMAN_ONLY = frozenset({DEAD_LETTER, SUBMISSION_AMBIGUOUS})
OPERATIONAL_KINDS = AUTO_RESOLVABLE | HUMAN_ONLY

SEVERITY = {
    WORKER_MISSING: "high",
    DEAD_LETTER: "high",
    SUBMISSION_AMBIGUOUS: "high",
    SUBMISSION_FAILURE_SPIKE: "high",
    APPLICATION_STALLED: "medium",
    CONSUMER_LAG: "medium",
    APPROVAL_BACKLOG: "low",
}
TITLE = {
    WORKER_MISSING: "Worker heartbeat missing",
    DEAD_LETTER: "Event delivery dead-lettered",
    SUBMISSION_AMBIGUOUS: "Ambiguous submission outcome requires review",
    SUBMISSION_FAILURE_SPIKE: "Submission failure rate above threshold",
    APPLICATION_STALLED: "Application stalled",
    APPROVAL_BACKLOG: "Approval backlog above threshold",
    CONSUMER_LAG: "Event consumer lag sustained above threshold",
}

#: States the system itself must move an application out of. Human-wait
#: states (awaiting approval, changes requested, ready to submit) are a
#: backlog, not a stall, and are covered by the approval-backlog rule.
STALL_STATUSES = (
    ApplicationStatus.CREATED,
    ApplicationStatus.RESUME_GENERATING,
    ApplicationStatus.QUESTIONS_PENDING,
    ApplicationStatus.SUBMITTING,
)

#: Serializes evaluations across concurrent ticks and monitor replicas.
_LOCK_KEY = 0x4A4F4D30  # stable, arbitrary


@dataclass(frozen=True)
class Thresholds:
    heartbeat_missing_seconds: int
    failure_window: int
    failure_rate: float
    stall_seconds: int
    approval_backlog: int
    consumer_lag: int
    consumer_lag_sustained_seconds: int
    stale_tick_seconds: int

    @classmethod
    def from_settings(cls, settings: Settings) -> Thresholds:
        return cls(
            heartbeat_missing_seconds=settings.monitor_heartbeat_missing_seconds,
            failure_window=settings.monitor_submission_failure_window,
            failure_rate=settings.monitor_submission_failure_rate,
            stall_seconds=settings.monitor_stall_seconds,
            approval_backlog=settings.monitor_approval_backlog,
            consumer_lag=settings.monitor_consumer_lag,
            consumer_lag_sustained_seconds=settings.monitor_consumer_lag_sustained_seconds,
            stale_tick_seconds=settings.monitor_stale_tick_seconds,
        )


class OperationsProbe(Protocol):
    async def heartbeats(self) -> list[Heartbeat]: ...

    async def forget(self, instances: list[str]) -> None: ...

    async def consumer_lags(self) -> list[ConsumerLag]: ...


# ----------------------------------------------------------- pure rules
def heartbeat_missing(beat: Heartbeat, now: datetime, threshold_seconds: int) -> bool:
    """Only a registered instance can be missing, and only strictly past the threshold."""
    return (now - beat.last_seen).total_seconds() > threshold_seconds


def failure_spike(failed: Sequence[bool], window: int, rate: float) -> bool | None:
    """`failed` holds the most recent attempts, newest first.

    None means "not evaluable": fewer than `window` attempts exist, which is
    neither a spike nor a verified recovery. Otherwise the most recent
    `window` attempts spike iff failures / window is strictly above `rate`.
    """
    if len(failed) < window:
        return None
    return sum(failed[:window]) / window > rate


def stalled(entered_at: datetime, now: datetime, threshold_seconds: int) -> bool:
    return (now - entered_at).total_seconds() > threshold_seconds


def backlog_exceeded(pending: int, threshold: int) -> bool:
    return pending > threshold


def lag_sustained(current: int | None, earlier: int | None, threshold: int) -> bool:
    """Both observations must exist and exceed the threshold."""
    if current is None or earlier is None:
        return False
    return current > threshold and earlier > threshold


def lag_recovered(current: int | None, threshold: int) -> bool:
    return current is not None and current <= threshold


def lag_key(lag: ConsumerLag) -> str:
    return f"{lag.stream}/{lag.group}"


# ----------------------------------------------------------- persistence
class _Opener:
    """Batched, idempotent alert opening for one kind.

    Loads the kind's alert identities once, so a repeated evaluation costs one
    read and at most one bulk INSERT however many entities it covers. A
    subject with an unresolved alert is skipped (one episode at a time); an id
    that already exists in any status is skipped, so the evaluation that
    produced a resolved alert can never reopen it.
    """

    def __init__(self, session: AsyncSession, kind: str) -> None:
        self.session, self.kind = session, kind
        self.known: set[UUID] = set()
        self.open: dict[str, UUID] = {}
        self.rows: list[dict[str, Any]] = []

    async def load(self) -> _Opener:
        rows = (
            await self.session.execute(
                select(Alert.id, Alert.status, Alert.detail["subject"].astext).where(
                    Alert.kind == self.kind
                )
            )
        ).all()
        self.known = {alert_id for alert_id, _, _ in rows}
        self.open = {
            str(subject): alert_id for alert_id, status, subject in rows if status != "resolved"
        }
        return self

    def want(
        self,
        subject: str,
        anchor: str,
        *,
        aggregate_type: str | None = None,
        aggregate_id: UUID | None = None,
    ) -> None:
        alert_id = uuid5(
            NAMESPACE_URL, f"jobforge:operational-alert:{self.kind}:{subject}:{anchor}"
        )
        if subject in self.open or alert_id in self.known:
            return
        self.known.add(alert_id)
        self.open[subject] = alert_id
        self.rows.append(
            {
                "id": alert_id,
                "kind": self.kind,
                "severity": SEVERITY[self.kind],
                "title": TITLE[self.kind],
                # Fixed, non-PII identifiers only; never payloads or free text.
                "detail": {"category": self.kind, "subject": subject},
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "status": "open",
                "auto_paused": False,  # never a generation pause
            }
        )

    async def flush(self) -> int:
        if not self.rows:
            return 0
        result = await self.session.execute(
            insert(cast(Table, Alert.__table__))
            .values(self.rows)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        self.rows = []
        return int(cast(Any, result).rowcount)


async def _auto_resolve(session: AsyncSession, alert_ids: Iterable[UUID], *, now: datetime) -> int:
    ids = list(alert_ids)
    if not ids:
        return 0
    result = await session.execute(
        update(Alert)
        .where(
            Alert.id.in_(ids),
            Alert.status != "resolved",
            # Defense in depth: never a pause, never a human-only incident.
            Alert.auto_paused.is_(False),
            Alert.kind.in_(AUTO_RESOLVABLE),
        )
        .values(
            status="resolved",
            resolved_at=now,
            detail=Alert.detail.op("||")(
                literal({"resolved_by": "monitor:recovered"}, type_=JSONB)
            ),
        )
        .execution_options(synchronize_session=False)
    )
    return int(cast(Any, result).rowcount)


@dataclass
class _Outcome:
    opened: int = 0
    resolved: int = 0


# ----------------------------------------------------------- rules (DB)
async def _workers(
    session: AsyncSession,
    beats: list[Heartbeat],
    now: datetime,
    thresholds: Thresholds,
    out: _Outcome,
) -> dict[str, dict[str, int]]:
    opener = await _Opener(session, WORKER_MISSING).load()
    already_open = dict(opener.open)
    summary: dict[str, Counter[str]] = {}
    recovered = []
    for beat in beats:
        subject = f"worker:{beat.instance}"
        missing = heartbeat_missing(beat, now, thresholds.heartbeat_missing_seconds)
        summary.setdefault(beat.kind, Counter())["missing" if missing else "live"] += 1
        if missing:
            opener.want(subject, beat.last_seen.isoformat())
        elif subject in already_open:
            # Recovery is the same instance beating again; a vanished one is not.
            recovered.append(already_open[subject])
    out.opened += await opener.flush()
    out.resolved += await _auto_resolve(session, recovered, now=now)
    return {kind: {"live": c["live"], "missing": c["missing"]} for kind, c in summary.items()}


async def _dead_letters(session: AsyncSession, out: _Outcome) -> int:
    rows = (
        await session.execute(
            select(ProcessedEvent.event_id, ProcessedEvent.consumer_group).where(
                ProcessedEvent.result == "dead_letter"
            )
        )
    ).all()
    opener = await _Opener(session, DEAD_LETTER).load()
    for event_id, group in rows:
        opener.want(f"{group}:{event_id}", "dead_letter")
    out.opened += await opener.flush()
    return len(rows)


async def _ambiguous_submissions(session: AsyncSession, out: _Outcome) -> None:
    # The structured M11 field, never a parse of failure text.
    rows = (
        await session.execute(
            select(Event.id, Event.payload["application_id"].astext).where(
                Event.type == "submission.failed",
                Event.payload["ambiguous"].as_boolean().is_(True),
            )
        )
    ).all()
    opener = await _Opener(session, SUBMISSION_AMBIGUOUS).load()
    for event_id, application_text in rows:
        try:
            application_id: UUID | None = UUID(str(application_text))
        except ValueError:
            application_id = None
        opener.want(
            f"submission_event:{event_id}",
            "ambiguous",
            aggregate_type="application" if application_id else None,
            aggregate_id=application_id,
        )
    out.opened += await opener.flush()


async def _submission_spike(
    session: AsyncSession, now: datetime, thresholds: Thresholds, out: _Outcome
) -> dict[str, int]:
    # Real send attempts only: preflight refusals never set started_at.
    rows = (
        await session.execute(
            select(Submission.id, Submission.result)
            .where(
                Submission.started_at.is_not(None),
                Submission.finished_at.is_not(None),
                Submission.result.in_([SubmissionResult.SUCCEEDED, SubmissionResult.FAILED]),
            )
            .order_by(Submission.finished_at.desc(), Submission.id.desc())
            .limit(thresholds.failure_window)
        )
    ).all()
    failed = [result is SubmissionResult.FAILED for _, result in rows]
    spike = failure_spike(failed, thresholds.failure_window, thresholds.failure_rate)
    opener = await _Opener(session, SUBMISSION_FAILURE_SPIKE).load()
    if spike:
        opener.want("submissions", str(rows[0][0]))
        out.opened += await opener.flush()
    elif spike is False:
        out.resolved += await _auto_resolve(session, opener.open.values(), now=now)
    return {"attempts": len(rows), "failures": sum(failed)}


async def _stalled_applications(
    session: AsyncSession, now: datetime, thresholds: Thresholds, out: _Outcome
) -> dict[str, int]:
    entered = func.coalesce(func.max(ApplicationStatusHistory.created_at), Application.created_at)
    rows = (
        await session.execute(
            select(Application.id, Application.status, entered)
            .outerjoin(
                ApplicationStatusHistory,
                (ApplicationStatusHistory.application_id == Application.id)
                & (ApplicationStatusHistory.to_status == Application.status),
            )
            .where(Application.status.in_(STALL_STATUSES))
            .group_by(Application.id, Application.status, Application.created_at)
        )
    ).all()
    opener = await _Opener(session, APPLICATION_STALLED).load()
    already_open = dict(opener.open)
    counts: Counter[str] = Counter()
    for app_id, status, entered_at in rows:
        if stalled(entered_at, now, thresholds.stall_seconds):
            counts[str(status)] += 1
            opener.want(
                f"application:{app_id}:{status}",
                entered_at.isoformat(),
                aggregate_type="application",
                aggregate_id=app_id,
            )
    out.opened += await opener.flush()
    # Recovery: the application is verifiably no longer in that status.
    stalled_in = {}
    for subject, alert_id in already_open.items():
        _, app_id_text, status_text = subject.split(":", 2)
        stalled_in[UUID(app_id_text)] = (status_text, alert_id)
    recovered = []
    if stalled_in:
        current = await session.execute(
            select(Application.id, Application.status).where(Application.id.in_(stalled_in))
        )
        recovered = [
            stalled_in[app_id][1]
            for app_id, status in current.all()
            if str(status) != stalled_in[app_id][0]
        ]
    out.resolved += await _auto_resolve(session, recovered, now=now)
    return dict(counts)


async def _approval_backlog(
    session: AsyncSession, now: datetime, bucket: datetime, thresholds: Thresholds, out: _Outcome
) -> int:
    pending = int(
        await session.scalar(
            select(func.count())
            .select_from(Approval)
            .where(Approval.status == ApprovalStatus.PENDING)
        )
        or 0
    )
    opener = await _Opener(session, APPROVAL_BACKLOG).load()
    if backlog_exceeded(pending, thresholds.approval_backlog):
        opener.want("approvals", bucket.isoformat())
        out.opened += await opener.flush()
    else:
        out.resolved += await _auto_resolve(session, opener.open.values(), now=now)
    return pending


async def _consumer_lag(
    session: AsyncSession,
    lags: list[ConsumerLag],
    now: datetime,
    bucket: datetime,
    thresholds: Thresholds,
    out: _Outcome,
) -> dict[str, int | None]:
    earlier_bucket = bucket - timedelta(seconds=thresholds.consumer_lag_sustained_seconds)
    earlier_metrics = await session.scalar(
        select(MetricsSnapshot.metrics).where(
            MetricsSnapshot.grain == "5min", MetricsSnapshot.bucket_start == earlier_bucket
        )
    )
    earlier = ((earlier_metrics or {}).get("operations") or {}).get("consumer_lag") or {}
    opener = await _Opener(session, CONSUMER_LAG).load()
    already_open = dict(opener.open)
    recovered = []
    for lag in lags:
        key = lag_key(lag)
        previous = earlier.get(key)
        if lag_sustained(
            lag.lag, previous if isinstance(previous, int) else None, thresholds.consumer_lag
        ):
            opener.want(key, bucket.isoformat())
        elif key in already_open and lag_recovered(lag.lag, thresholds.consumer_lag):
            recovered.append(already_open[key])
    out.opened += await opener.flush()
    out.resolved += await _auto_resolve(session, recovered, now=now)
    return {lag_key(lag): lag.lag for lag in lags}


# ----------------------------------------------------------- entry point
def _bucket(occurred_at: datetime) -> datetime:
    return occurred_at.replace(minute=(occurred_at.minute // 5) * 5, second=0, microsecond=0)


async def evaluate_operations(
    session: AsyncSession,
    event: Event,
    *,
    probe: OperationsProbe,
    thresholds: Thresholds,
    now: datetime | None = None,
) -> str:
    """Evaluate every rule once for this tick and record an operations snapshot."""
    now = now or utc_now()
    bucket = _bucket(event.occurred_at)
    if (now - event.occurred_at).total_seconds() > thresholds.stale_tick_seconds:
        # A tick replayed long after the fact must not judge the present.
        return "operations:skipped_stale_tick"
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})

    out = _Outcome()
    ops: dict[str, Any] = {"evaluated_at": now.isoformat()}
    try:
        beats = await probe.heartbeats()
        lags = await probe.consumer_lags()
    except RedisError as exc:
        # Unknown liveness is neither "missing" nor "recovered".
        log.warning("monitor.redis_unavailable", error_type=type(exc).__name__)
        beats, lags, ops["redis_available"] = [], [], False
    else:
        ops["redis_available"] = True
        ops["workers"] = await _workers(session, beats, now, thresholds, out)
        ops["consumer_lag"] = await _consumer_lag(session, lags, now, bucket, thresholds, out)
        expired = [
            b.instance
            for b in beats
            if (now - b.last_seen).total_seconds() > HEARTBEAT_RETENTION_SECONDS
        ]
        with contextlib.suppress(RedisError):
            await probe.forget(expired)
    ops["dead_letters"] = await _dead_letters(session, out)
    await _ambiguous_submissions(session, out)
    ops["submission_window"] = await _submission_spike(session, now, thresholds, out)
    ops["stalled_applications"] = await _stalled_applications(session, now, thresholds, out)
    ops["pending_approvals"] = await _approval_backlog(session, now, bucket, thresholds, out)
    open_rows = (
        await session.execute(
            select(Alert.kind, func.count())
            .where(Alert.status != "resolved", Alert.kind.in_(OPERATIONAL_KINDS))
            .group_by(Alert.kind)
        )
    ).all()
    ops["open_alerts"] = {kind: int(count) for kind, count in open_rows}
    # snapshot_jobs upserted this bucket's row earlier in the same transaction.
    await session.execute(
        update(MetricsSnapshot)
        .where(MetricsSnapshot.grain == "5min", MetricsSnapshot.bucket_start == bucket)
        .values(metrics=MetricsSnapshot.metrics.op("||")(literal({"operations": ops}, type_=JSONB)))
        .execution_options(synchronize_session=False)
    )
    return f"operations:opened={out.opened},resolved={out.resolved}"


Handler = Callable[[AsyncSession, Event], Awaitable[str]]


def monitor_tick_handler(probe: OperationsProbe, thresholds: Thresholds) -> Handler:
    """`schedule.tick` for cg.monitor: the existing job snapshot, then operations."""

    async def handle(session: AsyncSession, event: Event) -> str:
        snapshot = await snapshot_jobs(session, event)
        operations = await evaluate_operations(session, event, probe=probe, thresholds=thresholds)
        return f"{snapshot};{operations}"

    return handle
