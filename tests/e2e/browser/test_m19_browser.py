"""M19 application tracking journey against the fictional demo (opt-in: -m e2e).

Uses the fixture's run-unique `resume_ready` application, so the job title
is unique to this run. Tracking is a human record only: nothing here moves
the pipeline or sends anything.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.browser.conftest import Accounts, csrf_headers

pytestmark = pytest.mark.e2e


def test_owner_tracks_an_interview_and_follow_up_then_closes_it(
    owner: Page, reviewer: Page, fresh: Accounts
) -> None:
    path = f"/applications/{fresh.ready_application_id}"
    title = f"E2E Ready Engineer {fresh.run}"

    owner.goto(path)
    # "Today" as the browser sees it, which is what the dashboard sends.
    today = date.fromisoformat(
        owner.evaluate(
            "(() => { const d = new Date(); return [d.getFullYear(),"
            " String(d.getMonth() + 1).padStart(2, '0'),"
            " String(d.getDate()).padStart(2, '0')].join('-'); })()"
        )
    )
    form = owner.get_by_role("form", name="Application tracking")
    form.get_by_label("Where it stands").select_option("interviewing")
    form.get_by_role("button", name="Save tracking").click()
    expect(form.get_by_role("alert")).to_contain_text("record the date you applied")

    form.get_by_label("Date applied").fill(str(today - timedelta(days=2)))
    form.get_by_label("Follow up on").fill(str(today))
    form.get_by_label("Notes").fill("Synthetic E2E phone screen.")
    form.get_by_role("button", name="Save tracking").click()
    expect(form.get_by_role("status")).to_contain_text("nothing was sent")

    owner.goto("/")
    card = owner.get_by_test_id("follow-ups")
    expect(card.get_by_role("link", name=title)).to_contain_text("due today")
    expect(card.get_by_role("link", name=title)).to_contain_text("Interview")

    reviewer.goto(path)
    expect(reviewer.get_by_text("Synthetic E2E phone screen.")).to_be_visible()
    expect(reviewer.get_by_role("button", name="Save tracking")).to_have_count(0)
    denied = reviewer.request.patch(
        f"/api/v1{path}/tracking",
        data=json.dumps({"notes": "reviewer edit"}),
        headers=csrf_headers(reviewer),
    )
    assert denied.status == 403

    owner.goto(path)
    form = owner.get_by_role("form", name="Application tracking")
    form.get_by_label("Where it stands").select_option("rejected")
    form.get_by_role("button", name="Save tracking").click()
    expect(form.get_by_role("status")).to_contain_text("Tracking saved")
    owner.goto("/")
    expect(owner.get_by_test_id("follow-ups")).not_to_contain_text(title)
