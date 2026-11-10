"""M19 application tracking: outcome, applied date, follow-ups and notes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from packages.core.models import Application, ApplicationOutcome, ApplicationStatus, Event, User
from packages.core.services.application_tracking import (
    applied_via,
    due_follow_ups,
    update_tracking,
)
from packages.shared.clock import utc_today
from packages.shared.errors import ForbiddenError, ValidationError
from packages.shared.ids import uuid7
from tests.integration import test_submission_api as submission_api

client_factory = submission_api.client_factory


async def _user(session, role: str) -> User:
    user = User(id=uuid7(), email=f"{role}-{uuid7().hex}@example.invalid", role=role)
    session.add(user)
    await session.flush()
    return user


async def test_owner_records_applied_interview_and_follow_up(session, demo_application) -> None:
    owner = await _user(session, "owner")
    today = utc_today()
    app_id = demo_application["id"]

    with pytest.raises(ValidationError, match="record the date you applied"):
        await update_tracking(
            session, actor=owner, application_id=app_id, changes={"outcome": "interviewing"}
        )

    application = await update_tracking(
        session,
        actor=owner,
        application_id=app_id,
        changes={
            "applied_on": today - timedelta(days=3),
            "outcome": "interviewing",
            "follow_up_on": today + timedelta(days=2),
            "notes": "  Phone screen with the platform team.  ",
        },
    )
    assert application.outcome is ApplicationOutcome.INTERVIEWING
    assert application.notes == "Phone screen with the platform team."
    assert application.outcome_updated_at is not None
    # Tracking never moves the pipeline or claims a submission.
    assert application.status is ApplicationStatus.CREATED
    assert application.submitted_at is None

    events = (
        await session.scalars(
            select(Event).where(
                Event.type == "application.outcome.updated", Event.aggregate_id == app_id
            )
        )
    ).all()
    assert [e.payload for e in events] == [
        {
            "application_id": str(app_id),
            "from_outcome": "pending",
            "to_outcome": "interviewing",
            "source": "human",
        }
    ]

    # Notes-only edits emit no outcome event; clearing is explicit.
    await update_tracking(
        session, actor=owner, application_id=app_id, changes={"notes": None, "follow_up_on": None}
    )
    assert application.notes is None and application.follow_up_on is None
    assert (
        len(
            (
                await session.scalars(
                    select(Event).where(
                        Event.type == "application.outcome.updated", Event.aggregate_id == app_id
                    )
                )
            ).all()
        )
        == 1
    )

    with pytest.raises(ValidationError, match="record the date you applied"):
        await update_tracking(
            session, actor=owner, application_id=app_id, changes={"applied_on": None}
        )


async def test_tracking_validation_and_role(session, demo_application) -> None:
    owner = await _user(session, "owner")
    reviewer = await _user(session, "reviewer")
    app_id = demo_application["id"]
    today = utc_today()
    with pytest.raises(ForbiddenError):
        await update_tracking(
            session, actor=reviewer, application_id=app_id, changes={"notes": "x"}
        )
    for changes, message in (
        ({"applied_on": today + timedelta(days=5)}, "cannot be in the future"),
        ({"follow_up_on": today + timedelta(days=800)}, "within the next two years"),
        ({"notes": "x" * 4001}, "at most 4000"),
        ({"outcome": None}, "cannot be cleared"),
        ({"status": "submitted"}, "unknown tracking fields"),
    ):
        with pytest.raises(ValidationError, match=message):
            await update_tracking(session, actor=owner, application_id=app_id, changes=changes)


async def test_database_refuses_an_outcome_without_an_applied_date(
    session, demo_application
) -> None:
    with pytest.raises(IntegrityError, match="outcome_requires_applied"):
        async with session.begin_nested():
            await session.execute(
                text("UPDATE applications SET outcome = 'offer' WHERE id = :id"),
                {"id": demo_application["id"]},
            )


async def test_due_follow_ups_exclude_closed_and_future(session, demo_application) -> None:
    owner = await _user(session, "owner")
    today = utc_today()
    app_id = demo_application["id"]
    await update_tracking(
        session,
        actor=owner,
        application_id=app_id,
        changes={
            "applied_on": today - timedelta(days=10),
            "follow_up_on": today - timedelta(days=2),
        },
    )
    due = {row.application_id: row for row in await due_follow_ups(session, on=today)}
    assert due[app_id].days_overdue == 2
    assert due[app_id].job_title == "Demo Cloud Engineer"
    assert due[app_id].company_name == "Example Cloud Corp"

    # Not yet due today, but inside a 7-day look-ahead.
    await update_tracking(
        session,
        actor=owner,
        application_id=app_id,
        changes={"follow_up_on": today + timedelta(days=3)},
    )
    assert app_id not in {r.application_id for r in await due_follow_ups(session, on=today)}
    ahead = {r.application_id: r for r in await due_follow_ups(session, on=today, within_days=7)}
    assert ahead[app_id].days_overdue == -3

    # A rejection closes the application: nothing is due any more.
    await update_tracking(
        session, actor=owner, application_id=app_id, changes={"outcome": "rejected"}
    )
    assert app_id not in {
        r.application_id for r in await due_follow_ups(session, on=today, within_days=7)
    }


async def test_tracking_api_roles_csrf_and_due_list(
    client_factory, session, demo_application, demo_user
) -> None:
    reviewer = await _user(session, "reviewer")
    await session.commit()
    app_id = demo_application["id"]
    today = utc_today()
    url = f"/api/v1/applications/{app_id}/tracking"

    async with await client_factory(demo_user) as owner:
        assert (await owner.get(url)).json()["outcome"] == "pending"
        patched = await owner.patch(
            url,
            json={
                "applied_on": str(today - timedelta(days=4)),
                "outcome": "offer",
                "follow_up_on": str(today),
                "notes": "Synthetic offer note.",
            },
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["outcome"] == "offer"
        assert patched.json()["status"] == "created"
        bad = await owner.patch(url, json={"status": "submitted"})
        assert bad.status_code == 422
        missing = await owner.patch(f"/api/v1/applications/{uuid7()}/tracking", json={"notes": "x"})
        assert missing.status_code == 404
        due = (await owner.get("/api/v1/follow-ups", params={"on": str(today)})).json()
        mine = [row for row in due if row["application_id"] == str(app_id)]
        assert mine and mine[0]["days_overdue"] == 0 and mine[0]["outcome"] == "offer"

    async with await client_factory(demo_user, csrf=False) as no_csrf:
        assert (await no_csrf.patch(url, json={"notes": "x"})).status_code == 403

    async with await client_factory(reviewer.id) as reviewer_client:
        read = await reviewer_client.get(url)
        assert read.status_code == 200 and read.json()["notes"] == "Synthetic offer note."
        assert (await reviewer_client.get("/api/v1/follow-ups")).status_code == 200
        assert (await reviewer_client.patch(url, json={"notes": "x"})).status_code == 403

    stored = await session.get(Application, app_id, populate_existing=True)
    assert stored is not None and stored.notes == "Synthetic offer note."


async def test_jobforge_submission_date_fills_the_applied_date(session, demo_application) -> None:
    # Only the approved pipeline writes submitted_at; here it is set
    # directly (status stays `created`) to isolate the tracking rule.
    owner = await _user(session, "owner")
    app_id = demo_application["id"]
    await session.execute(
        text("UPDATE applications SET submitted_at = '2026-11-02 23:30:00+00' WHERE id = :id"),
        {"id": app_id},
    )
    application = await session.get(Application, app_id, populate_existing=True)
    assert application is not None and applied_via(application) == "jobforge"

    await update_tracking(
        session, actor=owner, application_id=app_id, changes={"outcome": "interviewing"}
    )
    assert str(application.applied_on) == "2026-11-02"
    assert applied_via(application) == "jobforge"


async def test_manual_application_is_recorded_as_manual(session, demo_application) -> None:
    owner = await _user(session, "owner")
    app_id = demo_application["id"]
    application = await update_tracking(
        session,
        actor=owner,
        application_id=app_id,
        changes={"applied_on": utc_today() - timedelta(days=1)},
    )
    assert applied_via(application) == "manual"
    assert application.status is ApplicationStatus.CREATED  # nothing claims a submission
    assert application.submitted_at is None


async def test_applications_list_filters_by_outcome_and_applied(
    client_factory, session, demo_application, demo_user
) -> None:
    owner = await _user(session, "owner")
    app_id = demo_application["id"]
    await update_tracking(
        session,
        actor=owner,
        application_id=app_id,
        changes={"applied_on": utc_today() - timedelta(days=6), "outcome": "offer"},
    )
    await session.commit()

    async with await client_factory(demo_user) as client:

        async def ids(**params: str) -> dict[str, dict]:
            response = await client.get("/api/v1/applications", params={"limit": 200, **params})
            assert response.status_code == 200, response.text
            return {row["id"]: row for row in response.json()}

        offers = await ids(outcome="offer")
        assert str(app_id) in offers
        assert all(row["outcome"] == "offer" for row in offers.values())
        row = offers[str(app_id)]
        assert row["applied_via"] == "manual"
        assert row["applied_on"] == str(utc_today() - timedelta(days=6))
        assert str(app_id) not in await ids(outcome="rejected")
        assert str(app_id) in await ids(applied="true")
        not_applied = await ids(applied="false")
        assert str(app_id) not in not_applied
        assert all(row["applied_via"] is None for row in not_applied.values())
        bad = await client.get("/api/v1/applications", params={"outcome": "hired"})
        assert bad.status_code == 422
