"""Authenticated HTTP C3 integration with real PostgreSQL and replayed model calls."""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.deps import revision_client
from apps.api.main import create_app
from packages.agents.llm.runtime import StructuredClient
from packages.agents.resume_optimizer.revision import load_revision_request
from packages.core.auth import create_session
from packages.core.cache import get_redis
from packages.core.db import get_session
from packages.core.models import Application, Approval, ApprovalStatus
from packages.core.services import resume_review
from packages.shared.config import Settings
from packages.shared.ids import uuid7
from tests.integration import test_resume_review as c2
from tests.integration import test_resume_revision as c3
from tests.unit.test_auth_sessions import FakeRedis

inputs = c3.inputs
pytestmark = [pytest.mark.integration, pytest.mark.safety]


@pytest.fixture
async def scenario(session, engine, demo_profile, demo_job, demo_application, inputs, tmp_path):
    _, _, facts = inputs
    app_id = demo_application["id"]
    await c2._ready(session, demo_profile, demo_job, demo_application, facts)
    actor = await c2._actor(session, demo_application)
    state = await c2._start(session, app_id, actor)
    actor_id, approval_id = actor.id, state.approval_id
    model = StructuredClient(Settings(), cassette_dir=tmp_path)
    # Stage exact requests from persisted decision data, then undo the decision.
    savepoint = await session.begin_nested()
    await resume_review.request_changes(
        session, application_id=app_id, actor=actor, note=c3.TIGHTEN
    )
    revision = await load_revision_request(session, app_id)
    await c3._stage_cassettes(
        session, tmp_path, model, revision, bullet_text="Built infrastructure with AWS."
    )
    await savepoint.rollback()
    await session.commit()

    redis = FakeRedis()
    login = await create_session(redis, user_id=actor_id, idle_minutes=60, absolute_hours=24)
    app = create_app()
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def database():
        async with maker() as db:
            try:
                yield db
            except Exception:
                await db.rollback()
                raise

    app.dependency_overrides[get_session] = database
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[revision_client] = lambda: model
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={Settings().session_cookie_name: login.id},
        headers={"X-CSRF-Token": login.csrf_token},
    ) as http:
        yield http, app_id, approval_id, maker, model, revision


async def post(scenario, **body):
    http, app_id, approval_id, *_ = scenario
    return await http.post(
        f"/api/v1/applications/{app_id}/resume-review/request-changes",
        json={"approval_id": str(approval_id), "note": c3.TIGHTEN, **body},
    )


async def test_http_regenerates_and_retries_without_another_version(scenario):
    first = await post(scenario)
    assert first.status_code == 200, first.text
    state = first.json()
    assert state["application_status"] == "resume_ready"
    assert state["resume_version"] == 2
    assert state["can_start_review"] and not state["can_approve"]
    second = await post(scenario)
    assert second.status_code == 200
    assert second.json()["resume_id"] == state["resume_id"]
    _, app_id, approval_id, maker, *_ = scenario
    async with maker() as db:
        assert len(await c3._resumes(db, app_id)) == 2
        approvals = (
            (await db.execute(select(Approval).where(Approval.application_id == app_id)))
            .scalars()
            .all()
        )
        assert len(approvals) == 1
        assert approvals[0].id == approval_id
        assert approvals[0].status == ApprovalStatus.CHANGES_REQUESTED
        assert len(await c2._events(db, app_id, "approval.changes_requested")) == 1
        assert len(await c2._events(db, app_id, "resume.generation.requested")) == 1
        assert not await c2._events(db, app_id, "approval.granted")
    assert (await post(scenario, note="Different instructions")).status_code == 409
    http = scenario[0]
    opened = await http.post(f"/api/v1/applications/{app_id}/resume-review", json={})
    assert opened.status_code == 200
    assert opened.json()["approval_id"] != str(approval_id)
    assert (await post(scenario)).status_code == 409


