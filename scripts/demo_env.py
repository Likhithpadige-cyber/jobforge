#!/usr/bin/env python3
"""Isolated demo environment helper. See docs/DEMO_ENVIRONMENT.md.

    python scripts/demo_env.py write-env    # host: create .env.demo (never overwrites)
    python -m scripts.demo_env check         # demo container: identity checks only
    python -m scripts.demo_env init          # demo container: mark DB, set role passwords
    python -m scripts.demo_env seed          # demo container: fictional demo data

Every database operation refuses to run unless **all** of these hold:

* `JOBFORGE_DEMO_ENVIRONMENT=1`, `ENVIRONMENT=local`, `LLM_MODE=replay`,
  `SUBMISSION_MODE=dry_run`, `EMAIL_TRANSPORT_MODE=fake`, and no provider key;
* the configured and the *connected* database are both `jobforge_demo`;
* for `seed`, the database also carries the demo marker comment that only
  `init` writes, and `init` itself only writes it on `jobforge_demo`.

Nothing here drops, truncates or deletes anything. Every row is fictional
(`example.com` addresses, synthetic incidents labelled as such) and the seed
is idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

DEMO_DATABASE = "jobforge_demo"
DEMO_MARKER = "jobforge-demo-environment: synthetic data only, safe to discard"
OWNER_EMAIL = "demo.candidate@example.com"  # created by scripts/seed.py
REVIEWER_EMAIL = "demo.reviewer@example.com"
ROOT = Path(__file__).resolve().parent.parent


class DemoGuardError(RuntimeError):
    """The target is not verifiably the isolated demo environment."""


# ------------------------------------------------------------- .env.demo
def write_env(path: Path) -> int:
    """Create .env.demo with fresh random local-only secrets. Never overwrites."""
    if path.exists():
        print(f"{path.name} already exists; leaving it untouched.")
        return 0
    pw = {name: secrets.token_hex(16) for name in ("postgres", "agent", "monitor", "api", "audit")}
    values = {
        "JOBFORGE_DEMO_ENVIRONMENT": "1",
        "ENVIRONMENT": "local",
        "APP_NAME": "JobForge Demo",
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "json",
        "API_PREFIX": "/api/v1",
        "CORS_ORIGINS": '["http://127.0.0.1:3100"]',
        "POSTGRES_USER": "jobforge_demo",
        "POSTGRES_PASSWORD": pw["postgres"],
        "POSTGRES_DB": DEMO_DATABASE,
        "APP_API_PASSWORD": pw["api"],
        "APP_AGENT_PASSWORD": pw["agent"],
        "APP_MONITOR_PASSWORD": pw["monitor"],
        "APP_AUDIT_PASSWORD": pw["audit"],
        "DEMO_WORKER_AGENT_DATABASE_URL": (
            f"postgresql+asyncpg://app_agent:{pw['agent']}@postgres:5432/{DEMO_DATABASE}"
        ),
        "DEMO_WORKER_MONITOR_DATABASE_URL": (
            f"postgresql+asyncpg://app_monitor:{pw['monitor']}@postgres:5432/{DEMO_DATABASE}"
        ),
        "REDIS_DB": "0",
        "LLM_MODE": "replay",
        "ANTHROPIC_API_KEY": "",
        "SUBMISSION_MODE": "dry_run",
        "EMAIL_TRANSPORT_MODE": "fake",
        "SMTP_HOST": "",
        "SMTP_USERNAME": "",
        "SMTP_PASSWORD": "",
        "ARTIFACT_STORAGE_BACKEND": "filesystem",
        "ARTIFACT_STORAGE_ROOT": "/home/app/jobforge-demo-artifacts",
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "TOTP_ENCRYPTION_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
        "DEMO_OWNER_PASSWORD": secrets.token_urlsafe(18),
        "DEMO_REVIEWER_PASSWORD": secrets.token_urlsafe(18),
        "DEMO_POSTGRES_HOST_PORT": "55433",
        "DEMO_REDIS_HOST_PORT": "56380",
        "DEMO_API_HOST_PORT": "8100",
        "DEMO_WEB_HOST_PORT": "3100",
    }
    header = (
        "# JobForge isolated demo environment (docs/DEMO_ENVIRONMENT.md).\n"
        "# Generated locally; git-ignored. Local-only random secrets for a\n"
        "# throwaway stack holding fictional data. Never copy values from .env.\n"
    )
    body = "".join(f"{key}={value}\n" for key, value in values.items())
    path.write_text(header + body)
    path.chmod(0o600)
    print(f"wrote {path.name} (mode 600)")
    return 0


# ---------------------------------------------------------------- guards
def check_settings() -> Any:
    from packages.shared.config import (
        EmailTransportMode,
        Environment,
        LLMMode,
        SubmissionMode,
        get_settings,
    )

    settings = get_settings()
    problems = []
    if os.environ.get("JOBFORGE_DEMO_ENVIRONMENT") != "1":
        problems.append("JOBFORGE_DEMO_ENVIRONMENT is not 1")
    if settings.postgres_db != DEMO_DATABASE:
        problems.append(f"POSTGRES_DB is {settings.postgres_db!r}, not {DEMO_DATABASE!r}")
    if DEMO_DATABASE not in str(settings.database_url):
        problems.append("DATABASE_URL does not point at the demo database")
    if settings.environment is not Environment.LOCAL:
        problems.append(f"ENVIRONMENT is {settings.environment}, not local")
    if settings.llm_mode is not LLMMode.REPLAY:
        problems.append(f"LLM_MODE is {settings.llm_mode}, not replay")
    if settings.submission_mode is not SubmissionMode.DRY_RUN:
        problems.append(f"SUBMISSION_MODE is {settings.submission_mode}, not dry_run")
    if settings.email_transport_mode is not EmailTransportMode.FAKE:
        problems.append(f"EMAIL_TRANSPORT_MODE is {settings.email_transport_mode}, not fake")
    if settings.anthropic_api_key:
        problems.append("a provider API key is configured; the demo must not carry one")
    if problems:
        raise DemoGuardError("refusing: " + "; ".join(problems))
    return settings


async def check_database(conn: Any, *, require_marker: bool) -> None:
    from sqlalchemy import text

    name = await conn.scalar(text("SELECT current_database()"))
    if name != DEMO_DATABASE:
        raise DemoGuardError(f"refusing: connected to {name!r}, not {DEMO_DATABASE!r}")
    if require_marker:
        marker = await conn.scalar(
            text(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
                "WHERE datname = current_database()"
            )
        )
        if marker != DEMO_MARKER:
            raise DemoGuardError("refusing: database lacks the demo marker; run `init` first")


async def _guarded_engine(*, require_marker: bool) -> Any:
    from sqlalchemy.ext.asyncio import create_async_engine

    settings = check_settings()
    engine = create_async_engine(str(settings.database_url))
    async with engine.connect() as conn:
        await check_database(conn, require_marker=require_marker)
    return engine


# ------------------------------------------------------------------ init
async def init() -> int:
    from sqlalchemy import text

    engine = await _guarded_engine(require_marker=False)
    try:
        async with engine.begin() as conn:
            await check_database(conn, require_marker=False)
            await conn.execute(text(f"COMMENT ON DATABASE {DEMO_DATABASE} IS '{DEMO_MARKER}'"))
            for role, variable in (
                ("app_api", "APP_API_PASSWORD"),
                ("app_agent", "APP_AGENT_PASSWORD"),
                ("app_monitor", "APP_MONITOR_PASSWORD"),
                ("app_audit", "APP_AUDIT_PASSWORD"),
            ):
                password = os.environ.get(variable, "")
                if not password:
                    raise DemoGuardError(f"refusing: {variable} is not set")
                exists = await conn.scalar(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
                )
                if exists:
                    # Roles are cluster-level; this cluster is the demo container's own.
                    literal = password.replace("'", "''")
                    await conn.execute(text(f"ALTER ROLE {role} LOGIN PASSWORD '{literal}'"))
        print(f"initialised {DEMO_DATABASE}: marker set, role passwords applied")
        return 0
    finally:
        await engine.dispose()


# ------------------------------------------------------------------ seed
async def seed() -> int:
    engine = await _guarded_engine(require_marker=True)
    await engine.dispose()

    # The existing fictional seeds, run against the verified demo settings.
    from scripts import seed as base_seed
    from scripts import seed_resume_studio_demo as studio_seed

    for step in (base_seed.main, studio_seed.main):
        if await step() != 0:
            print(f"seed step {step.__module__} failed")
            return 1
    engine = await _guarded_engine(require_marker=True)
    try:
        # Accounts first; the review workflow runs before the synthetic
        # pausing incidents exist, because those pause generation.
        await _seed_accounts(engine)
        await _seed_review_workflow()
        await _seed_incidents(engine)
    finally:
        await engine.dispose()
    print("demo seed complete")
    return 0


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"jobforge-demo:{name}")


async def _seed_accounts(engine: Any) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.core.auth import hash_password

    owner_password = os.environ.get("DEMO_OWNER_PASSWORD", "")
    reviewer_password = os.environ.get("DEMO_REVIEWER_PASSWORD", "")
    if len(owner_password) < 12 or len(reviewer_password) < 12:
        raise DemoGuardError("refusing: demo passwords missing or shorter than 12 characters")

    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        await check_database(await session.connection(), require_marker=True)
        owner = await session.scalar(
            text("SELECT id FROM users WHERE email = :e"), {"e": OWNER_EMAIL}
        )
        if owner is None:
            raise DemoGuardError("the base seed did not create the demo owner")
        await session.execute(
            text("UPDATE users SET password_hash = :h, role = 'owner' WHERE id = :id"),
            {"h": hash_password(owner_password), "id": owner},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, email, role, password_hash) "
                "VALUES (:id, :e, 'reviewer', :h) "
                "ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash"
            ),
            {"id": _id("reviewer"), "e": REVIEWER_EMAIL, "h": hash_password(reviewer_password)},
        )

    print(f"accounts: {OWNER_EMAIL} (owner), {REVIEWER_EMAIL} (reviewer)")


async def _seed_incidents(engine: Any) -> None:
    from typing import cast

    from sqlalchemy import Table, func, select
    from sqlalchemy.dialects.postgresql import insert
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.core.events.envelope import EventEnvelope
    from packages.core.events.guardrail_alerts import open_guardrail_alert
    from packages.core.models import Alert, Event, ProcessedEvent

    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        await check_database(await session.connection(), require_marker=True)
        # Two synthetic generation-pause incidents through the real Phase 1 path.
        for index in (1, 2):
            envelope = EventEnvelope(
                id=_id(f"guardrail:{index}"),
                type="guardrail.violation.detected",
                aggregate_type="system",
                actor_type="agent",
                actor_id="demo_seed",
                correlation_id=_id(f"guardrail:{index}"),
                payload={
                    "kind": "fabrication",
                    "severity": "critical",
                    "agent": "demo_seed",
                    "detail": "Synthetic demo incident; no real content.",
                },
            )
            await session.execute(
                insert(cast(Table, Event.__table__))
                .values(**envelope.as_row())
                .on_conflict_do_nothing(index_elements=["id"])
            )
            event = await session.get(Event, envelope.id)
            assert event is not None
            await open_guardrail_alert(session, event)

        # One synthetic dead-lettered delivery. The monitor raises its alert
        # from this row on its next tick, so the dashboard's dead-letter count
        # and the open alert always agree. No render worker runs in the demo,
        # so nothing ever re-delivers it.
        failed = EventEnvelope(
            id=_id("dead-letter:render"),
            type="resume.render.requested",
            aggregate_type="system",
            actor_type="system",
            actor_id="demo_seed",
            correlation_id=_id("dead-letter:render"),
            payload={"synthetic": True, "note": "Synthetic demo delivery; no real content."},
        )
        await session.execute(
            insert(cast(Table, Event.__table__))
            .values(**failed.as_row(), published=True, published_at=failed.occurred_at)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await session.execute(
            insert(cast(Table, ProcessedEvent.__table__))
            .values(event_id=failed.id, consumer_group="cg.render", result="dead_letter")
            .on_conflict_do_nothing(index_elements=["event_id", "consumer_group"])
        )

        # Resolved history, to exercise filters and pagination.
        now = datetime.now(timezone.utc)  # noqa: UP017 - host write-env runs on Python 3.9
        rows: list[dict[str, Any]] = []
        history = ("worker_heartbeat_missing", "consumer_lag", "application_stalled")
        for index in range(60):
            kind = history[index % 3]
            rows.append(
                _alert(
                    kind,
                    "medium",
                    f"demo:history:{kind}:{index}",
                    now - timedelta(days=1, minutes=17 * index),
                    resolved=True,
                )
            )
        await session.execute(
            insert(cast(Table, Alert.__table__))
            .values(rows)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        total = await session.scalar(select(func.count()).select_from(Alert))
        print(f"alerts in demo database: {total}")


REVIEW_JOB_URL = "mailto:careers@demo-cloud.example"
REVIEW_DOMAIN = "demo-cloud.example"  # reserved .example TLD: undeliverable by design
DEMO_QUESTIONS = (
    ("Are you legally authorized to work in the United States?", "boolean", True, None),
    ("Which cloud platform have you used most?", "text", True, 200),
    ("Why are you interested in this role?", "textarea", False, 1000),
)


async def _replay_cover_letter(session: Any, application_id: UUID) -> None:
    """Generate a verified cover letter through the real pipeline, replayed.

    The same offline technique the integration tests use: cassettes for the
    drafting call and each paragraph's B5 verification are written to a
    temporary directory, and `generate_cover_letter` runs end to end in
    replay mode. No provider is configured, so nothing can leave the machine.
    """
    import tempfile
    from decimal import Decimal
    from pathlib import Path

    from packages.agents.cover_letter.drafting import prepare_request as draft_request
    from packages.agents.llm.runtime import Response, canonical
    from packages.agents.resume_optimizer import verification as b5
    from packages.agents.resume_optimizer.context import load_tailoring_context
    from packages.core.models import Application
    from packages.core.services import cover_letters
    from packages.shared.config import get_settings

    application = await session.get(Application, application_id)
    context = await load_tailoring_context(
        session, profile_id=application.profile_id, job_id=application.job_id
    )
    by_text = {fact.text.lower(): fact for fact in context.facts}
    aws = next(fact for text, fact in by_text.items() if "aws" in text)
    paragraphs = [
        {
            "text": "I design and run cloud infrastructure on AWS, defined as code.",
            "fact_ids": [str(aws.id)],
            "numerals_used": [],
        },
        {
            "text": "My verified work covers the AWS foundations this role relies on.",
            "fact_ids": [str(aws.id)],
            "numerals_used": [],
        },
        {
            "text": "I would bring that same evidence-backed approach to this team.",
            "fact_ids": [str(aws.id)],
            "numerals_used": [],
        },
    ]
    with tempfile.TemporaryDirectory(prefix="jobforge-demo-replay-") as directory:
        client = cover_letters.CoverLetterClient(get_settings(), cassette_dir=Path(directory))
        draft = draft_request(context, model=client.model, note=None)
        (Path(directory) / f"{draft.digest}.json").write_text(
            Response(
                body=canonical({"paragraphs": paragraphs}),
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal("0"),
                stop_reason="tool_use",
            ).model_dump_json()
        )
        for paragraph in paragraphs:
            verify = b5.prepare_request(str(paragraph["text"]), [aws], client.model)
            (Path(directory) / f"{verify.digest}.json").write_text(
                Response(
                    body=canonical(
                        {"verdict": "supported", "explanation": "Synthetic replay verdict."}
                    ),
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=Decimal("0"),
                    stop_reason="tool_use",
                ).model_dump_json()
            )
        await cover_letters.generate_cover_letter(
            session,
            application_id=application_id,
            event_id=_id(f"cover:{application_id}"),
            client=client,
        )


async def _seed_review_workflow() -> None:
    """Two fictional applications that show the review and submission flow.

    * A (the Resume Studio application): questions prepared, resume review
      opened and a verified cover letter generated - awaiting review.
    * B (a second fictional job): everything approved, the resume rendered,
      an EMAIL policy for a reserved `.example` domain, and readiness
      computed - `submission_ready`, which dry-run mode then blocks.

    Everything runs through the real services. Idempotent by state.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from packages.core.db import dispose_engine, init_engine, session_scope
    from packages.core.db.audit_session import dispose_audit_engine, init_audit_engine
    from packages.core.models import (
        Application,
        ApplicationStatus,
        Event,
        Job,
        SitePolicy,
        SubmissionMethod,
        User,
    )
    from packages.core.services import cover_letters, resume_review
    from packages.core.services.applications import RawQuestion, prepare_application
    from packages.core.services.artifacts import render_resume
    from packages.core.services.submission_readiness import check_readiness_on_approval
    from packages.services.rendering import production_renderers
    from packages.services.rendering.store import FilesystemArtifactStore
    from packages.shared.clock import utc_today
    from scripts import seed_resume_studio_demo as studio

    settings = check_settings()
    await init_engine(settings)
    await init_audit_engine(settings)
    store = FilesystemArtifactStore(settings.artifact_storage_root)
    try:
        async with session_scope() as session:
            await check_database(await session.connection(), require_marker=True)
            owner = await session.scalar(select(User).where(User.email == OWNER_EMAIL))
            profile = await studio._demo_profile(session)
            if owner is None or profile is None:
                raise DemoGuardError("the base seed did not create the demo owner and profile")
            profile_id, user_id = profile

            # ---- A: awaiting review --------------------------------------
            job_a = await session.scalar(select(Job).where(Job.url == studio.DEMO_JOB_URL))
            app_a = (
                await session.scalar(select(Application).where(Application.job_id == job_a.id))
                if job_a
                else None
            )
            if app_a is not None and app_a.status is ApplicationStatus.RESUME_READY:
                await prepare_application(
                    session,
                    application_id=app_a.id,
                    questions=[
                        RawQuestion(
                            question_text=question,
                            input_type=kind,
                            is_required=required,
                            max_length=limit,
                        )
                        for question, kind, required, limit in DEMO_QUESTIONS
                    ],
                )
                await session.flush()
                await resume_review.enter_review(session, application_id=app_a.id, actor=owner)
                await session.flush()
                await _replay_cover_letter(session, app_a.id)
                print(f"review demo (A): {app_a.id} -> awaiting review")
            else:
                print("review demo (A): already prepared; nothing to do")

            # ---- B: submission ready (blocked by dry-run) ----------------
            if await session.scalar(select(Job.id).where(Job.url == REVIEW_JOB_URL)) is None:
                facts = await studio._facts(session, profile_id, user_id)
                app_b, _ = await studio.build_tailored_application(
                    session,
                    profile_id=profile_id,
                    facts=facts,
                    source=await studio._source(session),
                    company=await studio._company(session),
                    url=REVIEW_JOB_URL,
                    title="Platform Engineer",
                    description=(
                        "Fictional demo posting. Platform Engineer for AWS infrastructure "
                        "and CI/CD. Applications by email to the posting address."
                    ),
                    actor_id="demo_seed",
                )
                if (
                    await session.scalar(
                        select(SitePolicy.id).where(SitePolicy.domain == REVIEW_DOMAIN)
                    )
                    is None
                ):
                    session.add(
                        SitePolicy(
                            id=_id("policy:review-domain"),
                            domain=REVIEW_DOMAIN,
                            submission_method=SubmissionMethod.EMAIL,
                            automation_allowed=False,
                            next_review_due=utc_today() + timedelta(days=180),
                        )
                    )
                await prepare_application(session, application_id=app_b.id, questions=[])
                await session.flush()
                await resume_review.enter_review(session, application_id=app_b.id, actor=owner)
                await session.flush()
                await _replay_cover_letter(session, app_b.id)
                await cover_letters.approve_cover_letter(
                    session, application_id=app_b.id, actor=owner
                )
                resume = await resume_review.latest_tailored_resume(session, app_b.id)
                if resume is None:
                    raise DemoGuardError("the submission demo has no tailored resume")
                from packages.core.services.resume_export import preview_export

                export_preview = await preview_export(session, resume, owner)
                await resume_review.approve(
                    session,
                    application_id=app_b.id,
                    actor=owner,
                    export_digest=export_preview["digest"],
                )
                await render_resume(
                    session, store=store, renderers=production_renderers(), resume_id=resume.id
                )
                await session.flush()
                granted = await session.scalar(
                    select(Event)
                    .where(Event.type == "approval.granted", Event.correlation_id == app_b.id)
                    .order_by(Event.occurred_at.desc(), Event.id.desc())
                    .limit(1)
                )
                if granted is None:
                    raise DemoGuardError("approving the submission demo emitted no approval")
                await check_readiness_on_approval(session, granted, store=store)
                await session.flush()
                status = await session.scalar(
                    select(Application.status).where(Application.id == app_b.id)
                )
                print(f"submission demo (B): {app_b.id} -> {status}")
            else:
                print("submission demo (B): already present; nothing to do")
    finally:
        await dispose_audit_engine()
        await dispose_engine()


