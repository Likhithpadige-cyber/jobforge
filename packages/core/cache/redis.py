"""Async Redis connection management.

Redis is transport and cache only. Losing it costs speed, never data
(ARCHITECTURE.md §1.3) - so every consumer of this module must treat an
unavailable Redis as degraded, not fatal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import ConnectionPool, Redis

from packages.shared.config import Settings, get_settings
from packages.shared.logging import get_logger

log = get_logger(__name__)

_pool: ConnectionPool | None = None
_client: Redis | None = None


def create_pool(settings: Settings | None = None) -> ConnectionPool:
    settings = settings or get_settings()
    return ConnectionPool.from_url(
        str(settings.redis_url),
        max_connections=settings.redis_max_connections,
        socket_timeout=settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        health_check_interval=30,
        decode_responses=True,
    )


async def init_redis(settings: Settings | None = None) -> Redis:
    """Create the process-wide client. Idempotent."""
    global _pool, _client
    if _client is None:
        _pool = create_pool(settings)
        _client = Redis(connection_pool=_pool)
        log.info("redis.client.created")
    return _client


async def close_redis() -> None:
    global _pool, _client
    if _client is not None:
        await _client.aclose()
        log.info("redis.client.closed")
    if _pool is not None:
        await _pool.disconnect()
    _client = None
    _pool = None


def get_redis() -> Redis:
    if _client is None:
        raise RuntimeError("Redis client not initialised. Call init_redis() first.")
    return _client


@asynccontextmanager
async def redis_scope() -> AsyncIterator[Redis]:
    yield get_redis()
