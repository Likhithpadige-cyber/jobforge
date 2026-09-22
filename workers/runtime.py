"""M4 process lifecycle. Workers require an explicit least-privileged DSN."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Literal
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from packages.agents.llm.runtime import Prices
from packages.agents.resume_optimizer.evaluation import evaluation_handler
from packages.agents.resume_optimizer.first_draft import FirstDraftClient, generate_first_draft
from packages.agents.resume_optimizer.persistence import Thresholds
from packages.core.db.audit_session import dispose_audit_engine, init_audit_engine
from packages.core.events.monitoring import schedule_snapshot, snapshot_jobs
from packages.core.events.runtime import DeliveryRuntime, OutboxRelay, RetryPolicy, StreamConsumer
from packages.core.models import Event
from packages.shared.config import get_settings
from packages.shared.logging import configure_logging, get_logger

log = get_logger(__name__)


async def verify_role(engine: AsyncEngine, expected: str) -> None:
    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT current_user, rolsuper, rolcreaterole, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        ))).one()
        if row[0] != expected or any(row[1:]):
            raise RuntimeError(f"Worker requires the unprivileged {expected} role")


async def serve(step: Callable[[], Awaitable[object]], stop: asyncio.Event,
                *, interval: float = 0.25, shutdown_timeout: float = 30) -> None:
    """Stop intake on a signal, drain briefly, then cancel (rolling back work)."""
    while not stop.is_set():
        running = asyncio.ensure_future(step())
        stopping = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait((running, stopping), return_when=asyncio.FIRST_COMPLETED)
            if stopping in done and not running.done():
                try:
                    await asyncio.wait_for(running, timeout=shutdown_timeout)
                except TimeoutError:
                    log.warning("worker.shutdown_cancelled_inflight")
            else:
                await running
        except asyncio.CancelledError:
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await running
            raise
        except Exception as exc:
            # Error text may contain DSNs or provider content; record class only.
            log.error("worker.step_failed", error_type=type(exc).__name__)
        finally:
            stopping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopping
        if not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)


async def run(kind: Literal["relay", "monitor", "scheduler", "optimizer", "first-draft"]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    dsn = os.environ.get("WORKER_DATABASE_URL")
    if not dsn:
        raise RuntimeError("WORKER_DATABASE_URL is required; no owner-DSN fallback")
    engine = create_async_engine(dsn, pool_pre_ping=True, pool_size=2, max_overflow=0)
    redis = Redis.from_url(str(settings.redis_url), decode_responses=True,
                           socket_timeout=settings.redis_socket_timeout_seconds)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await verify_role(
            engine, "app_agent" if kind in {"relay", "optimizer", "first-draft"} else "app_monitor",
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        if kind == "relay":
            await serve(OutboxRelay(sessions, redis).step, stop)
        elif kind == "scheduler":
            async def schedule() -> None:
                async with sessions.begin() as session:
                    await schedule_snapshot(session)
            await serve(schedule, stop, interval=30)
        else:
            if kind == "first-draft":
                # Never inherit the API owner's DSN for this worker's audit pool.
                audit_engine = await init_audit_engine(
                    settings.model_copy(update={"database_url": dsn})
                )
                await verify_role(audit_engine, "app_audit" if settings.audit_database_url
                                  else "app_agent")
                prices = None
                if (settings.llm_tailoring_input_price_per_million is not None
                        and settings.llm_tailoring_output_price_per_million is not None):
                    prices = Prices(model=settings.llm_model_tailoring,
                                    input_per_million=settings.llm_tailoring_input_price_per_million,
                                    output_per_million=settings.llm_tailoring_output_price_per_million)

                async def first_draft(db: AsyncSession, event: Event) -> str:
                    client = FirstDraftClient(settings, prices=prices)
                    return await generate_first_draft(db, event, client)

                runtime = DeliveryRuntime(sessions, "cg.first-draft", {
                    "resume.first_draft.requested": first_draft,
                }, retry=RetryPolicy(delays=()))
            elif kind == "optimizer":
                selected = os.environ.get("WORKER_PROFILE_ID")
                handler = evaluation_handler(
                    UUID(selected) if selected else None, Thresholds.from_settings(settings),
                )
                runtime = DeliveryRuntime(sessions, "cg.optimizer", {"job.normalized": handler})
            else:
                runtime = DeliveryRuntime(sessions, "cg.monitor", {"schedule.tick": snapshot_jobs})
            consumer = StreamConsumer(redis, runtime, f"{socket.gethostname()}-{uuid4().hex[:8]}")
            initialized = False
            reconcile_at = 0.0

            async def step() -> None:
                nonlocal initialized, reconcile_at
                try:
                    if not initialized or time.monotonic() >= reconcile_at:
                        await consumer.initialize()
                        initialized = True
                        reconcile_at = time.monotonic() + 60
                    await consumer.step()
                except Exception:
                    initialized = False
                    raise

            await serve(step, stop)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        if kind == "first-draft":
            await dispose_audit_engine()
        await redis.aclose()
        await engine.dispose()
        log.info("worker.stopped", kind=kind)


def main(kind: Literal["relay", "monitor", "scheduler", "optimizer", "first-draft"]) -> None:
    try:
        asyncio.run(run(kind))
    except Exception as exc:
        # Startup failures must not print tracebacks containing connection secrets.
        log.error("worker.startup_failed", kind=kind, error_type=type(exc).__name__)
        raise SystemExit(1) from None
