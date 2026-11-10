"""M17 core-workflow journeys against the fictional demo (opt-in: -m e2e).

Rows created here are synthetic and named `E2E ...`, like the M13 journeys.
The posting link uses the reserved `.invalid` domain and is never fetched.
"""

from __future__ import annotations

import json
import re
from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.browser.conftest import Accounts, csrf_headers

pytestmark = pytest.mark.e2e

DESCRIPTION = (
    "Synthetic E2E posting. We need an engineer to run Linux hosts, automate with Ansible, "
    "write Bash scripts and join an on-call rotation for production services."
)


def test_owner_pastes_a_job_from_an_unreviewed_domain_and_opens_it(owner: Page) -> None:
    # Owner-pasted postings are exempt from employer-domain site policies
    # because nothing is fetched; the link's domain has no policy at all.
    run = uuid4().hex[:8]
    title = f"E2E Pasted Role {run}"
    owner.goto("/job-feed")
    owner.get_by_role("button", name="Add a job").click()
    form = owner.locator("#job-import-form")
    form.get_by_label("Job title").fill(title)
    form.get_by_label("Company").fill("E2E Synthetic Company")
    form.get_by_label("Posting link").fill(f"https://careers.unreviewed.invalid/jobs/{run}")
    form.get_by_label("Job description").fill(DESCRIPTION)
    form.get_by_role("button", name="Import").click()
    expect(owner.get_by_role("status")).to_contain_text("1 added")
    owner.get_by_role("link", name="Open job").click()
    expect(owner).to_have_url(re.compile(r"/job-feed/[0-9a-f-]{36}$"))
    expect(owner.get_by_text(title).first).to_be_visible()
    # The draft panel states the deployment's mode before anything is
    # requested: the demo replays recorded responses and live AI is off.
    expect(owner.get_by_test_id("draft-mode")).to_contain_text("Replay demonstration only")
    expect(owner.get_by_test_id("draft-mode")).to_contain_text("Live AI is disabled")
    expect(owner.get_by_role("button", name="Request AI draft")).to_have_count(0)
    # The fit card replaces the old "not shown here yet" placeholder and
    # never shows a number for an unevaluated job.
    fit = owner.get_by_role("region", name="Fit with your profile")
    expect(fit).to_be_visible()
    expect(owner.get_by_text("Fit score not shown here yet")).to_have_count(0)


def test_import_validation_and_reviewer_denial(owner: Page, reviewer: Page) -> None:
    owner.goto("/job-feed")
    owner.get_by_role("button", name="Add a job").click()
    form = owner.locator("#job-import-form")
    form.get_by_role("button", name="Import").click()
    expect(form.get_by_role("alert")).to_contain_text("Enter the job title")

    reviewer.goto("/job-feed")
    expect(reviewer.get_by_role("heading", name="Job Feed")).to_be_visible()
    expect(reviewer.get_by_role("button", name="Add a job")).to_have_count(0)
    body = json.dumps(
        {
            "source_slug": "demo-local",
            "format": "json",
            "content": json.dumps(
                [{"title": "E2E denied", "url": "https://jobs.example.invalid/denied"}]
            ),
        }
    )
    response = reviewer.request.post(
        "/api/v1/jobs/import",
        data=body,
        headers={"Content-Type": "application/json", **csrf_headers(reviewer)},
    )
    assert response.status == 403


def _seeded_submission_application(page: Page) -> str:
    rows = page.request.get("/api/v1/applications?limit=200&offset=0").json()
    matches = [
        row["id"]
        for row in rows
        if row["job_title"] == "Platform Engineer" and row["status"] == "submission_ready"
    ]
    if not matches:
        pytest.skip("the seeded submission demo is missing - run `make demo-seed`")
    return sorted(matches)[0]


def test_export_panel_lists_files_and_relays_the_ownership_refusal(owner: Page) -> None:
    # The run's E2E owner does not own the candidate profile, so the
    # contact-bearing PDF must be refused by the server and the refusal shown.
    # (The profile owner's successful download is covered by API journeys
    # and integration tests; this suite never signs in as the seeded owner.)
    app_id = _seeded_submission_application(owner)
    owner.goto(f"/resume-studio/{app_id}")
    export = owner.locator("section", has=owner.get_by_role("heading", name="Export"))
    download = export.get_by_role("button", name="Download PDF")
    expect(download).to_be_visible()
    download.click()
    expect(export.get_by_role("alert")).to_contain_text("artifact not found")
    expect(export.get_by_role("button", name="Download DOCX")).to_be_visible()


def test_owner_hands_application_questions_to_the_deterministic_worker(
    owner: Page, reviewer: Page, fresh: Accounts
) -> None:
    # The fixture's run-unique application sits at resume_ready with no
    # questions. The demo runs no application worker, so this journey covers
    # the UI and API trigger; the worker's deterministic preparation of the
    # queued payload is covered by tests/integration/test_applications.py.
    path = f"/applications/{fresh.ready_application_id}"

    reviewer.goto(path)
    expect(reviewer.get_by_text("Questions have not been prepared yet.")).to_be_visible()
    expect(reviewer.get_by_role("button", name="Prepare answers")).to_have_count(0)
    denied = reviewer.request.post(
        f"/api/v1{path}/prepare",
        data=json.dumps({"questions": []}),
        headers=csrf_headers(reviewer),
    )
    assert denied.status == 403

    owner.goto(path)
    owner.get_by_role("button", name="Prepare answers").click()
    refusal = owner.get_by_role("alert").filter(has_text="Enter at least one question")
    expect(refusal).to_be_visible()
    owner.get_by_label("The employer's application questions, one per line").fill(
        "Why are you interested in this role?\nAre you authorised to work in the UK?"
    )
    owner.get_by_role("button", name="Prepare answers").click()
    expect(owner.get_by_role("status").filter(has_text="Queued")).to_contain_text(
        "without any AI call"
    )
