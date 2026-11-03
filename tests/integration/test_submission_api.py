"""M11 Phase 2: POST /api/v1/applications/{application_id}/submit integration tests.

Verifies:
- 401 unauthenticated
- 403 reviewer forbidden (owner only)
- 403 missing CSRF token
- 404 nonexistent application (non-enumeration)
- 404 other user's application (tenant boundary)
- 409 application not submission_ready
- 409 submission switches disabled (either or both)
- 422 destination email domain mismatch
- 202 accepted on valid preconditions:
    - returns SubmitResponse
    - application advances to SUBMITTING
    - submission record has attempt 1, PENDING
    - submission.requested event emitted with actor=human, destination_email
- 409 repeated submit returns conflict (already SUBMITTING)
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.main import create_app
from packages.core.auth import create_session
from packages.core.cache import get_redis
from packages.core.db import get_session
from packages.core.models import (
    Application,
    ApplicationStatus,
    Event,
    Job,
    Profile,
    Submission,
    SubmissionResult,
    User,
)
from packages.core.services.settings import (
    SUBMISSION_ENABLED_KEY,
    set_submission_dispatch_enabled,
)
from packages.services.rendering.store import FilesystemArtifactStore
from packages.shared.config import get_settings
from packages.shared.ids import uuid7
from tests.integration import test_submission_readiness as sr_tests
from tests.integration import test_tailoring_context
from tests.unit.test_auth_sessions import FakeRedis

inputs = test_tailoring_context.inputs

pytestmark = [
    pytest.mark.integration,
    pytest.mark.safety,
    pytest.mark.usefixtures("audit_boundary_engine"),
]

DOMAIN = "example.invalid"


@pytest.fixture
def store(tmp_path):
    return FilesystemArtifactStore(tmp_path / "artifacts")


@pytest_asyncio.fixture(autouse=True)
async def _clean_all(engine):
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def _clean() -> None:
        async with maker() as conn:
            await conn.execute(
                text("DELETE FROM submissions"),
            )
            await conn.execute(
                text("DELETE FROM site_policies WHERE domain = :domain"),
                {"domain": DOMAIN},
            )
            await conn.execute(
                text("DELETE FROM settings WHERE key = :key"),
                {"key": SUBMISSION_ENABLED_KEY},
            )
            await conn.commit()

    await _clean()
    yield
    await _clean()


@pytest.fixture
def client_factory(engine):
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    app = create_app()

    async def database():
        async with maker() as db:
            try:
                yield db
            except Exception:
                await db.rollback()
                raise

    redis = FakeRedis()
    app.dependency_overrides[get_session] = database
    app.dependency_overrides[get_redis] = lambda: redis

    async def _make_client(user_id: UUID | None = None, csrf: bool = True):
        headers: dict[str, str] = {}
        cookies: dict[str, str] = {}
        settings = get_settings()

        if user_id is not None:
            login = await create_session(redis, user_id=user_id, idle_minutes=60, absolute_hours=24)
            cookies[settings.session_cookie_name] = login.id
            cookies[settings.csrf_cookie_name] = login.csrf_token
            if csrf:
                headers["X-CSRF-Token"] = login.csrf_token

        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies=cookies,
            headers=headers,
        )

    return _make_client


# ---------------------------------------------------------------------------
# Authorization & Validation boundaries
# ---------------------------------------------------------------------------


async def test_unauthenticated_submit_fails(client_factory) -> None:
    client = await client_factory(None)
    async with client:
        res = await client.post(f"/api/v1/applications/{uuid7()}/submit", json={})
    assert res.status_code == 401


async def test_reviewer_submit_forbidden(client_factory, session) -> None:
    reviewer = User(
        id=uuid7(),
        email=f"reviewer-{uuid7().hex}@example.invalid",
        role="reviewer",
    )
    session.add(reviewer)
    await session.commit()

    client = await client_factory(reviewer.id)
    async with client:
        res = await client.post(f"/api/v1/applications/{uuid7()}/submit", json={})
    assert res.status_code == 403


async def test_submit_requires_csrf(client_factory, demo_user) -> None:
    client = await client_factory(demo_user, csrf=False)
    async with client:
        res = await client.post(f"/api/v1/applications/{uuid7()}/submit", json={})
    assert res.status_code == 403


async def test_submit_nonexistent_application_404(client_factory, session, demo_user) -> None:
    await session.commit()
    client = await client_factory(demo_user)
    async with client:
        res = await client.post(f"/api/v1/applications/{uuid7()}/submit", json={})
    assert res.status_code == 404


async def test_submit_other_users_application_404(
    client_factory, session, demo_profile, demo_job, demo_user
) -> None:
    """Non-enumeration: submitting another user's application answers 404, not 403."""
    other_user = User(
        id=uuid7(),
        email=f"other-{uuid7().hex}@example.invalid",
        role="owner",
    )
    session.add(other_user)
    other_profile = Profile(
        id=uuid7(),
        user_id=other_user.id,
        full_name="Other User",
        target_roles=["Engineer"],
    )
    session.add(other_profile)
    other_app = Application(
        id=uuid7(),
        profile_id=other_profile.id,
        job_id=demo_job,
        status=ApplicationStatus.CREATED,
    )
    session.add(other_app)
    await session.commit()

    # Logged in as demo_user, trying to submit other_app
    client = await client_factory(demo_user)
    async with client:
        res = await client.post(f"/api/v1/applications/{other_app.id}/submit", json={})
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Preconditions via API
# ---------------------------------------------------------------------------


