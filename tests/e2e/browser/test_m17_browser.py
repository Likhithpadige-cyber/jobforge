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

from tests.e2e.browser.conftest import csrf_headers

pytestmark = pytest.mark.e2e

DESCRIPTION = (
    "Synthetic E2E posting. We need an engineer to run Linux hosts, automate with Ansible, "
    "write Bash scripts and join an on-call rotation for production services."
)


def test_owner_import_is_wired_and_surfaces_the_site_policy_refusal(owner: Page) -> None:
    # The fictional demo's only site policy (demo-cloud.example) blocks
    # ingestion, and unknown domains are blocked too. The panel must send the
    # posting to the server and show the server's refusal - not a fake success.
    run = uuid4().hex[:8]
    owner.goto("/job-feed")
    owner.get_by_role("button", name="Add a job").click()
    form = owner.locator("#job-import-form")
    form.get_by_label("Job title").fill(f"E2E Imported Role {run}")
    form.get_by_label("Company").fill("E2E Synthetic Company")
    form.get_by_label("Posting link").fill(f"https://demo-cloud.example/jobs/e2e-{run}")
    form.get_by_label("Job description").fill(DESCRIPTION)
    form.get_by_role("button", name="Import").click()
    alert = form.get_by_role("alert")
    expect(alert).to_contain_text(re.compile(r"site polic|blocked", re.I))
    expect(owner.get_by_role("status")).to_have_count(0)


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
