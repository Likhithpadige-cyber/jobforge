"""M13 Phase 4 end-to-end suites. Opt-in only.

Two suites live here, both marked `e2e`:

* `test_m13_api_journeys.py` - multi-step journeys through the real HTTP
  API and a real PostgreSQL (the integration database), signing in through
  `POST /auth/login`.
* `browser/` - real Chromium, via pytest-playwright, against the isolated
  demo stack (web, API, PostgreSQL and Redis).

Neither runs as part of `pytest`, `make test` or the unit, integration and
safety suites: everything under this directory is deselected unless the
run asks for it with `-m e2e` (see README, "End-to-end tests").

The integration fixtures are re-exported rather than copied, so there is one
definition of a synthetic user, profile, job and application.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration.conftest import *  # noqa: F403

E2E_DIR = Path(__file__).parent


def _requested(config: pytest.Config) -> bool:
    expression = config.getoption("markexpr") or ""
    return ("e2e" in expression and "not e2e" not in expression) or os.getenv("JOBFORGE_E2E") == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if _requested(config):
        browser = E2E_DIR / "browser"
        kinds = {
            browser in Path(item.path).parents
            for item in items
            if E2E_DIR in Path(item.path).parents
        }
        if kinds == {True, False}:
            # Playwright's sync API keeps an event loop running for the whole
            # session, which the async API journeys' fixtures cannot share.
            raise pytest.UsageError(
                "run the E2E suites in separate processes: `make test-e2e` (API journeys) "
                "and `make test-e2e-browser` (browser)"
            )
        return
    selected, deselected = [], []
    for item in items:
        (deselected if E2E_DIR in Path(item.path).parents else selected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected
