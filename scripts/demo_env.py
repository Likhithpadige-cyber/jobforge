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
        await _seed_accounts_and_incidents(engine)
    finally:
        await engine.dispose()
    print("demo seed complete")
    return 0


def _id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"jobforge-demo:{name}")


async def _seed_accounts_and_incidents(engine: Any) -> None:
    from typing import cast

    from sqlalchemy import Table, func, select, text
    from sqlalchemy.dialects.postgresql import insert
    from sqlalchemy.ext.asyncio import AsyncSession

    from packages.core.auth import hash_password
    from packages.core.events.envelope import EventEnvelope
    from packages.core.events.guardrail_alerts import open_guardrail_alert
    from packages.core.models import Alert, Event, ProcessedEvent

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
        print(f"accounts: {OWNER_EMAIL} (owner), {REVIEWER_EMAIL} (reviewer)")
        print(f"alerts in demo database: {total}")


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
    parser.add_argument("command", choices=["write-env", "check", "init", "seed"])
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
        return asyncio.run(seed())
    except DemoGuardError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
