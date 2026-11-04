"""M13 Phase 1: approvals inbox, submission readiness and redacted history.

Readiness reuses the Submit action's own precondition routine, so these tests
also check parity: whatever readiness reports, Submit agrees.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text, update

from packages.core.events.outbox import emit
from packages.core.models import Application, ApplicationStatus, Job, Submission, User
from packages.core.services.settings import set_submission_dispatch_enabled
from packages.shared.clock import utc_now
from packages.shared.config import get_settings
from packages.shared.ids import uuid7
from tests.integration import test_submission_api as submission_api
from tests.integration import test_submission_readiness as sr_tests

DOMAIN = submission_api.DOMAIN
client_factory = submission_api.client_factory
inputs = submission_api.inputs
store = submission_api.store
_clean_all = submission_api._clean_all

pytestmark = [
    pytest.mark.integration,
    pytest.mark.safety,
    pytest.mark.usefixtures("audit_boundary_engine"),
]

RECIPIENT = f"hiring@{DOMAIN}"


async def _reviewer(session) -> User:
    reviewer = User(id=uuid7(), email=f"reviewer-{uuid7().hex}@example.invalid", role="reviewer")
    session.add(reviewer)
    await session.commit()
    return reviewer


async def _ready(session, *, demo_profile, demo_job, demo_application, inputs, tmp_path, store):
    await session.execute(update(Job).where(Job.id == demo_job).values(url=f"mailto:{RECIPIENT}"))
    _base, _evaluation, facts = inputs
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
    await sr_tests._trigger_readiness(session, demo_application["id"], store=store)
    await session.commit()
    return demo_application["id"]


async def _enable_live(session, demo_user, monkeypatch) -> None:
    monkeypatch.setenv("SUBMISSION_MODE", "enabled")
    get_settings.cache_clear()
    owner = await session.get(User, demo_user)
    await set_submission_dispatch_enabled(session, True, actor=owner)
    await session.commit()


def _checks(body) -> dict[str, bool]:
    return {check["key"]: check["passed"] for check in body["checks"]}


# ------------------------------------------------------------- readiness
async def test_readiness_reports_every_unmet_condition(
    client_factory, session, demo_application, demo_user
) -> None:
    await session.commit()
    client = await client_factory(demo_user)
    async with client:
        res = await client.get(
            f"/api/v1/applications/{demo_application['id']}/submission-readiness"
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ready"] is False and body["can_submit"] is False
    checks = _checks(body)
    # Full evaluation, not first-failure: every condition is listed.
    assert list(checks) == [
        "application_status",
        "submission_enabled",
        "review_complete",
        "policy_allows_email",
        "policy_review_current",
        "attempt_recorded",
        "attempt_not_executed",
        "destination_authoritative",
    ]
    assert checks["application_status"] is False
    assert checks["submission_enabled"] is False
    assert body["gate"] == {"submission_mode": "dry_run", "persisted_enabled": False, "live": False}


async def test_dry_run_blocks_a_fully_ready_application(
    client_factory,
    session,
    demo_profile,
    demo_job,
    demo_application,
    demo_user,
    inputs,
    tmp_path,
    store,
) -> None:
    app_id = await _ready(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        inputs=inputs,
        tmp_path=tmp_path,
        store=store,
    )
    client = await client_factory(demo_user)
    async with client:
        res = await client.get(f"/api/v1/applications/{app_id}/submission-readiness")
        body = res.json()
        checks = _checks(body)
        assert checks.pop("submission_enabled") is False
        assert all(checks.values()), body
        assert body["ready"] is False and body["can_submit"] is False
        assert body["gate"]["submission_mode"] == "dry_run" and body["gate"]["live"] is False
        assert body["destination_domain"] == DOMAIN and body["attempt"] == 1
        assert "@" not in res.text and RECIPIENT not in res.text
        # Parity: Submit refuses for the same reason.
        submit = await client.post(f"/api/v1/applications/{app_id}/submit", json={})
        assert submit.status_code == 409
        assert "disabled" in submit.text


async def test_live_ready_application_parity_with_submit(
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
    await _enable_live(session, demo_user, monkeypatch)
    app_id = await _ready(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        inputs=inputs,
        tmp_path=tmp_path,
        store=store,
    )
    reviewer = await _reviewer(session)
    reviewer_client = await client_factory(reviewer.id)
    async with reviewer_client:
        res = await reviewer_client.get(f"/api/v1/applications/{app_id}/submission-readiness")
        assert res.json()["ready"] is True
        assert res.json()["can_submit"] is False  # reviewers never submit
        assert (
            await reviewer_client.post(f"/api/v1/applications/{app_id}/submit", json={})
        ).status_code == 403

    client = await client_factory(demo_user)
    async with client:
        body = (await client.get(f"/api/v1/applications/{app_id}/submission-readiness")).json()
        assert body["ready"] is True and body["can_submit"] is True
        assert body["gate"] == {
            "submission_mode": "enabled",
            "persisted_enabled": True,
            "live": True,
        }
        submit = await client.post(f"/api/v1/applications/{app_id}/submit", json={})
        assert submit.status_code == 202, submit.text
        # Duplicate requests are refused by the server, not just the UI.
        assert (
            await client.post(f"/api/v1/applications/{app_id}/submit", json={})
        ).status_code == 409
        after = (await client.get(f"/api/v1/applications/{app_id}/submission-readiness")).json()
        assert after["ready"] is False and after["can_submit"] is False
        assert _checks(after)["application_status"] is False

        history = await client.get(f"/api/v1/applications/{app_id}/submissions")
        assert history.status_code == 200
        [attempt] = history.json()
        assert (attempt["attempt"], attempt["outcome"], attempt["result"]) == (
            1,
            "pending",
            "pending",
        )


async def test_readiness_is_strictly_read_only(
    client_factory, session, demo_application, demo_user, monkeypatch
) -> None:
    await session.commit()

    async def state():
        row = (
            await session.execute(
                text(
                    "SELECT (SELECT count(*) FROM events), (SELECT count(*) FROM submissions), "
                    "(SELECT md5(string_agg(a::text, ',' ORDER BY a.id)) FROM applications a)"
                )
            )
        ).one()
        await session.commit()
        return row

    before = await state()
    client = await client_factory(demo_user)
    async with client:
        for path in ("submission-readiness", "submissions"):
            assert (
                await client.get(f"/api/v1/applications/{demo_application['id']}/{path}")
            ).status_code == 200
        assert (await client.get("/api/v1/approvals")).status_code == 200
    assert await state() == before

    # The database itself refuses a write inside the readiness transaction.
    from apps.api.routers import review_status

    async def writes(session, application, settings):
        await session.execute(
            text("UPDATE applications SET notes = 'x' WHERE id = :id"), {"id": application.id}
        )

    monkeypatch.setattr(review_status, "evaluate_submit_preconditions", writes)
    client = await client_factory(demo_user)
    async with client:
        with pytest.raises(Exception, match="read-only transaction"):
            await client.get(f"/api/v1/applications/{demo_application['id']}/submission-readiness")


async def test_readiness_and_history_are_not_enumerable(client_factory, session, demo_user) -> None:
    reviewer = await _reviewer(session)
    for user in (demo_user, reviewer.id):
        client = await client_factory(user)
        async with client:
            for path in ("submission-readiness", "submissions"):
                res = await client.get(f"/api/v1/applications/{uuid7()}/{path}")
                assert res.status_code == 404
                assert "application not found" in res.text
    anonymous = await client_factory(None)
    async with anonymous:
        for path in ("submission-readiness", "submissions"):
            assert (
                await anonymous.get(f"/api/v1/applications/{uuid7()}/{path}")
            ).status_code == 401
        assert (await anonymous.get("/api/v1/approvals")).status_code == 401


# --------------------------------------------------------------- history
async def test_history_outcomes_come_from_structured_fields_only(
    client_factory, session, demo_application, demo_user
) -> None:
    app_id = demo_application["id"]
    now = utc_now()
    rows = {
        1: {"result": "failed", "started_at": None, "finished_at": now},  # preflight
        2: {
            "result": "failed",
            "started_at": now,
            "finished_at": now,
            "next_attempt_after": now + timedelta(minutes=1),
            "http_status": 421,
            "failure_reason": "smtp.private.example said 421 to hiring@example.invalid",
        },
        3: {
            "result": "failed",
            "started_at": now,
            "finished_at": now,
            "failure_reason": "ambiguous: connection to mail.private.example dropped",
        },
        4: {
            "result": "failed",
            "started_at": now,
            "finished_at": now,
            "http_status": 550,
            "failure_reason": "rejected",
        },
        5: {"result": "succeeded", "started_at": now, "finished_at": now, "http_status": 250},
    }
    ids: dict[int, UUID] = {}
    for attempt, fields in rows.items():
        submission = Submission(
            application_id=app_id, attempt=attempt, method="email", target_domain=DOMAIN, **fields
        )
        session.add(submission)
        await session.flush()
        ids[attempt] = submission.id
    await emit(
        session,
        type="submission.failed",
        aggregate_type="submission",
        aggregate_id=ids[3],
        correlation_id=app_id,
        actor_type="system",
        payload={"submission_id": str(ids[3]), "ambiguous": True, "will_retry": False},
    )
    await session.commit()

    for user in (demo_user, (await _reviewer(session)).id):
        client = await client_factory(user)
        async with client:
            res = await client.get(f"/api/v1/applications/{app_id}/submissions")
        assert res.status_code == 200, res.text
        outcomes = {row["attempt"]: row["outcome"] for row in res.json()}
        assert outcomes == {
            1: "preflight_blocked",
            2: "retry_scheduled",
            3: "ambiguous",
            4: "failed",
            5: "sent",
        }
        assert "failure_reason" not in res.text
        assert "private.example" not in res.text and "@" not in res.text
        assert {row["smtp_status"] for row in res.json()} == {None, 421, 550, 250}


# ----------------------------------------------------------------- inbox
async def test_inbox_lists_pending_approvals_without_content(
    client_factory, session, pending_approval, demo_application, demo_user
) -> None:
    await session.execute(
        text("UPDATE approvals SET subject_snapshot = CAST(:snap AS jsonb) WHERE id = :id"),
        {"id": pending_approval, "snap": '{"private_note": "PRIVATE"}'},
    )
    await session.commit()
    reviewer = await _reviewer(session)
    for user in (demo_user, reviewer.id):
        client = await client_factory(user)
        async with client:
            res = await client.get(
                "/api/v1/approvals", params={"application_id": str(demo_application["id"])}
            )
            assert res.status_code == 200, res.text
            mine = [row for row in res.json() if row["approval_id"] == str(pending_approval)]
            assert len(mine) == 1
            item = mine[0]
            assert (item["kind"], item["status"], item["application_id"]) == (
                "application",
                "pending",
                str(demo_application["id"]),
            )
            assert "PRIVATE" not in res.text and "subject_snapshot" not in res.text
            scope = {"application_id": str(demo_application["id"])}
            filtered = await client.get("/api/v1/approvals", params={**scope, "kind": "resume"})
            assert str(pending_approval) not in filtered.text
            decided = await client.get("/api/v1/approvals", params={**scope, "status": "approved"})
            assert str(pending_approval) not in decided.text


@pytest.mark.parametrize(
    "params",
    [
        {"status": "maybe"},
        {"kind": "everything"},
        {"application_id": "not-a-uuid"},
        {"limit": 0},
        {"limit": 201},
        {"offset": -1},
    ],
)
async def test_inbox_rejects_invalid_filters(client_factory, session, demo_user, params) -> None:
    await session.commit()
    client = await client_factory(demo_user)
    async with client:
        assert (await client.get("/api/v1/approvals", params=params)).status_code == 422


async def test_inbox_pending_is_oldest_first(
    client_factory, session, demo_application, demo_user
) -> None:
    app_id = demo_application["id"]
    older, newer = uuid7(), uuid7()
    base = utc_now() - timedelta(days=400)
    for approval_id, kind, offset in ((newer, "resume", 2), (older, "cover_letter", 1)):
        await session.execute(
            text(
                "INSERT INTO approvals (id, application_id, kind, status, subject_snapshot, "
                "requested_at, created_at, updated_at) VALUES (:id, :app, :kind, 'pending', "
                "'{}'::jsonb, :at, now(), now())"
            ),
            {
                "id": approval_id,
                "app": app_id,
                "kind": kind,
                "at": base + timedelta(minutes=offset),
            },
        )
    await session.commit()
    client = await client_factory(demo_user)
    async with client:
        res = await client.get("/api/v1/approvals", params={"application_id": str(app_id)})
        ids = [row["approval_id"] for row in res.json()]
    assert ids == [str(older), str(newer)]
    application = await session.get(Application, app_id)
    assert application is not None and application.status is not ApplicationStatus.SUBMITTING
    assert (
        await session.scalar(select(Submission.id).where(Submission.application_id == app_id))
        is None
    )


# ---------------------------------------------------------- cover letter
async def test_cover_letter_content_for_review(
    client_factory,
    session,
    demo_profile,
    demo_job,
    demo_application,
    demo_user,
    inputs,
    tmp_path,
    store,
) -> None:
    await session.commit()
    client = await client_factory(demo_user)
    async with client:
        missing = await client.get(f"/api/v1/applications/{demo_application['id']}/cover-letter")
        assert missing.status_code == 404
    app_id = await _ready(
        session,
        demo_profile=demo_profile,
        demo_job=demo_job,
        demo_application=demo_application,
        inputs=inputs,
        tmp_path=tmp_path,
        store=store,
    )
    reviewer = await _reviewer(session)
    for user in (demo_user, reviewer.id):
        client = await client_factory(user)
        async with client:
            res = await client.get(f"/api/v1/applications/{app_id}/cover-letter")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["application_id"] == str(app_id) and body["version"] >= 1
        assert body["claims"], body
        paths = [claim["path"] for claim in body["claims"]]
        assert paths == sorted(paths)
        assert {"claim_text", "fact_ids", "verdict"} <= set(body["claims"][0])
