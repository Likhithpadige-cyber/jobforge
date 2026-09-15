"""Fixtures for the Phase 2 database integration tests.

Every test here needs a real PostgreSQL: the schema uses generated columns,
partial and expression indexes, plpgsql triggers, enum types, arrays and
role-based grants. None of that exists on SQLite, and weakening a constraint
so a SQLite test can pass would defeat the point of having the constraint.
Tests skip cleanly when PostgreSQL is unreachable.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from urllib.parse import quote
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from packages.shared.config import get_settings
from packages.shared.ids import uuid7

requires_postgres = pytest.mark.skipif(
    os.getenv("SKIP_PG_TESTS") == "1",
    reason="SKIP_PG_TESTS=1",
)


def _skip_or_fail(reason: str) -> None:
    """Skip, unless the run demands that security actually be verified.

    There are four ways a security test can fail to execute: PostgreSQL
    unreachable, migrations not applied, app_agent unable to connect, or the
    role missing entirely. Previously only the third honoured
    REQUIRE_SECURITY_TESTS, so `make test-security` could still report a
    green summary while proving nothing. Every path goes through here now.
    """
    if os.getenv("REQUIRE_SECURITY_TESTS") == "1":
        pytest.fail(
            f"{reason}\n"
            "REQUIRE_SECURITY_TESTS=1 is set, so a skipped security test is a "
            "failed one. Diagnose with: make diagnose-roles"
        )
    pytest.skip(reason)


def _url(role: str | None = None) -> str:
    """Build a DSN for `role`, reusing the host/port/database of the owner DSN.

    Two subtleties that bite the moment a real password is used:

    * the owner password may contain '@', so the host is found with
      rpartition, not partition - partition would split at the password's
      own '@' and produce a nonsense host;
    * the role password is percent-encoded, so ':', '/' and '@' in a
      generated password do not corrupt the URL.

    scripts/diagnose_roles.py::agent_url mirrors this exactly, so if the
    diagnostics connect and the fixture does not, the fault is in the fixture.
    """
    settings = get_settings()
    url = str(settings.database_url)
    if role is None:
        return url
    password = os.getenv(f"{role.upper()}_PASSWORD", role)
    prefix, _, rest = url.partition("://")
    _, _, hostpart = rest.rpartition("@")
    return f"{prefix}://{quote(role, safe='')}:{quote(password, safe='')}@{hostpart}"


@pytest_asyncio.fixture
async def engine(postgres_available: bool):  # type: ignore[no-untyped-def]
    if not postgres_available:
        _skip_or_fail("PostgreSQL not reachable - start it with `make dev`")
    eng = create_async_engine(_url(), poolclass=None, future=True)
    try:
        async with eng.connect() as conn:
            revision = await conn.execute(text("SELECT version_num FROM alembic_version"))
            if revision.scalar_one_or_none() is None:
                _skip_or_fail("migrations not applied - run `make migrate`")
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:  # type: ignore[no-untyped-def]
    """A session whose work is always rolled back, so tests never leak rows."""
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as sess:
        transaction = await sess.begin()
        try:
            yield sess
        finally:
            if transaction.is_active:
                await transaction.rollback()


@pytest_asyncio.fixture
async def agent_engine(postgres_available: bool):  # type: ignore[no-untyped-def]
    """Engine connected as the least-privileged agent role.

    Skips if the role does not exist or cannot log in, which happens when
    migration 0003 was skipped. It never falls back to a privileged
    connection: a security test that silently runs as superuser proves
    nothing and would be worse than no test at all.
    """
    if not postgres_available:
        _skip_or_fail("PostgreSQL not reachable")
    eng = create_async_engine(_url("app_agent"), future=True)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await eng.dispose()
        _skip_or_fail(
            f"cannot connect as app_agent ({type(exc).__name__}: {exc}). "
            "The agent-isolation invariants are UNVERIFIED."
        )
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def demo_user(session: AsyncSession):  # type: ignore[no-untyped-def]
    """Demo user. Not a real person and not real contact details."""
    user_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO users (id, email, role, created_at, updated_at) "
            "VALUES (:id, :email, 'owner', now(), now())"
        ),
        {"id": user_id, "email": f"demo-{uuid4().hex[:8]}@example.com"},
    )
    await session.flush()
    return user_id


@pytest_asyncio.fixture
async def demo_profile(session: AsyncSession, demo_user):  # type: ignore[no-untyped-def]
    """Demo profile with clearly fictional data."""
    profile_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO profiles (id, user_id, full_name, headline, "
            "location_city, target_roles, links, created_at, updated_at) "
            "VALUES (:id, :user_id, 'Demo Candidate', 'Demo headline', "
            "'Example City', ARRAY['Cloud Engineer'], '{}'::jsonb, now(), now())"
        ),
        {"id": profile_id, "user_id": demo_user},
    )
    await session.flush()
    return profile_id


@pytest_asyncio.fixture
async def demo_experience(session: AsyncSession, demo_profile):  # type: ignore[no-untyped-def]
    experience_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO experiences (id, profile_id, company_name, title, "
            "start_date, end_date, description, created_at, updated_at) "
            "VALUES (:id, :profile_id, 'Example Cloud Corp', 'Demo Engineer', "
            "DATE '2024-01-01', NULL, 'Did demo things.', now(), now())"
        ),
        {"id": experience_id, "profile_id": demo_profile},
    )
    await session.flush()
    return experience_id


@pytest_asyncio.fixture
async def verified_fact(session: AsyncSession, demo_profile, demo_experience, demo_user):  # type: ignore[no-untyped-def]
    fact_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO profile_facts (id, profile_id, fact_type, text, structured, "
            "source_table, source_id, status, verified_by, verified_at, "
            "created_at, updated_at) "
            "VALUES (:id, :profile_id, 'experience', 'Demo Engineer at Example Cloud "
            "Corp', '{\"numerals\": []}'::jsonb, 'experiences', :source_id, 'verified', "
            ":verified_by, now(), now(), now())"
        ),
        {
            "id": fact_id,
            "profile_id": demo_profile,
            "source_id": demo_experience,
            "verified_by": demo_user,
        },
    )
    await session.flush()
    return fact_id


@pytest_asyncio.fixture
async def draft_fact(session: AsyncSession, demo_profile, demo_experience):  # type: ignore[no-untyped-def]
    fact_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO profile_facts (id, profile_id, fact_type, text, "
            "source_table, source_id, status, created_at, updated_at) "
            "VALUES (:id, :profile_id, 'skill', 'Unverified demo claim', "
            "'experiences', :source_id, 'draft', now(), now())"
        ),
        {"id": fact_id, "profile_id": demo_profile, "source_id": demo_experience},
    )
    await session.flush()
    return fact_id


@pytest_asyncio.fixture
async def demo_site_policy(session: AsyncSession):  # type: ignore[no-untyped-def]
    policy_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO site_policies (id, domain, created_at, updated_at) "
            "VALUES (:id, :domain, now(), now())"
        ),
        {"id": policy_id, "domain": f"jobs-{uuid4().hex[:8]}.example.invalid"},
    )
    await session.flush()
    return policy_id


@pytest_asyncio.fixture
async def demo_job(session: AsyncSession):  # type: ignore[no-untyped-def]
    source_id, company_id, job_id = uuid7(), uuid7(), uuid7()
    suffix = uuid4().hex[:8]
    await session.execute(
        text(
            "INSERT INTO sources (id, name, slug, domain, created_at, updated_at) "
            "VALUES (:id, 'Demo Source', :slug, 'example.invalid', now(), now())"
        ),
        {"id": source_id, "slug": f"demo-{suffix}"},
    )
    await session.execute(
        text(
            "INSERT INTO companies (id, name, normalized_name, created_at, updated_at) "
            "VALUES (:id, 'Example Cloud Corp', :norm, now(), now())"
        ),
        {"id": company_id, "norm": f"example-cloud-corp-{suffix}"},
    )
    await session.execute(
        text(
            "INSERT INTO jobs (id, source_id, company_id, url, title, "
            "created_at, updated_at) VALUES (:id, :source_id, :company_id, "
            "'https://example.invalid/job', 'Demo Cloud Engineer', now(), now())"
        ),
        {"id": job_id, "source_id": source_id, "company_id": company_id},
    )
    await session.flush()
    return job_id


@pytest_asyncio.fixture
async def demo_application(session: AsyncSession, demo_profile, demo_job, demo_user):  # type: ignore[no-untyped-def]
    application_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO applications (id, job_id, profile_id, status, "
            "created_at, updated_at) VALUES (:id, :job_id, :profile_id, "
            "'created', now(), now())"
        ),
        {"id": application_id, "job_id": demo_job, "profile_id": demo_profile},
    )
    await session.flush()
    return {"id": application_id, "user_id": demo_user, "profile_id": demo_profile}


@pytest_asyncio.fixture
async def pending_approval(session: AsyncSession, demo_application):  # type: ignore[no-untyped-def]
    approval_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO approvals (id, application_id, kind, status, "
            "subject_snapshot, requested_at, created_at, updated_at) "
            "VALUES (:id, :app, 'application', 'pending', '{}'::jsonb, "
            "now(), now(), now())"
        ),
        {"id": approval_id, "app": demo_application["id"]},
    )
    await session.flush()
    return approval_id


@pytest_asyncio.fixture
async def demo_resume(session: AsyncSession, demo_profile, demo_application):  # type: ignore[no-untyped-def]
    resume_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO resumes (id, profile_id, application_id, kind, content, "
            "created_at, updated_at) VALUES (:id, :profile_id, :app, 'tailored', "
            "'{\"sections\": []}'::jsonb, now(), now())"
        ),
        {"id": resume_id, "profile_id": demo_profile, "app": demo_application["id"]},
    )
    await session.flush()
    return resume_id


@pytest_asyncio.fixture
async def committed_fact(engine):  # type: ignore[no-untyped-def]
    """A verified fact that is COMMITTED, so a second connection can see it.

    The `session` fixture rolls everything back, which is right for tests that
    use one connection. But the agent-isolation tests open a SEPARATE
    connection as app_agent, and an uncommitted row is invisible across
    connections. Without this, the two positive-path tests
    (`test_agent_can_read_facts` and the usage-counter test) fail with
    NoResultFound rather than proving anything.

    Cleaned up by deleting the user, which cascades to the profile and its
    facts. Audit rows are intentionally left behind: audit_logs is append-only
    and no role may delete from it.
    """
    user_id, profile_id, experience_id, fact_id = uuid7(), uuid7(), uuid7(), uuid7()
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with maker() as setup:
        await setup.execute(
            text(
                "INSERT INTO users (id, email, role, created_at, updated_at) "
                "VALUES (:id, :email, 'owner', now(), now())"
            ),
            {"id": user_id, "email": f"agent-test-{uuid4().hex[:8]}@example.invalid"},
        )
        await setup.execute(
            text(
                "INSERT INTO profiles (id, user_id, full_name, target_roles, links, "
                "created_at, updated_at) VALUES (:id, :user_id, 'Committed Demo', "
                "ARRAY['Cloud Engineer'], '{}'::jsonb, now(), now())"
            ),
            {"id": profile_id, "user_id": user_id},
        )
        await setup.execute(
            text(
                "INSERT INTO experiences (id, profile_id, company_name, title, "
                "start_date, created_at, updated_at) VALUES (:id, :profile_id, "
                "'Example Cloud Corp', 'Demo Engineer', DATE '2024-01-01', now(), now())"
            ),
            {"id": experience_id, "profile_id": profile_id},
        )
        await setup.execute(
            text(
                "INSERT INTO profile_facts (id, profile_id, fact_type, text, "
                "structured, source_table, source_id, status, verified_by, "
                "verified_at, created_at, updated_at) VALUES (:id, :profile_id, "
                "'experience', 'Committed demo fact', '{}'::jsonb, 'experiences', "
                ":source_id, 'verified', :user_id, now(), now(), now())"
            ),
            {"id": fact_id, "profile_id": profile_id,
             "source_id": experience_id, "user_id": user_id},
        )
        await setup.commit()

    try:
        yield fact_id
    finally:
        async with maker() as teardown:
            # Order matters. profile_facts has TWO foreign keys into this
            # graph, with different referential actions:
            #
            #   profile_id  -> profiles ON DELETE CASCADE
            #   verified_by -> users    ON DELETE SET NULL
            #
            # Deleting the user first fires both. The SET NULL nulls
            # verified_by on a fact row that still exists, and
            # ck_profile_facts_verified_requires_actor is then evaluated on a
            # row that is status='verified' with no verifier - so the delete
            # is rejected. The constraint is doing exactly its job: a verified
            # fact must always name the human who verified it.
            #
            # Deleting the profile first removes the facts via CASCADE, so by
            # the time the user is deleted nothing references it.
            await teardown.execute(
                text("DELETE FROM profiles WHERE user_id = :id"), {"id": user_id}
            )
            await teardown.execute(
                text("DELETE FROM users WHERE id = :id"), {"id": user_id}
            )
            await teardown.commit()

@pytest_asyncio.fixture
async def db_app_client(app_client):
    """ASGI client with application lifespan and database initialization."""
    yield app_client
