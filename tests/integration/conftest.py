"""Fixtures for the Phase 2 database integration tests.

Every test here needs a real PostgreSQL: the schema uses generated columns,
partial and expression indexes, plpgsql triggers, enum types, arrays and
role-based grants. None of that exists on SQLite, and weakening a constraint
so a SQLite test can pass would defeat the point of having the constraint.
Tests skip cleanly when PostgreSQL is unreachable.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from urllib.parse import quote
from uuid import UUID, uuid4

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
async def audit_boundary_engine(engine):  # type: ignore[no-untyped-def]
    """The process-wide audit engine, pointed at the test database.

    Depends on `engine` only to inherit its skip conditions (PostgreSQL
    reachable, migrations applied). It shares no connection with it - that
    separation is the property the audit boundary exists for, so a fixture
    that reused the domain engine would quietly disprove nothing.

    Uses the documented fallback DSN (no `app_audit` role) so the durability
    tests run on an unprovisioned machine; `test_audit_boundary.py` covers the
    provisioned shape separately and skips when the role is absent.
    """
    from packages.core.db.audit_session import dispose_audit_engine, init_audit_engine
    from packages.shared.config import Settings

    await dispose_audit_engine()
    await init_audit_engine(Settings(audit_database_url=_url()))
    try:
        yield
    finally:
        await dispose_audit_engine()


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
    except Exception as exc:
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
async def audit_engine(postgres_available: bool):  # type: ignore[no-untyped-def]
    """Engine connected as the inference-audit role (0005, I.6).

    Same contract as `agent_engine`: it never falls back to a privileged
    connection, because a privilege test that silently runs as the owner
    proves the opposite of what it claims. Skips - or fails, under
    REQUIRE_SECURITY_TESTS=1 - if the role is missing or cannot log in, which
    happens when 0005's grants were skipped or `make db-roles` has not run
    since 0005 was applied.
    """
    if not postgres_available:
        _skip_or_fail("PostgreSQL not reachable")
    eng = create_async_engine(_url("app_audit"), future=True)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        await eng.dispose()
        _skip_or_fail(
            f"cannot connect as app_audit ({type(exc).__name__}: {exc}). "
            "The audit-containment invariants are UNVERIFIED."
        )
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def revision_attempt(  # type: ignore[no-untyped-def]
    session: AsyncSession, demo_application, demo_resume, pending_approval
):
    """One accepted revision attempt, bound to a real source cycle.

    Inserted with raw SQL like the other fixtures here, so the row exists even
    when the ORM mapping is what is under test.
    """
    attempt_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO resume_revision_attempts ("
            "  id, application_id, source_resume_id, source_resume_version,"
            "  source_approval_id, instructions_snapshot, instructions_digest,"
            "  requested_by, status"
            ") VALUES ("
            "  :id, :app, :resume, 1, :approval, 'Tighten the summary.',"
            "  repeat('a', 64), :user, 'accepted'"
            ")"
        ),
        {
            "id": attempt_id,
            "app": demo_application["id"],
            "resume": demo_resume,
            "approval": pending_approval,
            "user": demo_application["user_id"],
        },
    )
    await session.flush()
    return {
        "id": attempt_id,
        "application_id": demo_application["id"],
        "source_resume_id": demo_resume,
        "source_approval_id": pending_approval,
        "user_id": demo_application["user_id"],
    }


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
async def approved_resume_fixtures(  # type: ignore[no-untyped-def]
    session: AsyncSession, demo_profile, demo_application, demo_user, demo_job
):
    """Four resumes on one application, covering every M8 precondition.

    Built with SQL rather than through the review service on purpose: this
    fixture has to produce states the service would refuse to create, such as
    an approval whose subject snapshot names a version that has since been
    superseded. Driving the service would make the fixture agree with the
    code by construction and prove nothing.

    Synthetic content only.

      approved_resume_id    v2, verification passed, granted approval whose
                            snapshot names v2 - the only renderable one
      unapproved_resume_id  v3, passed, no approval at all
      failed_resume_id      v4, verification failed
      superseded_resume_id  v5, passed, with a granted approval whose snapshot
                            still names v2, so it must not authorise v5
    """
    application_id = demo_application["id"]
    lines = {
        2: ["Ran a synthetic migration.", "Reduced a synthetic number."],
        3: ["A third synthetic line."],
        4: ["A fourth synthetic line."],
        5: ["A fifth synthetic line."],
    }

    def content(version: int) -> str:
        entries = {
            str(i): {"text": t, "fact_ids": [], "numerals_used": [], "order": i}
            for i, t in enumerate(lines[version])
        }
        return json.dumps({"experience": entries})

    ids: dict[int, UUID] = {}
    for version, status in ((2, "passed"), (3, "passed"), (4, "failed"), (5, "passed")):
        resume_id = uuid7()
        ids[version] = resume_id
        await session.execute(
            text(
                "INSERT INTO resumes (id, profile_id, application_id, kind, version, "
                "content, verification_status, created_at, updated_at) VALUES "
                "(:id, :profile_id, :application_id, 'tailored', :version, "
                "CAST(:content AS jsonb), :status, now(), now())"
            ),
            {
                "id": resume_id,
                "profile_id": demo_profile,
                "application_id": application_id,
                "version": version,
                "content": content(version),
                "status": status,
            },
        )

    def snapshot(resume_id: UUID, version: int) -> str:
        return json.dumps(
            {
                "resume_id": str(resume_id),
                "resume_version": version,
                "resume_kind": "tailored",
                "verification_status": "passed",
                "claim_count": len(lines[version]),
                "application_id": str(application_id),
                "application_status": "approved",
                "job_id": str(demo_job),
                "model": None,
                "prompt_version": None,
            }
        )

    approval_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO approvals (id, application_id, kind, status, "
            "subject_snapshot, requested_at, decided_at, decided_by, "
            "created_at, updated_at) VALUES (:id, :application_id, 'resume', "
            "'approved', CAST(:snapshot AS jsonb), now(), now(), :user, "
            "now(), now())"
        ),
        {
            "id": approval_id,
            "application_id": application_id,
            "snapshot": snapshot(ids[2], 2),
            "user": demo_user,
        },
    )
    await session.flush()

    # Match approve()'s write order: the trigger must see the human-decided
    # approval before the application enters approved, with the same approver.
    await session.execute(
        text(
            "UPDATE applications SET status = 'approved', approved_by = :user, "
            "approved_at = now() WHERE id = :id"
        ),
        {"id": application_id, "user": demo_user},
    )
    await session.flush()

    return {
        "application_id": application_id,
        "approval_id": approval_id,
        "approved_resume_id": ids[2],
        "approved_version": 2,
        "unapproved_resume_id": ids[3],
        "failed_resume_id": ids[4],
        "superseded_resume_id": ids[5],
        "content_lines": list(lines[2]),
        "content_words": ["synthetic", "migration"],
    }


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
async def api_sessions(app_client, engine):  # type: ignore[no-untyped-def]
    """Factory for signed-in API clients, one per role.

        owner = await api_sessions("owner")
        reviewer = await api_sessions("reviewer")

    Everything about the session is real: `create_session` writes it,
    `load_session` reads it back, the cookie names and CSRF token come from
    settings. Only the *store* is the `FakeRedis` the auth tests already use,
    and one instance is shared so several roles can be signed in at once.

    Each call creates its own user, committed on its own connection and
    deleted in teardown. Borrowing the `session` fixture would not work: it
    rolls back by design, and a user that exists only inside an uncommitted
    transaction is invisible to the connection serving the request, which
    would 401 for a reason unrelated to the test.

    The CSRF header is preset, because callers of this fixture are exercising
    endpoints rather than the CSRF check. A test that wants to prove CSRF
    pops the header; a test that wants to prove the authentication gate uses
    an unauthenticated client instead.
    """
    import httpx

    from apps.api.deps import CSRF_HEADER
    from packages.core.auth import create_session
    from packages.core.cache import redis as redis_module
    from packages.shared.config import get_settings
    from tests.unit.test_rate_limit import FakeLimiterRedis

    settings = get_settings()
    # One fake store for every role signed in during this test. Swapping the
    # module global rather than overriding the FastAPI dependency keeps this
    # working for extra clients built over the same app.
    #
    # `FakeLimiterRedis` is `FakeRedis` plus the counter operations M3 needs;
    # the session behaviour is unchanged. Tests reach it through
    # `redis_module._client` when they need to drive its clock.
    previous = redis_module._client
    redis = FakeLimiterRedis()
    redis_module._client = redis  # type: ignore[assignment]

    # The transport already holds the app that `app_client` drives; extra
    # clients must target that same instance, not a fresh one.
    app = app_client._transport.app  # type: ignore[attr-defined]
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    created_users: list[object] = []
    created_clients: list[object] = []

    async def make(role: str = "owner"):  # type: ignore[no-untyped-def]
        user_id = uuid7()
        async with maker() as setup:
            await setup.execute(
                text(
                    "INSERT INTO users (id, email, role, created_at, updated_at) "
                    "VALUES (:id, :email, :role, now(), now())"
                ),
                {
                    "id": user_id,
                    "email": f"{role}-{uuid4().hex[:12]}@example.invalid",
                    "role": role,
                },
            )
            await setup.commit()
        created_users.append(user_id)

        live = await create_session(
            redis,
            user_id=user_id,
            idle_minutes=settings.session_idle_timeout_minutes,
            absolute_hours=settings.session_absolute_timeout_hours,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        client.cookies.set(settings.session_cookie_name, live.id)
        client.cookies.set(settings.csrf_cookie_name, live.csrf_token)
        client.headers[CSRF_HEADER] = live.csrf_token
        created_clients.append(client)
        return client

    try:
        yield make
    finally:
        redis_module._client = previous
        for client in created_clients:
            await client.aclose()  # type: ignore[attr-defined]
        async with maker() as teardown:
            for user_id in created_users:
                await teardown.execute(
                    text("DELETE FROM users WHERE id = :id"), {"id": user_id}
                )
            await teardown.commit()


@pytest_asyncio.fixture
async def db_app_client(api_sessions):  # type: ignore[no-untyped-def]
    """An owner-authenticated client. The default for endpoint tests.

    Kept as its own fixture so existing HTTP integration tests read the same
    as they did before roles existed; `owner` is the role those tests have
    always implicitly assumed.
    """
    yield await api_sessions("owner")