async def test_submit_not_ready_status_409(
    client_factory, session, demo_application, demo_user
) -> None:
    """Application in AWAITING_APPROVAL returns 409 Conflict."""
    await session.commit()
    client = await client_factory(demo_user)
    app_id = demo_application["id"]
    async with client:
        res = await client.post(f"/api/v1/applications/{app_id}/submit", json={})
    assert res.status_code == 409
    assert "not submission_ready" in res.json()["detail"]


async def test_submit_switches_disabled_409(
    client_factory,
    session,
    demo_profile,
    demo_job,
    demo_application,
    demo_user,
    inputs,
    tmp_path,
    store,
    monkeypatch,
) -> None:
    """When submission_mode is enabled but settings.submission_enabled is False, returns 409."""
    monkeypatch.setenv("SUBMISSION_MODE", "enabled")
    get_settings.cache_clear()

    await session.execute(
        update(Job).where(Job.id == demo_job).values(url=f"mailto:hiring@{DOMAIN}")
    )

    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await sr_tests._allow_via_email(session)
    await sr_tests._approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    outcome = await sr_tests._trigger_readiness(session, app_id, store=store)
    assert outcome == "completed"
    await session.commit()

    client = await client_factory(demo_user)
    async with client:
        res = await client.post(
            f"/api/v1/applications/{app_id}/submit",
            json={},
        )
    assert res.status_code == 409
    assert "disabled" in res.json()["detail"].lower()


async def test_submit_rejects_caller_supplied_payload_422(
    client_factory,
    session,
    demo_profile,
    demo_job,
    demo_application,
    demo_user,
    inputs,
    tmp_path,
    store,
    monkeypatch,
) -> None:
    """Submit endpoint forbids caller from supplying destination_email or any extra body fields."""
    monkeypatch.setenv("SUBMISSION_MODE", "enabled")
    get_settings.cache_clear()

    owner_user = await session.get(User, demo_user)
    assert owner_user is not None
    await set_submission_dispatch_enabled(session, True, actor=owner_user)

    await session.execute(
        update(Job).where(Job.id == demo_job).values(url=f"mailto:hiring@{DOMAIN}")
    )

    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await sr_tests._allow_via_email(session)
    await sr_tests._approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    await sr_tests._trigger_readiness(session, app_id, store=store)
    await session.commit()

    client = await client_factory(demo_user)
    async with client:
        res = await client.post(
            f"/api/v1/applications/{app_id}/submit",
            json={"destination_email": "attacker@evil.com"},
        )
    assert res.status_code == 422


async def test_submit_job_without_mailto_url_422(
    client_factory,
    session,
    demo_profile,
    demo_job,
    demo_application,
    demo_user,
    inputs,
    tmp_path,
    store,
    monkeypatch,
) -> None:
    """When job.url is not mailto:, submit returns 422 (cannot derive destination)."""
    monkeypatch.setenv("SUBMISSION_MODE", "enabled")
    get_settings.cache_clear()

    owner_user = await session.get(User, demo_user)
    assert owner_user is not None
    await set_submission_dispatch_enabled(session, True, actor=owner_user)

    # demo_job has https:// url by default, not mailto:
    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await sr_tests._allow_via_email(session)
    await sr_tests._approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    await sr_tests._trigger_readiness(session, app_id, store=store)
    await session.commit()

    client = await client_factory(demo_user)
    async with client:
        res = await client.post(
            f"/api/v1/applications/{app_id}/submit",
            json={},
        )
    assert res.status_code == 422
    assert "mailto:" in res.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Successful Submit Acceptance (202 Accepted)
# ---------------------------------------------------------------------------


async def test_submit_successful_acceptance_and_replay_409(
    client_factory,
    session,
    demo_profile,
    demo_job,
    demo_application,
    demo_user,
    inputs,
    tmp_path,
    store,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUBMISSION_MODE", "enabled")
    get_settings.cache_clear()

    owner_user = await session.get(User, demo_user)
    assert owner_user is not None
    await set_submission_dispatch_enabled(session, True, actor=owner_user)

    await session.execute(
        update(Job).where(Job.id == demo_job).values(url=f"mailto:hiring@{DOMAIN}")
    )

    _base, _evaluation, facts = inputs
    app_id = demo_application["id"]
    await sr_tests._allow_via_email(session)
    await sr_tests._approve_everything(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        facts=facts,
        store=store,
        tmp_path=tmp_path,
    )
    await sr_tests._trigger_readiness(session, app_id, store=store)
    await session.commit()

    client = await client_factory(demo_user)
    async with client:
        res = await client.post(
            f"/api/v1/applications/{app_id}/submit",
            json={},
        )
        assert res.status_code == 202
        data = res.json()
        assert data["application_id"] == str(app_id)
        assert data["status"] == "submitting"
        assert data["attempt"] == 1
        assert "submission_id" in data

        # Replay attempt on the now-submitting application should return 409
        res_repeat = await client.post(
            f"/api/v1/applications/{app_id}/submit",
            json={},
        )
        assert res_repeat.status_code == 409

    # Verify database state
    sub_id = UUID(data["submission_id"])
    app = await session.get(Application, app_id)
    assert app is not None
    assert app.status is ApplicationStatus.SUBMITTING

    sub = await session.get(Submission, sub_id)
    assert sub is not None
    assert sub.result is SubmissionResult.PENDING
    assert sub.attempt == 1
    assert sub.started_at is None  # Started at is not set until worker begins transport execution

    # Verify event was emitted
    event = (
        await session.execute(
            select(Event).where(
                Event.type == "submission.requested",
                Event.correlation_id == app_id,
            )
        )
    ).scalar_one()
    assert event.actor_type == "human"
    assert "destination_email" not in event.payload
    assert event.payload["submission_id"] == str(sub_id)
    assert event.payload["application_id"] == str(app_id)
    assert event.payload["requested_by"] == str(owner_user.id)
