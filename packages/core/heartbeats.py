"""Worker liveness and stream lag, read from Redis (M12 Phase 2).

Liveness is inherently live state, so it lives in Redis rather than a new
table: each worker instance writes its kind and last-seen time into one hash.
A field only exists after the worker has registered, which is what lets the
monitor report a *missing* worker without ever inventing one that never ran.
A clean shutdown removes the field; a crash leaves it to go stale.

Nothing here touches PostgreSQL, and a Redis failure never stops a worker.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from packages.core.events.runtime import STREAMS
from packages.shared.logging import get_logger

log = get_logger(__name__)

HEARTBEAT_KEY = "jobforge:worker_heartbeats"
#: Registrations older than this are forgotten. The missing-worker alert they
#: produced is durable in PostgreSQL long before this.
HEARTBEAT_RETENTION_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class Heartbeat:
    instance: str
    kind: str
    last_seen: datetime


@dataclass(frozen=True)
class ConsumerLag:
    stream: str
    group: str
    lag: int | None  # None when Redis cannot determine it


class HeartbeatWriter:
    """Throttled registration for one worker process."""

    def __init__(
        self,
        redis: Redis,
        *,
        kind: str,
        instance: str,
        interval: float,
        key: str = HEARTBEAT_KEY,
    ) -> None:
        self.redis, self.kind, self.instance = redis, kind, instance
        self.interval, self.key = interval, key
        self._next = 0.0

    async def beat(self) -> None:
        if time.monotonic() < self._next:
            return
        value = json.dumps({"kind": self.kind, "last_seen": time.time()})
        try:
            await self.redis.hset(self.key, self.instance, value)  # type: ignore[misc]
        except RedisError as exc:
            log.warning("worker.heartbeat_failed", error_type=type(exc).__name__)
            return
        self._next = time.monotonic() + self.interval

    async def deregister(self) -> None:
        try:
            await self.redis.hdel(self.key, self.instance)  # type: ignore[misc]
        except RedisError as exc:
            log.warning("worker.heartbeat_deregister_failed", error_type=type(exc).__name__)


class RedisOperationsProbe:
    """What the monitor reads from Redis. Malformed entries are skipped."""

    def __init__(self, redis: Redis, *, prefix: str = "evt", key: str = HEARTBEAT_KEY) -> None:
        self.redis, self.prefix, self.key = redis, prefix, key

    async def heartbeats(self) -> list[Heartbeat]:
        raw = await self.redis.hgetall(self.key)  # type: ignore[misc]
        beats = []
        for instance, value in sorted(raw.items()):
            try:
                data = json.loads(value)
                seen = datetime.fromtimestamp(float(data["last_seen"]), UTC)
                kind = str(data["kind"])
            except (ValueError, KeyError, TypeError):
                continue
            beats.append(Heartbeat(instance=str(instance), kind=kind, last_seen=seen))
        return beats

    async def forget(self, instances: list[str]) -> None:
        if instances:
            await self.redis.hdel(self.key, *instances)  # type: ignore[misc]

    async def consumer_lags(self) -> list[ConsumerLag]:
        lags = []
        for name in STREAMS:
            stream = f"{self.prefix}.{name}"
            try:
                groups = await self.redis.xinfo_groups(stream)
            except ResponseError:
                continue  # the stream does not exist yet
            for group in groups:
                lag = group.get("lag")
                lags.append(
                    ConsumerLag(
                        stream=stream,
                        group=str(group["name"]),
                        lag=int(lag) if isinstance(lag, int) else None,
                    )
                )
        return sorted(lags, key=lambda item: (item.stream, item.group))
