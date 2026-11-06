"""Real-browser E2E against the isolated demo stack (opt-in: `-m e2e`).

Prerequisites (README, "End-to-end tests"):
  make demo-up && make demo-seed      # demo web on 127.0.0.1:3100
  make demo-web-host                  # or the web container, if it is healthy
  .venv/bin/playwright install chromium

Safety, enforced before any test runs:
  * `.env.demo` must mark the demo environment and dry-run submission;
  * the running API must report `submission_mode == "dry_run"` and
    `live == false`, or the whole session stops;
  * no test confirms a Submit or turns the submission switch on.
The demo itself uses replayed generation and a fake email transport, so
nothing here can reach a provider, an inbox or an employer.

Determinism: records a test changes (approving, answering, verifying,
retiring) come from `scripts.demo_env e2e-fixture`, which creates fresh,
uniquely named synthetic rows for each run. Read-only checks use the seeded
demo rows, located by their fixed synthetic titles, never by row order.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = ROOT / ".env.demo"
DESKTOP = {"width": 1280, "height": 800}
MOBILE = {"width": 375, "height": 812}
TIMEOUT_MS = 30_000  # the demo shares a small Docker VM; be patient, not flaky
expect.set_options(timeout=TIMEOUT_MS)

pytestmark = pytest.mark.e2e


def _demo_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        pytest.skip(".env.demo not found - run `make demo-env` and start the demo stack")
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if values.get("JOBFORGE_DEMO_ENVIRONMENT") != "1":
        pytest.exit("refusing: .env.demo is not a demo environment", returncode=2)
    if values.get("SUBMISSION_MODE", "dry_run") != "dry_run":
        pytest.exit("refusing: the demo must run with SUBMISSION_MODE=dry_run", returncode=2)
    return values


@dataclass(frozen=True)
class Demo:
    base_url: str
    owner_email: str
    owner_password: str = field(repr=False)
    reviewer_email: str
    reviewer_password: str = field(repr=False)
    env: dict[str, str] = field(repr=False)


@pytest.fixture(scope="session")
def demo() -> Demo:
    env = _demo_env()
    base = os.getenv(
        "JOBFORGE_E2E_BASE_URL", f"http://127.0.0.1:{env.get('DEMO_WEB_HOST_PORT', '3100')}"
    )
    return Demo(
        base_url=base,
        owner_email="demo.candidate@example.com",
        owner_password=env["DEMO_OWNER_PASSWORD"],
        reviewer_email="demo.reviewer@example.com",
        reviewer_password=env["DEMO_REVIEWER_PASSWORD"],
        env=env,
    )


@pytest.fixture(scope="session")
def base_url(demo: Demo) -> str:
    """pytest-playwright resolves relative `page.goto` paths against this."""
    return demo.base_url


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any], base_url: str) -> dict[str, Any]:
    return {**browser_context_args, "base_url": base_url, "viewport": DESKTOP}


def _sign_in(page: Page, email: str, password: str) -> None:
    page.goto("/login")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(lambda url: "/login" not in url, timeout=TIMEOUT_MS)


@pytest.fixture(scope="session")
def _storage(
    browser: Browser, demo: Demo, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Path]:
    """Sign in once per role (the login endpoint is rate limited) and verify
    the deployment is dry-run before any test touches it."""
    states: dict[str, Path] = {}
    for role, email, password in (
        ("owner", demo.owner_email, demo.owner_password),
        ("reviewer", demo.reviewer_email, demo.reviewer_password),
    ):
        # Optional existing owner session supports a locally changed password.
        # It is still authenticated and identity-checked against this demo API.
        owner_state = os.getenv("JOBFORGE_E2E_OWNER_STATE") if role == "owner" else None
        context = browser.new_context(
            base_url=demo.base_url, viewport=DESKTOP, storage_state=owner_state
        )
        context.set_default_timeout(TIMEOUT_MS)
        page = context.new_page()
        try:
            page.goto("/login", timeout=TIMEOUT_MS)
        except Exception as exc:  # any failure here means "not running"
            context.close()
            pytest.skip(f"demo web is not reachable at {demo.base_url}: {exc}")
        if owner_state:
            identity = page.request.get("/api/v1/auth/me")
            if (
                identity.status != 200
                or identity.json().get("email") != email
                or (identity.json().get("role") != role)
            ):
                context.close()
                pytest.exit(
                    "refusing: supplied owner session is expired or has the wrong identity",
                    returncode=2,
                )
        else:
            _sign_in(page, email, password)
        gate = page.request.get("/api/v1/settings/submission-gate").json()
        if gate.get("submission_mode") != "dry_run" or gate.get("live") is not False:
            context.close()
            pytest.exit(f"refusing: the demo API is not dry-run ({gate})", returncode=2)
        path = tmp_path_factory.mktemp("auth") / f"{role}.json"
        context.storage_state(path=str(path))
        states[role] = path
        context.close()
    return states


def _context(
    browser: Browser, demo: Demo, state: Path | None, viewport: dict[str, int]
) -> BrowserContext:
    context = browser.new_context(
        base_url=demo.base_url,
        viewport=viewport,
        storage_state=str(state) if state else None,
    )
    context.set_default_timeout(TIMEOUT_MS)
    return context


@pytest.fixture
def owner(browser: Browser, demo: Demo, _storage: dict[str, Path]) -> Iterator[Page]:
    context = _context(browser, demo, _storage["owner"], DESKTOP)
    yield context.new_page()
    context.close()


@pytest.fixture
def reviewer(browser: Browser, demo: Demo, _storage: dict[str, Path]) -> Iterator[Page]:
    context = _context(browser, demo, _storage["reviewer"], DESKTOP)
    yield context.new_page()
    context.close()


@pytest.fixture
def signed_in(browser: Browser, demo: Demo, _storage: dict[str, Path]):
    """A page for a role at a viewport: `signed_in("owner", MOBILE)`."""
    contexts: list[BrowserContext] = []

    def open_page(role: str, viewport: dict[str, int] = DESKTOP) -> Page:
        context = _context(browser, demo, _storage[role], viewport)
        contexts.append(context)
        return context.new_page()

    yield open_page
    for context in contexts:
        context.close()


@pytest.fixture
def anonymous(browser: Browser, demo: Demo) -> Iterator[Page]:
    context = _context(browser, demo, None, DESKTOP)
    yield context.new_page()
    context.close()


@pytest.fixture(scope="session")
def fresh(demo: Demo, _storage: dict[str, Path]) -> dict[str, str]:
    """Run-unique synthetic records: an application awaiting review with an
    unanswered required question, and a draft fact.

    Created on the host against the demo's published ports so the Docker VM
    is not asked for another Python process; the script's own guards still
    verify it is talking to the marked demo database.
    """
    env = {
        key: value for key, value in os.environ.items() if key in ("PATH", "HOME", "LANG", "TMPDIR")
    }
    env.update(demo.env)
    env.update(
        POSTGRES_HOST="127.0.0.1",
        POSTGRES_PORT=demo.env.get("DEMO_POSTGRES_HOST_PORT", "55433"),
        REDIS_HOST="127.0.0.1",
        REDIS_PORT=demo.env.get("DEMO_REDIS_HOST_PORT", "56380"),
    )
    result = subprocess.run(  # noqa: S603 - fixed argv, our own script
        [sys.executable, "-m", "scripts.demo_env", "e2e-fixture"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if result.returncode != 0 or not lines:
        pytest.fail(f"e2e-fixture failed ({result.returncode}): {result.stderr[-800:]}")
    return json.loads(lines[-1])


def csrf_headers(page: Page) -> dict[str, str]:
    """The header the web app sends with every mutation."""
    token = next((c["value"] for c in page.context.cookies() if c["name"] == "jobforge_csrf"), "")
    return {"X-CSRF-Token": token, "Content-Type": "application/json"}


def no_horizontal_overflow(page: Page) -> None:
    widths = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert widths[0] <= widths[1], f"horizontal overflow on {page.url}: {widths}"


__all__ = ["DESKTOP", "MOBILE", "csrf_headers", "expect", "no_horizontal_overflow"]
