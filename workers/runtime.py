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
from packages.core.services.applications import (
    prepare_application,
    questions_from_event_payload,
)
from packages.core.services.artifacts import render_resume
from packages.core.services.cover_letters import CoverLetterClient, generate_cover_letter
from packages.core.services.submission_readiness import (
    check_readiness_on_approval,
    retry_packet_generation,
)
from packages.services.rendering import production_renderers
from packages.services.rendering.store import (
    ArtifactStore,
    ArtifactStoreUnavailableError,
    FilesystemArtifactStore,
    S3ArtifactStore,
)
from packages.shared.config import Settings, get_settings
from packages.shared.logging import configure_logging, get_logger

log = get_logger(__name__)

WorkerKind = Literal[
    "relay", "monitor", "scheduler", "optimizer", "first-draft", "render", "application",
    "cover-letter",
]


def build_artifact_store(settings: Settings) -> ArtifactStore:
    """The worker's object store, chosen the same way the API chooses its own.

    Duplicated deliberately rather than imported from apps.api.deps: a worker
    must not depend on the API package, and the alternative - a shared module
    that both import - would be a third place to keep in step for two short
    branches. If a third caller appears, extract it then.
    """
    if settings.artifact_storage_backend == "filesystem":
        return FilesystemArtifactStore(settings.artifact_storage_root)
    if settings.artifact_storage_backend == "s3":
        return S3ArtifactStore(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
        )
    raise ArtifactStoreUnavailableError(
        f"unknown artifact storage backend {settings.artifact_storage_backend!r}"
    )


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


async def run(
    kind: WorkerKind,
) -> None:
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
            engine,
            "app_agent"
            if kind in {"relay", "optimizer", "first-draft", "render", "application",
                        "cover-letter"}
            else "app_monitor",
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
            elif kind == "render":
                # M8. Deterministic and offline: no model, no provider budget,
                # no network beyond the object store. The renderers are the
                # production pair; a non-production renderer is refused by
                # `render_resume`, so this worker cannot be talked into
                # serving a placeholder document.
                store = build_artifact_store(settings)
                renderers = production_renderers()

                async def render(db: AsyncSession, event: Event) -> str:
                    resume_id = UUID(str(event.payload["resume_id"]))
                    await render_resume(
                        db,
                        store=store,
                        renderers=renderers,
                        resume_id=resume_id,
                        causation_id=event.id,
                    )
                    return "completed"

                runtime = DeliveryRuntime(
                    sessions,
                    "cg.render",
                    {"resume.render.requested": render},
                )
            elif kind == "application":
                # M9. Deterministic and offline, same as "render": no model,
                # no provider budget. Question extraction, classification and
                # grounding are all pattern/database matches, not inference.
                async def prepare(db: AsyncSession, event: Event) -> str:
                    application_id = UUID(str(event.payload["application_id"]))
                    questions = questions_from_event_payload(event.payload)
                    await prepare_application(
                        db,
                        application_id=application_id,
                        questions=questions,
                        causation_id=event.id,
                    )
                    return "completed"

                # M10 Phase 2. Also deterministic and offline - a snapshot
                # comparison and an already-built policy lookup, no model.
                # Reuses this same worker kind rather than a new one, per the
                # locked design: readiness needs no LLM/audit-engine wiring,
                # so it belongs beside "prepare", not beside "cover-letter".
                readiness_store = build_artifact_store(settings)

                async def on_approval_granted(db: AsyncSession, event: Event) -> str:
                    return await check_readiness_on_approval(
                        db, event, store=readiness_store
                    )

                # M10 Phase 2 stabilization (part C). The deterministic retry
                # path for the packet race: resume.render.requested fires the
                # instant a resume is approved, but the "render" worker that
                # turns it into a resume_pdf artifact is asynchronous and may
                # not have finished by the time readiness ran. Already an
                # expected cg.application stream (ARCHITECTURE.md's own
                # table), and almost every delivery is a cheap no-op - see
                # retry_packet_generation's own docstring.
                async def on_resume_rendered(db: AsyncSession, event: Event) -> str:
                    return await retry_packet_generation(
                        db, event, store=readiness_store
                    )

                runtime = DeliveryRuntime(
                    sessions,
                    "cg.application",
                    {
                        "application.questions.requested": prepare,
                        "approval.granted": on_approval_granted,
                        "resume.rendered": on_resume_rendered,
                    },
                )
            elif kind == "cover-letter":
                # M10 Phase 1. LLM-calling and cost-shaped, like "first-draft":
                # needs its own audit-engine connection and pricing, not the
                # deterministic "application"/"render" workers' shape. Same
                # no-auto-retry choice as "first-draft", for the same reason -
                # a failed LLM attempt is not blindly retried.
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

                async def draft(db: AsyncSession, event: Event) -> str:
                    client = CoverLetterClient(settings, prices=prices)
                    application_id = UUID(str(event.payload["application_id"]))
                    note = event.payload.get("note") or None
                    cover_letter = await generate_cover_letter(
                        db,
                        application_id=application_id,
                        event_id=event.id,
                        client=client,
                        note=note,
                        causation_id=event.id,
                    )
                    return f"{cover_letter.verification_status}:{cover_letter.id}"

                runtime = DeliveryRuntime(sessions, "cg.cover-letter", {
                    "cover_letter.generation.requested": draft,
                }, retry=RetryPolicy(delays=()))
            elif kind == "optimizer":
                selected = os.environ.get("WORKER_PROFILE_ID")
                handler = evaluation_handler(
                    UUID(selected) if selected else None,
                    Thresholds.from_settings(settings),
                )
                runtime = DeliveryRuntime(
                    sessions, "cg.optimizer", {"job.normalized": handler}
                )
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
        if kind in {"first-draft", "cover-letter"}:
            await dispose_audit_engine()
        await redis.aclose()
        await engine.dispose()
        log.info("worker.stopped", kind=kind)


def main(
    kind: WorkerKind,
) -> None:
    try:
        asyncio.run(run(kind))
    except Exception as exc:
        # Startup failures must not print tracebacks containing connection secrets.
        log.error("worker.startup_failed", kind=kind, error_type=type(exc).__name__)
        raise SystemExit(1) from None
