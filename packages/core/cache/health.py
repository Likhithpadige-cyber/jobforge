"""Redis health probe."""

from __future__ import annotations

import time

from packages.core.cache.redis import get_redis
from packages.shared.logging import get_logger

log = get_logger(__name__)


async def check_redis(timeout_seconds: float = 3.0) -> dict[str, object]:
    """Return a health record for Redis. Never raises."""
    started = time.perf_counter()
    try:
        client = get_redis()
        pong = await client.ping()
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        if not pong:
            return {"status": "unavailable", "latency_ms": latency_ms, "error": "NoPong"}
        return {"status": "ok", "latency_ms": latency_ms}
    except Exception as exc:  # noqa: BLE001
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        log.warning("health.redis.failed", error=type(exc).__name__)
        return {
            "status": "unavailable",
            "latency_ms": latency_ms,
            "error": type(exc).__name__,
        }
