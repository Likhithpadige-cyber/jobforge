"""Durable delivery over Redis Streams; PostgreSQL remains authoritative.

Handlers must use the supplied transaction and must not commit or perform
unrecoverable external side effects. Domain writes, emitted events and the
ProcessedEvent claim commit together. Failures roll back a savepoint before
an append-only agent.run.failed record is committed by the outer transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from redis.typing import EncodableT
from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.events.envelope import (
    HUMAN_ONLY_EVENT_TYPES,
    KNOWN_EVENT_TYPES,
    SUPPORTED_EVENT_VERSIONS,
)
from packages.core.events.outbox import claim_event, emit, record_claim_result
from packages.core.models import Event, ProcessedEvent
from packages.shared.clock import utc_now
from packages.shared.logging import get_logger

log = get_logger(__name__)
Handler = Callable[[AsyncSession, Event], Awaitable[str]]
Outcome = Literal["completed", "duplicate", "dead", "retry", "deferred", "busy", "unsubscribed"]
ACKABLE = {"completed", "duplicate", "dead", "unsubscribed"}
STREAMS = ("jobs", "resumes", "applications", "approvals", "submissions", "system")
ROUTES = {"job": "jobs", "resume": "resumes", "cover_letter": "resumes",
          "application": "applications", "approval": "approvals", "submission": "submissions"}


@dataclass(frozen=True)
class RetryPolicy:
    # ARCHITECTURE.md's five total attempts, with its explicit backoff values.
    delays: tuple[int, ...] = (1, 4, 16, 60)
    repeated_error_limit: int = 3
    handler_timeout: float = 240

    def __post_init__(self) -> None:
        if any(delay < 0 for delay in self.delays):
            raise ValueError("Retry delays must be non-negative")
        if self.repeated_error_limit < 1 or self.handler_timeout <= 0:
            raise ValueError("Retry limits and timeout must be positive")


def stream_for(event_type: str, prefix: str = "evt") -> str:
    return f"{prefix}.{ROUTES.get(event_type.split('.')[0], 'system')}"


def lock_key(event_id: UUID, group: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{event_id}:{group}".encode()).digest()[:8], signed=True)


async def publish_event(redis: Redis, event: Event, prefix: str = "evt") -> None:
    # No profile content, instructions, credentials, or other payload PII on Redis.
    message: dict[EncodableT, EncodableT] = {"event_id": str(event.id)}
    await redis.xadd(stream_for(event.type, prefix), message)
    if (event.type == "agent.run.failed" and event.payload.get("delivery_runtime") == 1
            and event.payload.get("will_retry") is False):
        await redis.xadd(f"{prefix}.dlq", message)
    event.published = True
    event.published_at = utc_now()


class MissingEventReferenceError(ValueError):
    """Redis referenced no authoritative PostgreSQL event."""


class InvalidEventError(ValueError):
    """An envelope violates the catalogue, version or human-actor boundary."""


class OutboxRelay:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], redis: Redis,
                 *, prefix: str = "evt", batch_size: int = 200) -> None:
        if batch_size < 1:
            raise ValueError("Batch size must be positive")
        self.sessions, self.redis = sessions, redis
        self.prefix, self.batch_size = prefix, batch_size

    async def step(self) -> int:
        async with self.sessions.begin() as session:
            rows = (await session.scalars(
                select(Event).where(Event.published.is_(False))
                .order_by(Event.occurred_at, Event.id).limit(self.batch_size)
                .with_for_update(skip_locked=True)
            )).all()
            for event in rows:
                await publish_event(self.redis, event, self.prefix)
            # XADD before commit: a crash can duplicate delivery, never lose it.
            if rows:
                log.info("outbox.batch_published", count=len(rows))
            return len(rows)


class DeliveryRuntime:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], group: str,
                 handlers: dict[str, Handler], *, retry: RetryPolicy | None = None) -> None:
        if not group or not handlers or not set(handlers) <= KNOWN_EVENT_TYPES:
            raise ValueError("A named group and registered event handlers are required")
        self.sessions, self.group, self.handlers = sessions, group, dict(handlers)
        self.retry = retry or RetryPolicy()

    async def process(self, event_id: UUID) -> Outcome:
        async with self.sessions.begin() as session:
            acquired = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": lock_key(event_id, self.group)},
            )
            if not acquired:
                return "busy"
            event = await session.get(Event, event_id)
            if event is None:
                # A reference to no committed source must never be treated as done.
                raise MissingEventReferenceError("Stream reference has no authoritative event")
            if await session.get(ProcessedEvent, (event_id, self.group)) is not None:
                return "duplicate"
            if event.type not in KNOWN_EVENT_TYPES:
                raise InvalidEventError("Unknown event type")
            if event.type not in self.handlers:
                return "unsubscribed"
            last = await session.scalar(
                select(Event).where(
                    Event.type == "agent.run.failed", Event.causation_id == event_id,
                    Event.payload["delivery_runtime"].as_integer() == 1,
                    Event.payload["consumer_group"].as_string() == self.group,
                ).order_by(Event.occurred_at.desc(), Event.id.desc()).limit(1)
            )
            started = await session.scalar(
                select(Event).where(
                    Event.type == "worker.delivery.started", Event.causation_id == event_id,
                    Event.payload["consumer_group"].as_string() == self.group,
                ).order_by(Event.occurred_at.desc(), Event.id.desc()).limit(1)
            )
            attempts = int(started.payload["attempt"]) if started else 0
            if started and (last is None or attempts > int(last.payload["attempt"])):
                # The previous process disappeared before it could finish/record
                # failure. Its durable start still consumes the bounded budget.
                return await self._failure(session, event, attempts, "WorkerInterruptedError", last)
            if last and last.payload["will_retry"] and attempts <= len(self.retry.delays):
                due = last.occurred_at + timedelta(seconds=self.retry.delays[attempts - 1])
                if utc_now() < due:
                    return "deferred"
            attempt = attempts + 1
            # Independent connection, while the outer transaction retains the
            # advisory lock. A hard process death cannot erase this attempt.
            async with self.sessions.begin() as ledger:
                await emit(
                    ledger, type="worker.delivery.started", aggregate_type="system",
                    actor_type="system", correlation_id=event.correlation_id,
                    causation_id=event.id,
                    payload={"consumer_group": self.group, "attempt": attempt,
                             "event_id": str(event.id)},
                )
            try:
                async with session.begin_nested():
                    if not await claim_event(session, event_id, self.group):
                        return "duplicate"
                    if event.version not in SUPPORTED_EVENT_VERSIONS[event.type]:
                        raise InvalidEventError("Unsupported event version")
                    if event.type in HUMAN_ONLY_EVENT_TYPES and event.actor_type != "human":
                        raise InvalidEventError("Forbidden event actor")
                    async with asyncio.timeout(self.retry.handler_timeout):
                        result = await self.handlers[event.type](session, event)
                    await record_claim_result(session, event_id, self.group, result)
                    await session.flush()
            except Exception as exc:
                # The savepoint removed all domain writes and the completion claim.
                return await self._failure(
                    session, event, attempt, type(exc).__name__, last,
                    terminal=isinstance(exc, InvalidEventError),
                )
            log.info("worker.delivery_completed", event_id=str(event_id), group=self.group)
            return "completed"

    async def _failure(self, session: AsyncSession, event: Event, attempt: int,
                       error_type: str, last: Event | None, *, terminal: bool = False) -> Outcome:
        counts = dict(last.payload.get("error_counts", {})) if last else {}
        counts[error_type] = int(counts.get(error_type, 0)) + 1
        terminal = (terminal or attempt > len(self.retry.delays)
                    or counts[error_type] >= self.retry.repeated_error_limit)
        failure = await emit(
            session, type="agent.run.failed", aggregate_type="system",
            correlation_id=event.correlation_id, causation_id=event.id,
            actor_type="system", payload={
                "delivery_runtime": 1, "consumer_group": self.group,
                "agent": self.group, "event_id": str(event.id),
                "attempt": attempt, "error_type": error_type,
                "error_counts": counts, "will_retry": not terminal,
            },
        )
        if terminal:
            await claim_event(session, event.id, self.group)
            await record_claim_result(session, event.id, self.group, "dead_letter")
        log.warning("worker.delivery_failed", event_id=str(event.id),
                    group=self.group, error_type=error_type, attempt=attempt,
                    terminal=terminal, failure_event_id=str(failure.id))
        return "dead" if terminal else "retry"

    async def quarantine(self, stream: str, message_id: str) -> None:
        """Durably reject a corrupt transport reference without retaining its data."""
        reference = uuid5(NAMESPACE_URL, f"jobforge:invalid:{self.group}:{stream}:{message_id}")
        async with self.sessions.begin() as session:
            if not await claim_event(session, reference, self.group):
                return
            await emit(
                session, type="agent.run.failed", aggregate_type="system",
                correlation_id=reference, actor_type="system", payload={
                    "delivery_runtime": 1, "consumer_group": self.group,
                    "agent": self.group, "attempt": 1,
                    "error_counts": {"InvalidReference": 1},
                    "error_type": "InvalidReference", "will_retry": False,
                    "invalid_reference": True, "stream": stream, "message_id": message_id,
                },
            )
            await record_claim_result(session, reference, self.group, "dead_letter")

    async def reconcile(self, redis: Redis, prefix: str = "evt", limit: int = 200,
                        *, event_ids: Sequence[UUID] | None = None) -> int:
        """Recover published but unprocessed events after loss/trim of Redis data.

        Subscriptions are explicit. Unsubscribed events are not claimed, so a
        future handler can replay them from PostgreSQL rather than losing them.
        """
        async with self.sessions() as session:
            done = exists().where(ProcessedEvent.event_id == Event.id,
                                  ProcessedEvent.consumer_group == self.group)
            query = select(Event).where(Event.published.is_(True),
                                        Event.type.in_(self.handlers), ~done)
            if event_ids is not None:
                query = query.where(Event.id.in_(event_ids))
            rows = (await session.scalars(
                query.order_by(Event.occurred_at, Event.id).limit(limit)
            )).all()
            for event in rows:
                await redis.xadd(stream_for(event.type, prefix), {"event_id": str(event.id)})
            return len(rows)


class StreamConsumer:
    def __init__(self, redis: Redis, runtime: DeliveryRuntime, name: str,
                 *, prefix: str = "evt", reclaim_ms: int = 300_000) -> None:
        self.redis, self.runtime, self.name = redis, runtime, name
        self.prefix, self.reclaim_ms = prefix, reclaim_ms
        self.streams = sorted({stream_for(kind, prefix) for kind in runtime.handlers})

    async def initialize(self, *, recover: bool = True) -> None:
        for stream in self.streams:
            try:
                await self.redis.xgroup_create(stream, self.runtime.group, id="0", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
        if recover:
            await self.runtime.reconcile(self.redis, self.prefix)

    async def _deliver(self, stream: str, message_id: str, fields: dict[str, str]) -> None:
        try:
            event_id = UUID(fields["event_id"])
        except (KeyError, ValueError):
            # Untrusted transport data is never used as domain input or logged.
            await self.runtime.quarantine(stream, message_id)
            await self.redis.xack(stream, self.runtime.group, message_id)
            log.error("worker.invalid_reference", stream=stream, message_id=message_id)
            return
        try:
            outcome = await self.runtime.process(event_id)
        except (MissingEventReferenceError, InvalidEventError):
            await self.runtime.quarantine(stream, message_id)
            await self.redis.xack(stream, self.runtime.group, message_id)
            return
        if outcome in ACKABLE:
            # A failed ACK is safe: the durable claim makes the next delivery a no-op.
            await self.redis.xack(stream, self.runtime.group, message_id)

    async def step(self) -> None:
        for stream in self.streams:
            reclaimed = await self.redis.xautoclaim(
                stream, self.runtime.group, self.name, self.reclaim_ms, count=20,
            )
            for message_id, fields in reclaimed[1]:
                await self._deliver(stream, message_id, fields)
            # Own pending entries honor the short backoff; abandoned entries are
            # reclaimed separately. Always read new entries too to avoid starvation.
            for marker in ("0", ">"):
                batches = await self.redis.xreadgroup(
                    self.runtime.group, self.name, {stream: marker}, count=20,
                )
                for _, messages in batches:
                    for message_id, fields in messages:
                        await self._deliver(stream, message_id, fields)