async def test_http_failure_rolls_back_decision_and_pipeline_then_retries(scenario, monkeypatch):
    from packages.core.services import resume_revision
    from packages.shared.errors import DependencyUnavailableError

    original = resume_revision.persist_resume

    async def fail_after_persistence(*args, **kwargs):
        await original(*args, **kwargs)
        raise DependencyUnavailableError("Simulated persistence boundary failure")

    monkeypatch.setattr(resume_revision, "persist_resume", fail_after_persistence)
    response = await post(scenario)
    assert response.status_code == 503, response.text
    _, app_id, approval_id, maker, *_ = scenario
    async with maker() as db:
        assert (await db.get(Application, app_id)).status == "awaiting_approval"
        assert (await db.get(Approval, approval_id)).status == ApprovalStatus.PENDING
        assert len(await c3._resumes(db, app_id)) == 1
        assert not await c2._events(db, app_id, "approval.changes_requested")
        assert not await c2._events(db, app_id, "resume.generation.requested")
    monkeypatch.setattr(resume_revision, "persist_resume", original)
    assert (await post(scenario)).status_code == 200


async def test_http_verification_failure_stops_and_retry_reuses(scenario, tmp_path):
    _, app_id, _, maker, model, revision = scenario
    async with maker() as db:
        await c3._stage_cassettes(
            db,
            tmp_path,
            model,
            revision,
            bullet_text="Built infrastructure with AWS.",
            verdict="unsupported",
        )
    first = await post(scenario)
    assert first.status_code == 200, first.text
    assert first.json()["application_status"] == "verification_failed"
    assert not first.json()["can_approve"]
    assert not first.json()["can_start_review"]
    second = await post(scenario)
    assert second.status_code == 200
    assert second.json()["resume_id"] == first.json()["resume_id"]


@pytest.mark.parametrize("case", ["csrf", "auth", "stale", "empty", "identity", "missing_id"])
async def test_http_guards_do_not_start_generation(scenario, case):
    http, app_id, _, maker, *_ = scenario
    kwargs = {}
    expected = 422
    if case == "csrf":
        http.headers["X-CSRF-Token"] = "invalid"
        expected = 403
    elif case == "auth":
        http.cookies.clear()
        expected = 401
    elif case == "stale":
        kwargs["approval_id"] = str(uuid7())
        expected = 409
    elif case == "empty":
        kwargs["note"] = "   "
    elif case == "identity":
        kwargs["decided_by"] = str(uuid7())
    elif case == "missing_id":
        response = await http.post(
            f"/api/v1/applications/{app_id}/resume-review/request-changes",
            json={"note": c3.TIGHTEN},
        )
    if case != "missing_id":
        response = await post(scenario, **kwargs)
    assert response.status_code == expected, response.text
    async with maker() as db:
        assert (await db.get(Application, app_id)).status == "awaiting_approval"
        assert not await c2._events(db, app_id, "resume.generation.requested")


async def test_concurrent_http_retries_create_one_revision(scenario):
    import asyncio

    responses = await asyncio.gather(post(scenario), post(scenario))
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json()["resume_id"] == responses[1].json()["resume_id"]
    _, app_id, _, maker, *_ = scenario
    async with maker() as db:
        assert len(await c3._resumes(db, app_id)) == 2
        assert len(await c2._events(db, app_id, "resume.generation.requested")) == 1


async def test_preexisting_committed_change_request_can_resume(scenario):
    _, app_id, approval_id, maker, *_ = scenario
    async with maker() as db:
        from packages.core.models import User

        application = await db.get(Application, app_id)
        from packages.core.models import Profile

        profile = await db.get(Profile, application.profile_id)
        actor = await db.get(User, profile.user_id)
        await resume_review.request_changes(db, application_id=app_id, actor=actor, note=c3.TIGHTEN)
        await db.commit()
    assert (await post(scenario)).status_code == 200