async def e2e_fixture() -> int:
    """Fresh, uniquely named synthetic records for one browser E2E run.

    Prints one JSON line: an application in `awaiting_approval` (pending
    resume and cover-letter review, one required question nobody has
    answered), an application at `resume_ready` with no questions yet, and
    a draft fact. Each run creates new rows, so tests that
    approve or retire never depend on what an earlier run left behind.
    Everything goes through the real services with replayed generation;
    if generation is paused, this fails closed rather than bypassing it.
    """
    import json
    from uuid import NAMESPACE_URL, uuid5

    from sqlalchemy import select

    from packages.core.auth.passwords import hash_password
    from packages.core.db import dispose_engine, init_engine, session_scope
    from packages.core.db.audit_session import dispose_audit_engine, init_audit_engine
    from packages.core.generation_safety import generation_is_paused, resolve_alert
    from packages.core.models import Alert, Experience, FactType, User
    from packages.core.repositories import FactRepository
    from packages.core.services import resume_review
    from packages.core.services.applications import RawQuestion, prepare_application
    from packages.shared.ids import uuid7
    from scripts import seed_resume_studio_demo as studio

    settings = check_settings()
    run = uuid7().hex[-12:]
    await init_engine(settings)
    await init_audit_engine(settings)
    try:
        async with session_scope() as session:
            await check_database(await session.connection(), require_marker=True)
            owner = await session.scalar(select(User).where(User.email == OWNER_EMAIL))
            profile = await studio._demo_profile(session)
            if owner is None or profile is None:
                raise DemoGuardError("run `make demo-seed` before the browser E2E suite")
            profile_id, user_id = profile
            # Run-unique test accounts. The caller generates their passwords
            # in memory and passes them in the environment; they are hashed
            # here and never printed. `e2e-retire` disables them afterwards.
            # The seeded demo owner and reviewer are never touched.
            accounts = {}
            created: dict[str, Any] = {}
            for role, variable in (
                ("owner", "JOBFORGE_E2E_OWNER_PASSWORD"),
                ("reviewer", "JOBFORGE_E2E_REVIEWER_PASSWORD"),
            ):
                password = os.environ.get(variable, "")
                if len(password) < 16:
                    raise DemoGuardError(f"{variable} must be set (16+ characters) by the caller")
                user = User(
                    id=uuid7(),
                    email=f"e2e-{role}-{run}@example.com",
                    role=role,
                    password_hash=hash_password(password),
                )
                session.add(user)
                created[role] = user
                accounts[f"{role}_email"] = user.email
            await session.flush()

            # A freshly seeded demo is deliberately paused by its two synthetic
            # incidents (_seed_incidents). Resolve exactly those, through the
            # real owner-resolution service and attributed to this run's test
            # owner -- the same action as Resolve in the Alerts UI. Any other
            # pausing incident is left alone and stops the run (fail closed).
            for index in (1, 2):
                alert_id = uuid5(
                    NAMESPACE_URL, f"jobforge:guardrail-alert:{_id(f'guardrail:{index}')}"
                )
                alert = await session.get(Alert, alert_id)
                if alert is not None and alert.status != "resolved":
                    await resolve_alert(session, alert_id, actor=created["owner"])
            await session.flush()
            if await generation_is_paused(session):
                raise DemoGuardError(
                    "generation is paused by an incident the demo seed did not create; "
                    "review and resolve it in Alerts before running the browser suite"
                )
            facts = await studio._facts(session, profile_id, user_id)
            application, _ = await studio.build_tailored_application(
                session,
                profile_id=profile_id,
                facts=facts,
                source=await studio._source(session),
                company=await studio._company(session),
                url=f"https://jobs.example/e2e/{run}",
                title=f"E2E Cloud Engineer {run}",
                description=f"Fictional browser E2E posting {run}. AWS, Terraform and CI/CD.",
                actor_id="demo_e2e_fixture",
            )
            await prepare_application(
                session,
                application_id=application.id,
                questions=[
                    RawQuestion(
                        question_text="Why are you interested in this role?",
                        input_type="textarea",
                        is_required=True,
                        max_length=1000,
                    )
                ],
            )
            await session.flush()
            await resume_review.enter_review(session, application_id=application.id, actor=owner)
            await session.flush()
            await _replay_cover_letter(session, application.id)
            # A second run-unique application left at `resume_ready` with no
            # questions, for the question-preparation journey. Its posting
            # and resume are synthetic, built from the demo's verified facts.
            ready, _ = await studio.build_tailored_application(
                session,
                profile_id=profile_id,
                facts=facts,
                source=await studio._source(session),
                company=await studio._company(session),
                url=f"https://jobs.example/e2e/{run}/ready",
                title=f"E2E Ready Engineer {run}",
                description=f"Fictional browser E2E posting {run}. Linux, Ansible and on-call.",
                actor_id="demo_e2e_fixture",
            )
            await session.flush()
            experience = await session.scalar(
                select(Experience.id).where(Experience.profile_id == profile_id).limit(1)
            )
            fact = await FactRepository(session).create_draft(
                profile_id=profile_id,
                fact_type=FactType.SKILL,
                text_value=f"Synthetic E2E skill {run}",
                source_table="experiences",
                source_id=experience or uuid7(),
            )
            await session.flush()
            print(
                json.dumps(
                    {
                        "run": run,
                        "application_id": str(application.id),
                        "ready_application_id": str(ready.id),
                        "fact_id": str(fact.id),
                        **accounts,
                    }
                )
            )
    finally:
        await dispose_audit_engine()
        await dispose_engine()
    return 0


