"""M13 end-to-end API journeys through the real HTTP stack (opt-in: `-m e2e`).

Each journey signs in through `POST /auth/login` (real password hashing,
real session and CSRF cookies), then drives the same routes the UI calls, in
the order a person would, against a real PostgreSQL. Redis is the in-process
fake every API test uses; no LLM call, email or external request is made,
and both email transports are replaced by tripwires that fail the test if
anything tries to send.

What these journeys do not cover is the browser itself (rendering, focus,
layout); component tests and the documented browser verification do.

Synthetic data throughout. Ordering never relies on a shared database's row
order: every assertion filters to rows this test created.
"""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.deps import get_redis, get_session
from apps.api.main import create_app
from packages.core.auth.passwords import hash_password
from packages.core.models import Application, ApplicationStatus, Submission, User
from packages.core.services.applications import RawQuestion, prepare_application
from packages.core.state_machines.application import transition_application
from packages.services.delivery import email as email_delivery
from packages.shared.config import get_settings
from packages.shared.ids import uuid7
from tests.integration import test_applications as applications_tests
from tests.integration import test_cover_letter_review as clr
from tests.integration import test_review_status_api as review_tests
from tests.integration import test_submission_api as submission_api
from tests.unit.test_auth_sessions import FakeRedis

inputs = review_tests.inputs
store = review_tests.store
_clean_all = submission_api._clean_all

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.usefixtures("audit_boundary_engine", "no_outbound_email"),
]

PASSWORD = "e2e-synthetic-password-1"  # pragma: allowlist secret
PRIVATE_MARKER = "E2E-PRIVATE-APPLICANT-TEXT"
WRONG_PASSWORD = PASSWORD[::-1]


# ----------------------------------------------------------------- harness
@pytest.fixture
def no_outbound_email(monkeypatch):
    """Any attempt to send email during a journey fails the test."""

    async def tripwire(self, message):  # pragma: no cover - must never run
        raise AssertionError("a journey tried to send email")

    monkeypatch.setattr(email_delivery.SMTPEmailTransport, "send", tripwire)
    monkeypatch.setattr(email_delivery.FakeEmailTransport, "send", tripwire)


@pytest.fixture
def stack(engine):
    """The real app with the test database; returns a sign-in function."""
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

    def anonymous() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def sign_in(email: str, password: str = PASSWORD) -> Browser:
        client = anonymous()
        res = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert res.status_code == 200, res.text
        return Browser(client)

    sign_in.anonymous = anonymous  # type: ignore[attr-defined]
    return sign_in


