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