#: Only accounts `e2e-fixture` creates can be retired by `e2e-retire`.
E2E_ACCOUNT = re.compile(r"e2e-(owner|reviewer)-[0-9a-f]{12}@example\.com")


async def e2e_retire(emails: list[str]) -> int:
    """Disable run-unique E2E accounts: clear their password hash so they can
    never sign in again. Rows are kept, because approvals and verified facts
    still name them. Refuses anything that is not an `e2e-fixture` account."""
    from sqlalchemy import update

    from packages.core.db import dispose_engine, init_engine, session_scope
    from packages.core.models import User

    refused = [email for email in emails if not E2E_ACCOUNT.fullmatch(email)]
    if refused or not emails:
        raise DemoGuardError(f"refusing to retire non-E2E accounts: {refused or 'none given'}")
    settings = check_settings()
    await init_engine(settings)
    try:
        async with session_scope() as session:
            await check_database(await session.connection(), require_marker=True)
            result = await session.execute(
                update(User).where(User.email.in_(emails)).values(password_hash=None)
            )
            print(f"retired {result.rowcount} E2E account(s)")
    finally:
        await dispose_engine()
    return 0


def _alert(
    kind: str,
    severity: str,
    subject: str,
    created_at: datetime,
    *,
    resolved: bool = False,
    aggregate: tuple[str, Any] | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {"category": kind, "subject": subject, "synthetic": True}
    if resolved:
        detail["resolved_by"] = "monitor:recovered"
    return {
        "id": _id(f"alert:{subject}"),
        "kind": kind,
        "severity": severity,
        "title": "Synthetic demo incident",
        "detail": detail,
        "aggregate_type": aggregate[0] if aggregate else None,
        "aggregate_id": aggregate[1] if aggregate else None,
        "status": "resolved" if resolved else "open",
        "auto_paused": False,
        "created_at": created_at,
        "resolved_at": created_at + timedelta(minutes=10) if resolved else None,
    }


# ------------------------------------------------------------------ main
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=["write-env", "check", "init", "seed", "e2e-fixture", "e2e-retire"]
    )
    parser.add_argument("emails", nargs="*", help="e2e-retire: the E2E account emails")
    args = parser.parse_args()
    if args.command == "write-env":
        return write_env(ROOT / ".env.demo")
    try:
        if args.command == "check":

            async def _check() -> int:
                engine = await _guarded_engine(require_marker=False)
                await engine.dispose()
                print(f"ok: configured and connected database is {DEMO_DATABASE}")
                return 0

            return asyncio.run(_check())
        if args.command == "init":
            return asyncio.run(init())
        if args.command == "e2e-fixture":
            return asyncio.run(e2e_fixture())
        if args.command == "e2e-retire":
            return asyncio.run(e2e_retire(args.emails))
        return asyncio.run(seed())
    except DemoGuardError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
