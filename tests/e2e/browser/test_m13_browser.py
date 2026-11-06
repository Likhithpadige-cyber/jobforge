"""M13 in a real browser, against the isolated demo stack (opt-in: `-m e2e`).

Covers sign-in and roles, the dashboard, agent activity, the review
workflow, submission readiness, profile, facts and settings, at desktop and
at 375 px. See conftest.py for the safety guards and fixture strategy.
"""

from __future__ import annotations

import json
import re
from uuid import uuid4

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.browser.conftest import DESKTOP, MOBILE, csrf_headers, no_horizontal_overflow

pytestmark = pytest.mark.e2e

# Status and alert regions are matched by their text: the Next.js dev overlay
# adds its own `status` and route-announcer `alert` elements to every page.

PROFILE_FIELDS = (
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
SUBMISSION_DEMO_TITLE = "Platform Engineer"  # seeded by `make demo-seed`
LEAK_MARKERS = (
    "Traceback",
    "error_message",
    "prompt_redacted",
    "response_redacted",
    "guardrail_results",
)


def _submission_ready_application(page: Page) -> str:
    rows = page.request.get("/api/v1/applications?limit=200&offset=0").json()
    matches = [
        row["id"]
        for row in rows
        if row["job_title"] == SUBMISSION_DEMO_TITLE and row["status"] == "submission_ready"
    ]
    if not matches:
        pytest.skip("the seeded submission demo is missing - run `make demo-seed`")
    return sorted(matches)[0]


# ------------------------------------------------------- sign-in and roles
def test_signed_out_visitors_are_sent_to_sign_in(anonymous: Page) -> None:
    anonymous.goto("/approvals")
    anonymous.wait_for_url(re.compile(r"/login\?next=%2Fapprovals"))
    status = anonymous.request.get("/api/v1/approvals").status
    assert status == 401


def test_wrong_password_is_refused_without_detail(anonymous: Page, fresh: dict[str, str]) -> None:
    anonymous.goto("/login")
    anonymous.get_by_label("Email").fill(fresh["reviewer_email"])
    anonymous.get_by_label("Password", exact=True).fill(f"wrong-{uuid4().hex}")
    anonymous.get_by_role("button", name="Sign in").click()
    # Next.js renders its own (empty) route-announcer alert; match ours by text.
    expect(
        anonymous.get_by_role("alert").filter(has_text="Invalid email, password or code.")
    ).to_be_visible()
    assert "/login" in anonymous.url


def test_sign_in_and_sign_out(anonymous: Page, fresh: dict[str, str]) -> None:
    anonymous.goto("/login")
    anonymous.get_by_label("Email").fill(fresh["reviewer_email"])
    anonymous.get_by_label("Password", exact=True).fill(fresh["reviewer_password"])
    anonymous.get_by_role("button", name="Sign in").click()
    # "e2e-reviewer-<run>" -> first and last word: E and the run's first letter.
    expect(anonymous.get_by_test_id("account-avatar")).to_be_visible()
    expect(anonymous.get_by_test_id("account-avatar")).to_have_accessible_name(
        f"Signed in as {fresh['reviewer_email']} (reviewer)"
    )
    anonymous.get_by_role("button", name="Sign out").first.click()
    anonymous.wait_for_url(re.compile(r"/login"))
    assert anonymous.request.get("/api/v1/auth/me").status == 401


def test_header_shows_each_users_own_initials(owner: Page, reviewer: Page, fresh) -> None:
    owner.goto("/")
    avatar = owner.get_by_test_id("account-avatar")
    expect(avatar).to_have_text(re.compile(r"^[A-Z]{1,2}$"))
    expect(avatar).to_have_attribute("href", "/profile")
    expect(avatar).to_have_accessible_name(
        re.compile(rf"Your profile\. Signed in as {re.escape(fresh.owner_email)}$")
    )

    reviewer.goto("/")
    avatar = reviewer.get_by_test_id("account-avatar")
    expect(avatar).to_have_text(re.compile(r"^[A-Z]{1,2}$"))
    expect(avatar).to_have_accessible_name(
        re.compile(rf"Signed in as {re.escape(fresh.reviewer_email)} \(reviewer\)$")
    )
    expect(reviewer.get_by_role("link", name="Monitoring")).to_have_count(0)


# --------------------------------------------------------------- dashboard
def test_dashboard_shows_real_counts(owner: Page) -> None:
    jobs = owner.request.get("/api/v1/jobs/counts").json()["total"]
    owner.goto("/")
    metrics = owner.get_by_test_id("metrics")
    expect(metrics).to_contain_text(f"Jobs found{jobs}")
    expect(metrics).not_to_contain_text("—")
    expect(owner.get_by_test_id("priority")).to_be_visible()
    expect(owner.get_by_role("heading", name="Recent agent activity", exact=True)).to_be_visible()


def test_dashboard_unknown_values_show_a_dash_and_retry(owner: Page) -> None:
    failing = {"on": True}

    def approvals(route):
        if failing["on"]:
            route.fulfill(status=500, body="{}", content_type="application/json")
        else:
            route.continue_()

    owner.route(re.compile(r"/api/v1/approvals(\?|$)"), approvals)
    owner.goto("/")
    expect(owner.get_by_label("Waiting on you: unknown")).to_have_text("—")
    expect(owner.get_by_text("Approvals are unavailable right now.")).to_be_visible()
    failing["on"] = False
    owner.get_by_role("button", name="Retry").click()
    expect(owner.get_by_label("Waiting on you: unknown")).to_have_count(0)
    expect(owner.get_by_test_id("metrics")).not_to_contain_text("—")


# ---------------------------------------------------------- agent activity
def test_agent_activity_never_shows_sensitive_output(reviewer: Page) -> None:
    bodies: list[str] = []
    reviewer.on(
        "response",
        lambda response: bodies.append(response.text())
        if "/api/v1/agent-runs" in response.url
        else None,
    )
    reviewer.goto("/agent-activity")
    expect(reviewer.get_by_test_id("activity-summary")).to_be_visible()
    first = reviewer.get_by_role("button", name="Show steps").first
    first.click()
    expect(reviewer.get_by_role("list", name="Steps").first).to_be_visible()
    for text in [reviewer.content(), *bodies]:
        for marker in LEAK_MARKERS:
            assert marker not in text, marker


# --------------------------------------------------------- review workflow
def test_review_workflow_on_fresh_synthetic_application(
    reviewer: Page, owner: Page, fresh: dict[str, str]
) -> None:
    app_id, run = fresh["application_id"], fresh["run"]
    title = f"E2E Cloud Engineer {run}"

    reviewer.goto("/approvals")
    expect(reviewer.get_by_text(title).first).to_be_visible()

    reviewer.goto(f"/applications/{app_id}")
    expect(reviewer.get_by_test_id("next-step")).to_contain_text("waiting for your review")

    # Cover letter: read with its claims, then approve through the dialog.
    reviewer.goto(f"/applications/{app_id}/cover-letter")
    expect(reviewer.get_by_text("Supported").first).to_be_visible()
    reviewer.get_by_role("button", name="Approve cover letter…").click()
    dialog = reviewer.get_by_role("dialog", name="Approve this cover letter?")
    expect(dialog.get_by_role("button", name="Approve")).to_be_disabled()
    dialog.get_by_role("checkbox").check()
    dialog.get_by_role("button", name="Approve").click()
    expect(reviewer.get_by_role("status").filter(has_text="Cover letter approved")).to_be_visible()

    # Answers: approval is refused while a required answer is missing.
    reviewer.goto(f"/applications/{app_id}/answers")
    expect(reviewer.get_by_text("1 need your answer")).to_be_visible()
    reviewer.get_by_role("button", name="Approve answers package…").click()
    dialog = reviewer.get_by_role("dialog", name="Approve this answers package?")
    dialog.get_by_role("checkbox").check()
    dialog.get_by_role("button", name="Approve").click()
    expect(dialog.get_by_role("alert")).to_contain_text("require a human answer")
    dialog.get_by_role("button", name="Cancel").click()

    # Answer it, save, then the owner approves the complete package.
    reviewer.get_by_label("Why are you interested in this role?").fill("Synthetic E2E answer.")
    reviewer.get_by_role("button", name="Save answer").click()
    expect(reviewer.get_by_role("status").filter(has_text="Answer saved.")).to_be_visible()

    owner.goto(f"/applications/{app_id}/answers")
    owner.get_by_role("button", name="Approve answers package…").click()
    dialog = owner.get_by_role("dialog", name="Approve this answers package?")
    dialog.get_by_role("checkbox").check()
    dialog.get_by_role("button", name="Approve").click()
    expect(owner.get_by_role("status").filter(has_text="Answers approved")).to_be_visible()
    expect(owner.get_by_text("These answers are approved.")).to_be_visible()

    # The resume is still pending, so readiness is not complete.
    owner.goto(f"/applications/{app_id}/submission")
    expect(
        owner.get_by_text(re.compile(r"Not met:\s*Not all required materials are approved"))
    ).to_be_visible()


# -------------------------------------------------------- submission safety
def test_submission_is_blocked_in_dry_run_for_everyone(owner: Page, reviewer: Page) -> None:
    app_id = _submission_ready_application(owner)
    owner.goto(f"/applications/{app_id}/submission")
    expect(owner.get_by_test_id("submission-gate")).to_contain_text("Dry-run mode")
    submit = owner.get_by_role("button", name="Submit application…")
    expect(submit).to_be_disabled()
    expect(owner.get_by_test_id("submit-unavailable")).to_contain_text("never sends email")

    refused = owner.request.post(
        f"/api/v1/applications/{app_id}/submit", data="{}", headers=csrf_headers(owner)
    )
    # The isolated test owner does not own the candidate's profile, so the API
    # refuses before any precondition, with the same non-enumerating 404 a
    # missing application gets. (The profile owner's dry-run 409 is covered by
    # tests/e2e/test_m13_api_journeys.py.) Either way, nothing is sent.
    assert refused.status == 404

    reviewer.goto(f"/applications/{app_id}/submission")
    expect(reviewer.get_by_test_id("reviewer-no-submit")).to_be_visible()
    expect(reviewer.get_by_role("button", name="Submit application…")).to_have_count(0)
    forbidden = reviewer.request.post(
        f"/api/v1/applications/{app_id}/submit", data="{}", headers=csrf_headers(reviewer)
    )
    assert forbidden.status == 403

    history = owner.request.get(f"/api/v1/applications/{app_id}/submissions").json()
    assert all(item["started_at"] is None for item in history), "nothing was ever sent"


# ---------------------------------------------------------- profile, facts
def test_owner_edits_profile_and_reviewer_sees_no_private_fields(
    owner: Page, reviewer: Page
) -> None:
    original = owner.request.get("/api/v1/profile").json()
    marker = f"E2E headline {uuid4().hex[:8]}"
    try:
        owner.goto("/profile")
        owner.get_by_role("button", name="Edit basics").click()
        owner.get_by_label("Headline").fill(marker)
        owner.get_by_role("button", name="Save profile").click()
        expect(owner.get_by_role("status").filter(has_text="Profile saved")).to_be_visible()
        expect(owner.get_by_text(marker)).to_be_visible()
    finally:
        # Always put the demo profile back, even if an assertion above failed.
        current = owner.request.get("/api/v1/profile").json()
        restore = {key: original[key] for key in PROFILE_FIELDS} | {"version": current["version"]}
        restored = owner.request.put(
            "/api/v1/profile", data=json.dumps(restore), headers=csrf_headers(owner)
        )
        assert restored.ok, restored.text()

    profile_body: list[str] = []
    reviewer.on(
        "response",
        lambda response: profile_body.append(response.text())
        if response.url.endswith("/api/v1/profile")
        else None,
    )
    reviewer.goto("/profile")
    expect(reviewer.get_by_role("heading", name="Facts", exact=True)).to_be_visible()
    expect(reviewer.get_by_text("Work authorization")).to_have_count(0)
    expect(reviewer.get_by_role("button", name="Edit basics")).to_have_count(0)
    redacted = reviewer.request.get("/api/v1/profile").json()
    assert all(
        redacted[k] is None
        for k in ("email", "phone", "work_authorization", "min_salary", "max_salary")
    )


def test_fact_verification_attribution_and_retirement(
    owner: Page, reviewer: Page, fresh: dict[str, str]
) -> None:
    fact_id, run = fresh["fact_id"], fresh["run"]
    text = f"Synthetic E2E skill {run}"
    me = owner.request.get("/api/v1/auth/me").json()

    spoofed = owner.request.post(
        f"/api/v1/profile/facts/{fact_id}/verify",
        data=json.dumps({"verified_by": str(uuid4())}),
        headers=csrf_headers(owner),
    )
    assert spoofed.status == 422

    reviewer.goto("/profile")
    item = reviewer.get_by_role("listitem").filter(has_text=text)
    expect(item).to_be_visible()
    expect(item.get_by_role("button", name="Verify…")).to_have_count(0)

    owner.goto("/profile")
    item = owner.get_by_role("listitem").filter(has_text=text)
    item.get_by_role("button", name="Verify…").click()
    dialog = owner.get_by_role("dialog", name="Verify this fact?")
    dialog.get_by_role("checkbox").check()
    dialog.get_by_role("button", name="Verify").click()
    expect(owner.get_by_role("status").filter(has_text="Verified.")).to_be_visible()
    fact = owner.request.get(f"/api/v1/profile/facts/{fact_id}").json()
    assert (fact["status"], fact["verified_by"]) == ("verified", me["id"])

    owner.get_by_role("button", name="Verified", exact=True).click()
    item = owner.get_by_role("listitem").filter(has_text=text)
    item.get_by_role("button", name="Retire…").click()
    dialog = owner.get_by_role("dialog", name="Retire this fact?")
    dialog.get_by_role("checkbox").check()
    dialog.get_by_role("button", name="Retire").click()
    expect(owner.get_by_role("status").filter(has_text="Retired.")).to_be_visible()
    assert owner.request.get(f"/api/v1/profile/facts/{fact_id}").json()["status"] == "retired"


# ---------------------------------------------------------------- settings
def test_settings_never_implies_dry_run_can_send(owner: Page, reviewer: Page) -> None:
    before = owner.request.get("/api/v1/settings/submission-gate").json()
    owner.goto("/settings")
    expect(owner.get_by_test_id("settings-gate")).to_contain_text("Dry-run · never sends")
    toggle = owner.get_by_role("button", name=re.compile(r"Turn submission switch (on|off)…"))
    toggle.click()
    dialog = owner.get_by_role("dialog")
    expect(dialog).to_contain_text(
        "nothing is sent" if not before["persisted_enabled"] else "refuses new Submit"
    )
    expect(dialog).not_to_contain_text("sends a real email")
    owner.keyboard.press("Escape")
    expect(owner.get_by_role("dialog")).to_have_count(0)
    expect(toggle).to_be_focused()
    assert owner.request.get("/api/v1/settings/submission-gate").json() == before

    reviewer.goto("/settings")
    expect(reviewer.get_by_test_id("reviewer-read-only")).to_be_visible()
    expect(reviewer.get_by_role("button", name=re.compile("Turn submission switch"))).to_have_count(
        0
    )


# ----------------------------------------------------------- layout, a11y
PAGES = ["/", "/agent-activity", "/approvals", "/profile", "/settings"]


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "375px"])
@pytest.mark.parametrize("role", ["owner", "reviewer"])
def test_m13_pages_fit_the_viewport(signed_in, role: str, viewport: dict[str, int]) -> None:
    page = signed_in(role, viewport)
    app_id = _submission_ready_application(page)
    for path in [*PAGES, f"/applications/{app_id}", f"/applications/{app_id}/submission"]:
        page.goto(path)
        expect(page.get_by_role("heading", level=1)).to_be_visible()
        expect(page.get_by_test_id("review-loading")).to_have_count(0)
        expect(page.get_by_test_id("account-avatar")).to_be_visible()
        no_horizontal_overflow(page)
        assert page.locator("main").count() == 1, path


def test_mobile_navigation_drawer_is_a_keyboard_dialog(signed_in) -> None:
    page = signed_in("owner", MOBILE)
    page.goto("/")
    more = page.get_by_role("button", name="More navigation")
    expect(page.get_by_role("navigation", name="Mobile")).to_be_visible()
    more.click()
    drawer = page.get_by_role("dialog", name="Navigation")
    expect(drawer).to_be_visible()
    assert page.evaluate(
        "document.querySelector('[data-testid=nav-drawer]').contains(document.activeElement)"
    )
    page.keyboard.press("Escape")
    expect(drawer).to_have_count(0)
    expect(more).to_be_focused()