class Browser:
    """A signed-in client that sends the CSRF header the way the web app does."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    def _csrf(self) -> dict[str, str]:
        token = self.client.cookies.get(get_settings().csrf_cookie_name)
        return {"X-CSRF-Token": token} if token else {}

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.client.get(url, **kwargs)

    async def send(self, method: str, url: str, body: object = None) -> httpx.Response:
        return await self.client.request(method, url, json=body, headers=self._csrf())

    async def close(self) -> None:
        await self.client.aclose()


async def _account(session, email_prefix: str, role: str) -> User:
    user = User(
        id=uuid7(),
        email=f"{email_prefix}-{uuid7().hex}@example.com",
        role=role,
        password_hash=hash_password(PASSWORD),
    )
    session.add(user)
    await session.commit()
    return user


async def _with_password(session, user_id: UUID) -> User:
    user = await session.get(User, user_id)
    user.password_hash = hash_password(PASSWORD)
    await session.commit()
    return user


# ------------------------------------------------- authentication and roles
async def test_sign_in_roles_csrf_and_sign_out(stack, session) -> None:
    owner = await _account(session, "owner", "owner")
    reviewer = await _account(session, "reviewer", "reviewer")

    async with stack.anonymous() as anonymous:
        assert (await anonymous.get("/api/v1/approvals")).status_code == 401
        assert (await anonymous.get("/api/v1/agent-runs")).status_code == 401
        wrong = await anonymous.post(
            "/api/v1/auth/login", json={"email": owner.email, "password": WRONG_PASSWORD}
        )
        assert wrong.status_code == 401

    me = await stack(owner.email)
    who = (await me.get("/api/v1/auth/me")).json()
    assert (who["email"], who["role"]) == (owner.email, "owner")

    # A mutation without the CSRF header is refused, even for the owner.
    forged = await me.client.put("/api/v1/settings/submission-enabled", json={"enabled": True})
    assert forged.status_code == 403
    assert (await me.get("/api/v1/settings/submission-gate")).json()["persisted_enabled"] is False

    them = await stack(reviewer.email)
    assert (await them.get("/api/v1/auth/me")).json()["role"] == "reviewer"
    refused = await them.send("PUT", "/api/v1/settings/submission-enabled", {"enabled": True})
    assert refused.status_code == 403

    assert (await me.send("POST", "/api/v1/auth/logout")).status_code in (200, 204)
    assert (await me.get("/api/v1/auth/me")).status_code == 401
    assert (await them.get("/api/v1/auth/me")).status_code == 200  # sessions are independent
    await me.close()
    await them.close()


# ---------------------------------------------------- cover-letter review
async def test_cover_letter_review_journey(
    stack, session, demo_profile, demo_job, demo_application, demo_user, inputs, tmp_path
) -> None:
    _base, _evaluation, facts = inputs
    await clr._ready_for_review(session, demo_profile, demo_job, demo_application, inputs[2])
    await clr._generate_cover_letter(
        session,
        application_id=demo_application["id"],
        demo_profile=demo_profile,
        demo_job=demo_job,
        tmp_path=tmp_path,
        fact_id=str(facts[0].id),
        event_id=uuid7(),
    )
    await session.commit()
    app_id = str(demo_application["id"])
    reviewer = await _account(session, "reviewer", "reviewer")
    await _with_password(session, demo_user)
    owner_email = (await session.get(User, demo_user)).email

    them = await stack(reviewer.email)
    inbox = (await them.get("/api/v1/approvals", params={"application_id": app_id})).json()
    assert {item["kind"] for item in inbox} >= {"cover_letter"}
    content = await them.get(f"/api/v1/applications/{app_id}/cover-letter")
    assert content.status_code == 200, content.text
    assert content.json()["claims"], "the reviewer reads the letter with its cited claims"

    me = await stack(owner_email)
    approved = await me.send(
        "POST",
        f"/api/v1/applications/{app_id}/cover-letter-review/approve",
        {"decision_note": None},
    )
    assert approved.status_code == 200, approved.text
    state = (await them.get(f"/api/v1/applications/{app_id}/cover-letter-review")).json()
    assert state["review_status"] == "approved"
    still_pending = (
        await them.get(
            "/api/v1/approvals", params={"application_id": app_id, "kind": "cover_letter"}
        )
    ).json()
    assert still_pending == []
    await me.close()
    await them.close()


# --------------------------------------------------------- answers package
async def test_answers_package_journey(
    stack, session, demo_profile, demo_job, demo_application, demo_user, inputs
) -> None:
    await applications_tests._ready(session, demo_profile, demo_job, demo_application, inputs)
    app_id = demo_application["id"]
    await prepare_application(
        session,
        application_id=app_id,
        questions=[RawQuestion(question_text="What is your favorite color?", input_type="text")],
    )
    application = await session.get(Application, app_id)
    await transition_application(
        session, application, ApplicationStatus.AWAITING_APPROVAL, actor_type="human"
    )
    await session.commit()
    owner = await _with_password(session, demo_user)
    reviewer = await _account(session, "reviewer", "reviewer")
    base = f"/api/v1/applications/{app_id}"

    them = await stack(reviewer.email)
    questions = (await them.get(f"{base}/questions")).json()
    answer = questions[0]["answer"]
    assert answer["requires_human"] is True and not answer["value"]

    refused = await them.send("POST", f"{base}/package/approve", {})
    assert refused.status_code == 409
    assert "human answer" in refused.json()["detail"]

    saved = await them.send("PATCH", f"{base}/answers/{answer['id']}", {"value": "Synthetic blue"})
    assert saved.status_code == 200, saved.text

    me = await stack(owner.email)
    approved = await me.send("POST", f"{base}/package/approve", {})
    assert approved.status_code == 200, approved.text
    decided = (
        await them.get(
            "/api/v1/approvals",
            params={"application_id": str(app_id), "kind": "answers", "status": "approved"},
        )
    ).json()
    assert len(decided) == 1

    readiness = (await them.get(f"{base}/submission-readiness")).json()
    assert readiness["ready"] is False and readiness["can_submit"] is False
    assert {check["key"] for check in readiness["checks"]} >= {
        "application_status",
        "review_complete",
    }
    await me.close()
    await them.close()


# -------------------------------------------------------- submission safety
async def test_dry_run_submission_is_never_sent(
    stack,
    session,
    demo_profile,
    demo_job,
    demo_application,
    demo_user,
    inputs,
    tmp_path,
    store,
    _clean_all,
) -> None:
    app_id = await review_tests._ready(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        inputs=inputs,
        tmp_path=tmp_path,
        store=store,
    )
    owner = await _with_password(session, demo_user)
    reviewer = await _account(session, "reviewer", "reviewer")
    base = f"/api/v1/applications/{app_id}"

    me = await stack(owner.email)
    them = await stack(reviewer.email)
    readiness = (await me.get(f"{base}/submission-readiness")).json()
    assert readiness["application_status"] == "submission_ready"
    assert readiness["gate"] == {
        "submission_mode": "dry_run",
        "persisted_enabled": False,
        "live": False,
    }
    assert readiness["can_submit"] is False

    # Even with the persisted switch on, dry-run is never live.
    assert (
        await me.send("PUT", "/api/v1/settings/submission-enabled", {"enabled": True})
    ).status_code == 200
    gate = (await me.get("/api/v1/settings/submission-gate")).json()
    assert gate == {"submission_mode": "dry_run", "persisted_enabled": True, "live": False}

    assert (await me.send("POST", f"{base}/submit", {})).status_code == 409
    assert (await them.send("POST", f"{base}/submit", {})).status_code == 403
    assert (
        await me.send("POST", f"{base}/submit", {"recipient": "x@evil.example"})
    ).status_code == 422

    history = (await them.get(f"{base}/submissions")).json()
    assert [item["outcome"] for item in history] == ["pending"]
    assert all(item["started_at"] is None for item in history)
    started = await session.scalar(
        select(Submission.started_at).where(Submission.application_id == app_id)
    )
    assert started is None, "no attempt was ever handed to a sender"
    status = await session.scalar(select(Application.status).where(Application.id == app_id))
    assert status is ApplicationStatus.SUBMISSION_READY
    await me.close()
    await them.close()


# ----------------------------------------------------------- profile, facts
async def test_profile_and_fact_journey(
    stack, session, demo_profile, demo_experience, demo_user
) -> None:
    owner = await _with_password(session, demo_user)
    reviewer = await _account(session, "reviewer", "reviewer")
    me = await stack(owner.email)
    them = await stack(reviewer.email)

    served = (await me.get("/api/v1/profile")).json()
    edit = {
        key: served[key]
        for key in (
            "full_name",
            "headline",
            "email",
            "phone",
            "location_city",
            "location_state",
            "location_country",
            "work_authorization",
            "willing_to_relocate",
            "target_roles",
            "min_salary",
            "max_salary",
            "links",
        )
    }
    edit |= {"email": "e2e-private@example.invalid", "work_authorization": PRIVATE_MARKER}
    saved = await me.send("PUT", "/api/v1/profile", edit | {"version": served["version"]})
    assert saved.status_code == 200, saved.text
    stale = await me.send("PUT", "/api/v1/profile", edit | {"version": served["version"]})
    assert stale.status_code == 409, "an edit from an outdated copy is refused"

    seen = await them.get("/api/v1/profile")
    assert seen.status_code == 200
    assert PRIVATE_MARKER not in seen.text and "e2e-private@example.invalid" not in seen.text
    assert seen.json()["full_name"] == served["full_name"]
    assert (await them.send("PUT", "/api/v1/profile", edit)).status_code == 403

    fact_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO profile_facts (id, profile_id, fact_type, text, structured, "
            "source_table, source_id, status, created_at, updated_at) VALUES (:id, :p, "
            "'skill', 'Runs synthetic infrastructure', '{}'::jsonb, 'experiences', :s, "
            "'draft', now(), now())"
        ),
        {"id": fact_id, "p": demo_profile, "s": demo_experience},
    )
    await session.commit()
    fact = f"/api/v1/profile/facts/{fact_id}"

    assert (await them.send("POST", f"{fact}/verify", {})).status_code == 403
    spoofed = await me.send("POST", f"{fact}/verify", {"verified_by": str(reviewer.id)})
    assert spoofed.status_code == 422
    verified = await me.send("POST", f"{fact}/verify", {})
    assert verified.status_code == 200, verified.text
    assert verified.json()["verified_by"] == str(owner.id)
    forged = await me.send(
        "POST",
        f"{fact}/evidence",
        {"kind": "attestation", "note": "x", "added_by": str(reviewer.id)},
    )
    assert forged.status_code == 422
    assert (await them.send("POST", f"{fact}/retire")).status_code == 403
    retired = await me.send("POST", f"{fact}/retire")
    assert retired.json()["status"] == "retired"
    assert (await me.send("POST", f"{fact}/verify", {})).status_code == 409
    citable = {row["id"] for row in (await them.get("/api/v1/profile/facts/citable")).json()}
    assert str(fact_id) not in citable
    await me.close()
    await them.close()


# ------------------------------------------------- dashboard and activity
async def test_dashboard_sources_and_agent_activity_never_leak(
    stack, session, demo_application
) -> None:
    reviewer = await _account(session, "reviewer", "reviewer")
    run_id, step_id = uuid7(), uuid7()
    await session.execute(
        text(
            "INSERT INTO agent_runs (id, agent, pipeline, status, steps_total, steps_completed, "
            "error_type, error_message, aggregate_type, aggregate_id) VALUES (:id, "
            "'resume_optimizer', 'tailoring', 'failed', 1, 0, 'ModelOutputError', :msg, "
            "'application', :app)"
        ),
        {"id": run_id, "msg": f"Traceback ... {PRIVATE_MARKER}", "app": demo_application["id"]},
    )
    await session.execute(
        text(
            "INSERT INTO agent_run_steps (id, run_id, step_index, step_name, status, output, "
            "guardrail_results) VALUES (:id, :run, 0, 'draft', 'failed', CAST(:out AS jsonb), "
            "CAST(:out AS jsonb))"
        ),
        {"id": step_id, "run": run_id, "out": json.dumps({"text": PRIVATE_MARKER})},
    )
    await session.execute(
        text(
            "INSERT INTO llm_calls (id, run_id, step_id, model, prompt_redacted, "
            "response_redacted) VALUES (:id, :run, :step, 'replay', :p, :p)"
        ),
        {"id": uuid7(), "run": run_id, "step": step_id, "p": PRIVATE_MARKER},
    )
    await session.commit()
    try:
        them = await stack(reviewer.email)
        for path in (
            "/api/v1/jobs/counts",
            "/api/v1/applications",
            "/api/v1/approvals",
            "/api/v1/profile/facts/summary",
            "/api/v1/agent-runs/summary",
        ):
            res = await them.get(path)
            assert res.status_code == 200, (path, res.text)

        runs = await them.get(
            "/api/v1/agent-runs", params={"application_id": str(demo_application["id"])}
        )
        detail = await them.get(f"/api/v1/agent-runs/{run_id}")
        for res in (runs, detail):
            assert res.status_code == 200, res.text
            assert PRIVATE_MARKER not in res.text and "Traceback" not in res.text
        assert [row["error_type"] for row in runs.json()] == ["ModelOutputError"]
        assert detail.json()["steps"] == [
            {
                k: detail.json()["steps"][0][k]
                for k in (
                    "step_index",
                    "step_name",
                    "status",
                    "duration_ms",
                    "started_at",
                    "finished_at",
                )
            }
        ]
        await them.close()
    finally:
        await session.execute(text("DELETE FROM llm_calls WHERE run_id = :id"), {"id": run_id})
        await session.execute(
            text("DELETE FROM agent_run_steps WHERE run_id = :id"), {"id": run_id}
        )
        await session.execute(text("DELETE FROM agent_runs WHERE id = :id"), {"id": run_id})
        await session.commit()
