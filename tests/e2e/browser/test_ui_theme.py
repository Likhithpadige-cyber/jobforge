"""Palette, WCAG rules, controls and keyboard checks against the guarded demo.

axe-core is a frontend dev dependency; npm ci is required before this suite.
No real delivery, submission switch confirmation or external provider is used.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route

from tests.e2e.browser.conftest import DESKTOP, MOBILE, expect, no_horizontal_overflow

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[3]
AXE = ROOT / "apps/web/node_modules/axe-core/axe.min.js"

PAINT_CHECK = """() => {
  const approved = new Set(['10,15,20','15,61,62','46,111,115',
    '127,179,184','234,220,200','198,96,31']);
  const failures = [];
  for (const element of document.querySelectorAll('body *')) {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    if (!rect.width || !rect.height || style.visibility === 'hidden' || style.opacity === '0') {
      continue;
    }
    for (const property of ['color','backgroundColor','borderTopColor','outlineColor','fill']) {
      // HTML and SVG containers have a default black fill but paint no geometry.
      if (property === 'fill' && !element.matches(
        'svg path,svg polygon,svg rect,svg circle,svg ellipse,svg polyline,svg text,svg use'
      )) continue;
      const value = style[property];
      const match = value.match(/^rgba?\\(([^)]+)\\)$/);
      if (!match) continue;
      const values = match[1].split(/[, ]+/).map(Number);
      if (values.length === 4 && values[3] === 0) continue;
      if (!approved.has(values.slice(0,3).join(','))) {
      failures.push(element.tagName + ':' + property + '=' + value);
    }
    }
  }
  return [...new Set(failures)];
}"""


def check_page(page: Page, label: str, *, settled: bool = True) -> None:
    if settled:
        page.wait_for_load_state("networkidle")
    expect(page.get_by_role("heading", level=1)).to_be_visible()
    # Colours ease over 120-180 ms (globals.css); sample only settled values,
    # never a colour interpolated mid-transition.
    page.wait_for_function("() => document.getAnimations().every(a => a.playState !== 'running')")
    no_horizontal_overflow(page)
    assert page.locator("body").evaluate("e => getComputedStyle(e).backgroundColor") == (
        "rgb(234, 220, 200)"
    )
    fonts = page.evaluate("""async () => {
      await document.fonts.ready;
      const heading = document.querySelector('h1');
      const brand = document.querySelector('.brand-wordmark');
      return {body:getComputedStyle(document.body).fontFamily,
        heading: heading ? getComputedStyle(heading).fontFamily : '',
        headingWeight: heading ? getComputedStyle(heading).fontWeight : '',
        brandWeight: brand ? getComputedStyle(brand).fontWeight : null};
    }""")
    assert "inter" in fonts["body"].lower()
    assert "sora" in fonts["heading"].lower()
    assert fonts["headingWeight"] == "700"
    if fonts["brandWeight"]:
        assert fonts["brandWeight"] == "800"
    typography = page.evaluate("""() => {
      const metric = document.querySelector('.metric-value');
      const nav = [...document.querySelectorAll('nav a,nav button')]
        .filter(e => e.getBoundingClientRect().width > 0);
      return {metric: metric ? getComputedStyle(metric).fontWeight : null,
        navigation: nav.map(e => getComputedStyle(e).fontWeight)};
    }""")
    if typography["metric"]:
        assert typography["metric"] == "700"
    assert all(weight == "500" for weight in typography["navigation"])
    paint = page.evaluate(PAINT_CHECK)
    assert not paint, f"{label}: off-palette computed styles: {paint}"
    page.add_script_tag(path=str(AXE))
    result = page.evaluate("""async () => {
      const result = await axe.run(document, {runOnly: {type: 'tag', values:
        ['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa']}});
      return result.violations.map(v => ({id: v.id, impact: v.impact,
        nodes: v.nodes.map(n => ({target: n.target, summary: n.failureSummary}))}));
    }""")
    assert not result, f"{label}: {result}"


def evidence(page: Page, name: str) -> None:
    directory = os.getenv("JOBFORGE_UI_EVIDENCE_DIR")
    if directory:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target / f"{name}.png"), full_page=True)


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "375px"])
def test_login_theme_and_focus(anonymous: Page, viewport: dict[str, int]) -> None:
    anonymous.set_viewport_size(viewport)
    anonymous.goto("/login")
    check_page(anonymous, "login")
    font_requests = anonymous.evaluate("""() => performance.getEntriesByType('resource')
      .filter(e => e.name.includes('.woff2')).map(e => e.name)""")
    assert len(set(font_requests)) == 2
    assert all(url.startswith(anonymous.url.split("/login")[0]) for url in font_requests)
    # Tab to each form control and prove the visible indicator survives the reset.
    anonymous.get_by_label("Email").focus()
    anonymous.keyboard.press("Tab")
    expect(anonymous.get_by_label("Password", exact=True)).to_be_focused()
    focus = anonymous.evaluate("""() => { const s = getComputedStyle(document.activeElement);
      return {color:s.outlineColor,width:s.outlineWidth,style:s.outlineStyle}; }""")
    assert focus == {"color": "rgb(127, 179, 184)", "width": "2px", "style": "solid"}
    evidence(anonymous, f"login-{viewport['width']}")
    submit = anonymous.get_by_role("button", name="Sign in", exact=True)
    expect(submit).to_be_disabled()
    anonymous.get_by_label("Email").fill("palette-check@example.com")
    anonymous.get_by_label("Password", exact=True).fill("synthetic-ui-only")
    expect(submit).to_be_enabled()
    # Colours ease over ~120 ms (globals.css), so wait for the settled value.
    expect(submit).to_have_css("background-color", "rgb(198, 96, 31)")
    expect(submit).to_have_css("color", "rgb(10, 15, 20)")
    submit.hover()
    expect(submit).to_have_css("background-color", "rgb(10, 15, 20)")
    expect(submit).to_have_css("color", "rgb(234, 220, 200)")
    # Exercise the error presentation without making a login attempt at the API.
    anonymous.route(
        "**/api/v1/auth/login",
        lambda route: route.fulfill(
            status=401, json={"detail": "Invalid email, password or code."}
        ),
    )
    submit.click()
    expect(
        anonymous.get_by_role("alert").filter(has_text="Invalid email, password or code.")
    ).to_be_visible()
    check_page(anonymous, "login error")
    anonymous.goto("/this-page-does-not-exist")
    check_page(anonymous, "not found")


@pytest.mark.parametrize("role", ["owner", "reviewer"])
@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "375px"])
def test_all_workspace_routes(signed_in, fresh, role: str, viewport: dict[str, int]) -> None:
    page = signed_in(role, viewport)
    application = fresh["application_id"]
    page.goto("/")
    applications = page.request.get("/api/v1/applications?limit=200").json()
    details = next(item for item in applications if item["id"] == application)
    routes = [
        "/",
        "/job-feed",
        "/applications",
        "/approvals",
        "/resume-studio",
        "/agent-activity",
        "/profile",
        "/settings",
        f"/applications/{application}",
        f"/applications/{application}/cover-letter",
        f"/applications/{application}/answers",
        f"/applications/{application}/submission",
        f"/resume-studio/{application}",
        f"/job-feed/{details['job_id']}",
    ]
    if role == "owner":
        routes += ["/monitoring", "/alerts"]
        # Any alert will do for the detail page; a fresh demo's seeded pause
        # incidents are resolved by the E2E fixture, so do not require an open one.
        alerts = page.request.get("/api/v1/alerts?status=all&limit=1").json()
        assert alerts, "seeded demo alert required for detail-page verification"
        routes.append(f"/alerts/{alerts[0]['id']}")
    for route in routes:
        page.goto(route)
        check_page(page, f"{role} {viewport['width']} {route}")
        if route in ("/", "/applications", "/approvals", "/profile", "/settings"):
            evidence(page, f"{role}-{route.strip('/') or 'dashboard'}-{viewport['width']}")
        elif role == "owner":
            labels = {
                f"/applications/{application}": "workspace",
                f"/applications/{application}/cover-letter": "cover-letter",
                f"/applications/{application}/answers": "answers",
                f"/applications/{application}/submission": "submission",
                f"/resume-studio/{application}": "resume-review",
                "/job-feed": "job-feed",
                "/monitoring": "monitoring",
            }
            if route in labels:
                evidence(page, f"owner-{labels[route]}-{viewport['width']}")


def test_dialog_and_form_controls(owner: Page) -> None:
    owner.goto("/profile")
    owner.get_by_role("button", name="Edit basics", exact=True).click()
    check_page(owner, "profile edit")
    owner.get_by_role("button", name="Cancel", exact=True).click()
    owner.goto("/settings")
    before = owner.request.get("/api/v1/settings/submission-gate").json()
    toggle = owner.get_by_role("button", name=re.compile(r"Turn submission switch (on|off)…"))
    toggle.click()
    dialog = owner.get_by_role("dialog")
    expect(dialog).to_be_visible()
    check_page(owner, "submission confirmation")
    checkbox = dialog.get_by_role("checkbox")
    checkbox.check()
    # Checking acknowledgement is local only; never confirm the switch.
    assert not owner.evaluate(PAINT_CHECK)
    owner.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(toggle).to_be_focused()
    assert owner.request.get("/api/v1/settings/submission-gate").json() == before


@pytest.mark.parametrize("viewport", [DESKTOP, MOBILE], ids=["desktop", "375px"])
def test_navigation_and_feedback_states(owner: Page, viewport: dict[str, int]) -> None:
    owner.set_viewport_size(viewport)
    pending: list[Route] = []
    pattern = re.compile(r"/api/v1/jobs\?")
    # Hold job requests to observe the loading state, then release them all:
    # development Strict Mode may issue the same request twice on load.
    released = {"done": False}

    def hold(route: Route) -> None:
        if released["done"]:
            route.fulfill(status=200, json=[])
        else:
            pending.append(route)

    owner.route(pattern, hold)
    owner.goto("/job-feed")
    expect(owner.get_by_role("heading", name="Loading jobs", exact=True)).to_be_visible()
    check_page(owner, "loading jobs", settled=False)
    assert pending
    released["done"] = True
    while pending:
        pending.pop().fulfill(status=200, json=[])
    expect(owner.get_by_text("No jobs yet", exact=True)).to_be_visible()
    check_page(owner, "empty jobs")
    owner.unroute(pattern)
    owner.route(pattern, lambda route: route.fulfill(status=503, json={"detail": "Unavailable"}))
    owner.reload()
    expect(owner.get_by_role("alert").filter(has_text="Request failed: 503")).to_be_visible()
    check_page(owner, "unavailable jobs")
    if viewport["width"] == MOBILE["width"]:
        opener = owner.get_by_role("button", name="Open navigation", exact=True)
        opener.click()
        check_page(owner, "mobile navigation")
        evidence(owner, "owner-navigation-375")
        owner.keyboard.press("Escape")
        expect(opener).to_be_focused()
    else:
        search = owner.get_by_role("searchbox", name="Search pages")
        search.fill("Approvals")
        check_page(owner, "page search results")
        owner.get_by_role("option", name="Approvals").click()
        expect(owner.get_by_role("heading", name="Approvals", level=1)).to_be_visible()
