# CI and safe local verification

`ci.yml` runs on pushes and pull requests to `dev` and `main`, and manually.
It grants only repository read access; checkout credentials are not persisted.
Every job has a timeout. No workflow pushes images, sends email, submits an
application, or provisions cloud infrastructure.

## Required checks

- Backend lint/format, strict mypy, schema conformance, model shadowing and
  generated down-migration consistency.
- Full backend pytest with coverage, PostgreSQL/pgvector, Redis and a
  runner-only MinIO. Migrations run before tests. The app_agent, app_api,
  app_monitor and app_audit roles receive runner-only dummy passwords.
- Safety tests and the six API E2E journeys run in separate pytest processes.
  `REQUIRE_SECURITY_TESTS=1` makes unavailable security fixtures fail closed.
- `scripts/check_test_report.py` rejects missing, malformed, empty, failed
  or skipped pytest JUnit evidence. It gates backend, safety and both E2E suites.
  Coverage and JUnit reports are uploaded even after test failures.
- Frontend uses `npm ci`, then the full dependency audit, lint, typecheck,
  Vitest and production build under Node 22. Docker also uses `npm ci`;
  an invalid/missing lockfile is an error rather than an install fallback.
- Python audit includes development requirements (including pypdf). Secret
  scanning uses the read-only hook and the reviewed baseline on tracked files,
  then Gitleaks 8.30.1 (release binary, checksum-verified) scans every
  reachable commit with the default rules and the one documented exclusion in
  `.gitleaks.toml`. Locally: `make history-scan`.
  The CAPTCHA/stealth check scans dependency manifests and the npm lockfile,
  avoiding the old false positive on its own scanner source.
- API and web production images are built with `push: false`.
- Both workflows are checked with checksum-verified actionlint 1.7.7.
  The Ubuntu runner's ShellCheck is used by actionlint when available.

All workflow tests force `LLM_MODE=replay`, `SUBMISSION_MODE=dry_run` and
`EMAIL_TRANSPORT_MODE=fake`. CI needs no provider, SMTP or production secrets.
A freshly hosted run is still required to establish the GitHub runner result;
local checks do not substitute for a hosted Actions execution.

## Browser workflow

`playwright.yml` is a separate manual workflow to avoid coupling browser and
Docker startup time to the ordinary checks. Once this file exists on the
repository's default branch, select **Playwright E2E → Run workflow** in
GitHub Actions and choose the desired branch. There is no automatic schedule.

The runner generates a fresh `.env.demo`, builds only the isolated demo
PostgreSQL, Redis, API and web, waits for health, migrates, marks the database,
seeds fictional data, installs Chromium and runs the browser suite (the M13
journeys and the theme checks). It starts no submission/generation workers.
Fixtures refuse a demo that lacks its identity marker or reports a live
submission gate. They never sign in as the seeded owner: each run creates
`e2e-owner-<run>` and `e2e-reviewer-<run>` through the guarded
`scripts.demo_env e2e-fixture` (passwords generated in memory, passed only
through that script's environment, never uploaded) and retires both accounts
at the end, so the job needs no stored credentials or repository secrets. JUnit is
uploaded; no authentication storage state or generated `.env.demo` is uploaded.
The final cleanup removes only this ephemeral runner's demo project/volumes.
Do not run that cleanup command against a valued local demo database.

## Local equivalent

Use Python 3.12, Node 22, a virtualenv with `requirements-dev.txt`, and a
**dedicated test database** migrated to head. Configure PostgreSQL, Redis,
MinIO and the four app-role passwords for that database; never reuse production
settings. Existing services need not be stopped or restarted.

```sh
ruff check .
ruff format --check .
mypy apps packages workers
python scripts/verify_schema.py
python scripts/check_model_shadowing.py
python scripts/gen_down_migration.py --check
export ENVIRONMENT=test LLM_MODE=replay SUBMISSION_MODE=dry_run
export EMAIL_TRANSPORT_MODE=fake REQUIRE_SECURITY_TESTS=1
pytest --cov --cov-report=xml --junitxml=test-results/backend.xml
python scripts/check_test_report.py test-results/backend.xml
REQUIRE_SECURITY_TESTS=1 pytest -m safety --junitxml=test-results/safety.xml
python scripts/check_test_report.py test-results/safety.xml
pytest -m e2e tests/e2e/test_m13_api_journeys.py --junitxml=test-results/api-e2e.xml
python scripts/check_test_report.py test-results/api-e2e.xml
pip-audit -r requirements-dev.txt
make history-scan
(cd apps/web && npm ci && npm audit && npm run lint && npm run typecheck && npm test && npm run build)
actionlint
```

For browser setup use the README's E2E instructions and
[DEMO_ENVIRONMENT.md](DEMO_ENVIRONMENT.md). Run browser and API E2E in separate
processes. If port 3100 is occupied, inspect its owner first; use a separate
frontend directory/cache and `JOBFORGE_E2E_BASE_URL` for another port rather
than disrupting another process. A green result with skips is not verification.
