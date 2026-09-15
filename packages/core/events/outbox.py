"""Transactional outbox writer (ARCHITECTURE.md §3.4, EVENTS.md §4.3).

An event is written to the `events` table **in the caller's transaction**,
alongside the state change it describes. If the state change rolls back so
does the event, and if it commits the event is durably recorded even when
Redis is down. The relay publishes unpublished rows afterwards.

This module deliberately does not publish anything. Writing and publishing
are separate steps; conflating them is how "state changed but event lost"
bugs happen - which in this system means a silently stuck job.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.events.envelope import (
    ActorType,
    AggregateType,
    EventEnvelope,
)
from packages.core.models import Event, ProcessedEvent
from packages.shared.logging import get_logger

log = get_logger(__name__)


async def record_event(session: AsyncSession, envelope: EventEnvelope) -> Event:
    """Write one event into the caller's open transaction.

    No commit and no flush-and-forget: the caller owns the transaction
    boundary, which is what makes the outbox guarantee hold.
    """
    event = Event(**envelope.as_row())
    session.add(event)
    await session.flush()
    log.info(
        "event.recorded",
        event_type=envelope.type,
        aggregate_id=str(envelope.aggregate_id) if envelope.aggregate_id else None,
        correlation_id=str(envelope.correlation_id),
    )
    return event


async def emit(
    session: AsyncSession,
    *,
    type: str,
    aggregate_type: AggregateType,
    payload: dict[str, Any],
    correlation_id: UUID,
    actor_type: ActorType = "agent",
    actor_id: str | None = None,
    aggregate_id: UUID | None = None,
    causation_id: UUID | None = None,
    trace_id: str | None = None,
) -> Event:
    """Convenience wrapper: build the envelope and record it."""
    envelope = EventEnvelope(
        type=type,
        aggregate_type=aggregate_type,
        payload=payload,
        correlation_id=correlation_id,
        actor_type=actor_type,
        actor_id=actor_id,
        aggregate_id=aggregate_id,
        causation_id=causation_id,
        trace_id=trace_id,
    )
    return await record_event(session, envelope)


async def already_processed(
    session: AsyncSession, event_id: UUID, consumer_group: str
) -> bool:
    """Read-only check: has this consumer group already handled this event?

    **Do not use this for idempotency.** A SELECT followed by an INSERT is a
    time-of-check/time-of-use race: two concurrent deliveries both read
    "not processed", both proceed, and the work happens twice. Use
    `claim_event()`, which decides from the INSERT itself.

    Kept for diagnostics and for read-only reporting, where no decision to
    perform work depends on the answer.
    """
    stmt = select(ProcessedEvent).where(
        ProcessedEvent.event_id == event_id,
        ProcessedEvent.consumer_group == consumer_group,
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def claim_event(
    session: AsyncSession, event_id: UUID, consumer_group: str
) -> bool:
    """Atomically claim `(event_id, consumer_group)`. Returns True if claimed.

    EVENTS.md §4.1: the consumer inserts first and decides from the insert.

        INSERT INTO processed_events (event_id, consumer_group)
        ON CONFLICT DO NOTHING
        -> 0 rows affected means someone else already has it

    Three properties that matter:

    * **Atomic.** The decision comes from the INSERT's rowcount, not from a
      prior SELECT, so there is no window between checking and acting.
    * **Transaction-scoped.** The claim lives in the caller's transaction.
      If the work then fails and the caller rolls back, the claim rolls back
      with it and a legitimate retry can re-claim. A claim that outlived a
      failed handler would strand the event forever.
    * **No exception on conflict.** `ON CONFLICT DO NOTHING` returns zero
      rows rather than raising, so a concurrent duplicate is an ordinary
      no-op rather than an error the caller has to catch.

    Concurrency note: if another *uncommitted* transaction holds the same
    key, PostgreSQL blocks here on the primary-key index until that
    transaction commits or rolls back. That is the desired behaviour - the
    loser then sees either a conflict (no-op) or a free slot (if the winner
    rolled back), and never a torn result.

    Does not commit. The caller owns the transaction boundary.
    """
    stmt = (
        pg_insert(ProcessedEvent.__table__)
        .values(event_id=event_id, consumer_group=consumer_group)
        .on_conflict_do_nothing(index_elements=["event_id", "consumer_group"])
    )
    outcome = await session.execute(stmt)
    claimed = outcome.rowcount == 1
    if not claimed:
        log.info(
            "event.claim.declined",
            event_id=str(event_id),
            consumer_group=consumer_group,
        )
    return claimed


async def record_claim_result(
    session: AsyncSession, event_id: UUID, consumer_group: str, result: str
) -> None:
    """Annotate an existing claim with its outcome, for observability.

    Separate from the claim itself: the outcome is not known until the work
    has been done, and the claim has to happen before any of it.
    """
    await session.execute(
        update(ProcessedEvent)
        .where(
            ProcessedEvent.event_id == event_id,
            ProcessedEvent.consumer_group == consumer_group,
        )
        .values(result=result)
    )
