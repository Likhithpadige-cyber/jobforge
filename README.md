# Jobforge

AI Job Application Platform — processes a job queue, evaluates fit against a **verified** profile, tailors a resume per role, prepares the application packet, and tracks everything on a dashboard.

**Human-in-the-loop by default.** The system prepares; a human approves. There is no automatic-approval path and no configuration flag that creates one.

> **Status: Phase 3 — ingestion, normalization, deduplication.** Deterministic only: no LLM, no crawler, no browser automation. Prior status: Phase 2 — database layer.
>
> **Phase 2 note:** 37 tables, 17 enums, least-privilege database roles, the profile and fact system, and profile CRUD endpoints are in place on top of the Phase 1 foundation. Agents, ingestion and the dashboard land in later phases (see [docs/ARCHITECTURE.md §16](docs/ARCHITECTURE.md)).

---

## Quick start

Requires Docker Desktop and Make. Python and Node are only needed if you want to run tools outside containers.

```bash
git clone <your-repo-url> jobforge
cd jobforge

cp .env.example .env          # placeholders only; add no real secrets yet
make bootstrap                # build images, start postgres + redis, run migrations
make dev                      # start the full core stack
```

Then:

| Service | URL |
|---|---|
| API docs (Swagger) | http://localhost:8000/docs |
| Liveness | http://localhost:8000/health |
| Readiness | http://localhost:8000/api/v1/health |
| Dashboard | http://localhost:3000 |

Verify by hand:

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
curl -s http://localhost:8000/api/v1/health | python3 -m json.tool
```

A healthy readiness response looks like:

```json
{
  "status": "ok",
  "service": "jobforge",
  "version": "0.1.0",
  "environment": "local",
  "uptime_seconds": 12.4,
  "checks": {
    "postgres": { "status": "ok", "latency_ms": 1.8, "error": null },
    "redis":    { "status": "ok", "latency_ms": 0.4, "error": null }
  },
  "migration_revision": "0001_baseline"
}
```

---

## Commands

`make help` lists everything. The ones you'll use daily:

| Command | Does |
|---|---|
| `make bootstrap` | First-time setup |
| `make dev` | Start postgres, redis, api, web |
| `make dev-full` | Also start MinIO and MailHog |
| `make down` | Stop containers, keep data |
| `make clean` | Stop containers, **delete volumes** |
| `make logs SVC=api` | Tail one service |
| `make migrate` | Apply migrations |
| `make migration m="add jobs"` | Create a migration |
| `make db-roles` | Set passwords for the app_* roles |
| `make db-grants` | Show what each role can do to profile_facts |
| `make db-reset` | Drop and rebuild the schema (destroys local data) |
| `make seed` | Load fictional demo data |
| `make verify-schema` | Offline: models vs migration vs design |
| `make test-security` | Run only the security-invariant tests |
| `make test` | Backend tests in the container |
| `make test-unit` | Unit tests, no services needed |
| `make check` | Lint + types + unit tests (what CI runs) |
| `make verify` | Offline scaffold check, no services needed |
| `make web-test` | Frontend tests |
| `make shell-db` | psql into the database |

---

## Layout

```
apps/api/          FastAPI: routers, schemas, dependencies
apps/web/          Next.js dashboard (TypeScript, Tailwind)
packages/shared/   config, logging, ids, errors — no framework imports
packages/core/     db, cache, models, events, grounding, policies, state machines
packages/agents/   the three agents (Phase 4+)
packages/services/ ingestion, rendering, delivery (Phase 3+)
workers/           process entrypoints (Phase 3+)
migrations/        Alembic
tests/             unit · integration · contract · evals · e2e
infra/docker/      Dockerfiles
infra/terraform/   AWS IaC (Phase 8)
docs/              the specification — read this first
```

**Import direction** (enforced by `make verify` and by `import-linter` in CI):

```
apps → packages/agents · packages/services → packages/core → packages/shared
```

`packages/core` imports no web framework. Nothing imports `workers`.

---

## Configuration

All configuration is environment-driven via `packages/shared/config.py`. Copy `.env.example` to `.env` and edit.

Two things worth knowing:

**No secret has a usable default.** Secret-bearing fields default to an empty string, and `ENVIRONMENT=production` with any of them empty raises at startup. A misconfigured deployment fails loudly instead of running on a placeholder.

**Defaults are safe.** Out of the box: `SUBMISSION_MODE=dry_run` and `LLM_MODE=replay`. Nothing leaves your machine and no LLM call costs money until you deliberately change both. Real submission additionally requires a UI toggle — two independent switches by design.

The anti-fabrication guardrails ([docs/SECURITY.md §4](docs/SECURITY.md)) are **not configurable** and appear nowhere in `.env.example`. That is deliberate.

---

## Testing

**Backend** — pytest, with markers so you can run only what you need:

```bash
make test-unit                              # no services required
make test-integration                       # needs the stack running
pytest -m "unit and safety"                 # the safety regression subset
pytest --cov --cov-report=term-missing      # coverage
```

Integration tests that need PostgreSQL or Redis skip cleanly when those aren't reachable, so `pytest` works on a laptop with nothing running.

**End-to-end** — opt-in only. Everything under `tests/e2e/` is deselected from
`pytest`, `make test` and the unit, integration and safety suites, and runs
only when a run selects the `e2e` marker:

| Suite | What it drives | Needs |
|---|---|---|
| `tests/e2e/test_m13_api_journeys.py` | Multi-step owner and reviewer journeys through the real HTTP API: sign-in, CSRF, review, answers, dry-run submission, profile, facts, agent activity | The dev stack's PostgreSQL (same as integration tests) |
| `tests/e2e/browser/` | Real Chromium via [pytest-playwright](https://playwright.dev/python/): sign-in and roles, dashboard, agent activity, review workflow, submission readiness, profile, facts, settings, at 1280 px and 375 px | The isolated demo stack ([docs/DEMO_ENVIRONMENT.md](docs/DEMO_ENVIRONMENT.md)) |

One-time setup (installs `pytest-playwright` from `requirements-dev.txt` and
downloads Playwright's Chromium, about 100 MB, into `~/Library/Caches/ms-playwright`
on macOS or `~/.cache/ms-playwright` on Linux):

```bash
make e2e-install        # = pip install -r requirements-dev.txt && playwright install chromium
```

Start the services, then run:

```bash
make demo-up demo-seed  # isolated demo: PostgreSQL, Redis, API on :8100 (dry-run, replay LLM, fake email)
make demo-web-host      # demo web on http://127.0.0.1:3100 (separate terminal; or the web container)
make test-e2e-browser   # = .venv/bin/pytest -m e2e tests/e2e/browser
make test-e2e           # API journeys, inside the dev stack
```

Useful options: `--headed` to watch, `--slowmo 250`, `-k review` to select,
`JOBFORGE_E2E_BASE_URL=http://127.0.0.1:3100` to point at another demo web.

Safety: the browser suite refuses to run unless `.env.demo` marks a demo
environment with `SUBMISSION_MODE=dry_run` *and* the running API reports
dry-run and `live: false`. No test confirms a Submit or turns the submission
switch on; the demo uses replayed generation and a fake email transport.
Records that tests change come from `python -m scripts.demo_env e2e-fixture`,
which creates fresh, uniquely named synthetic rows for every run.

**Frontend** — Vitest with Testing Library and jsdom:

```bash
cd apps/web && npm install && npm run test
```

**Offline** — `make verify` checks file layout, Python syntax, import direction, config validity, migration chain, and several safety invariants without needing any service. It runs in under a second and is a good pre-push habit.

---

## Health endpoints

Two levels, deliberately separated:

| Endpoint | Checks | Used by |
|---|---|---|
| `GET /health` | Nothing external. Always fast | Docker/ECS container restart decisions |
| `GET /api/v1/health` | PostgreSQL + Redis + migration revision | Load balancer, dashboard |

`/healthz` and `/readyz` are aliases.

Mixing these is a classic outage amplifier: if liveness checked the database, one brief database blip would restart every container at once. Readiness returns `200` for `ok` and `degraded`, `503` only for `unavailable` — Redis being down is degraded (slower), PostgreSQL being down is unavailable (can't serve).

---

## Database

### Setup

```bash
make bootstrap          # creates .env, starts postgres + redis, runs migrations
make db-roles           # set passwords for app_api / app_agent / app_monitor / app_migrate
make seed               # fictional demo profile and facts
```

`make db-roles` is not optional if you want the security tests to mean
anything: they connect **as `app_agent`** and assert that its writes are
refused. Without the role passwords those tests skip rather than pass, and a
skipped security test proves nothing.

### Migrations

```bash
make migrate                        # upgrade to head
make migration m="add widgets"      # new revision
make downgrade                      # back one revision
make db-reset                       # downgrade base, then upgrade head
```

Three revisions: `0001_baseline` (extensions) → `0002_core_schema` (all 37
tables, 17 enums, indexes, constraints, triggers) → `0003_roles_grants`
(roles and least-privilege grants).

The DDL lives in `migrations/sql/*.sql` and is executed by thin Alembic
revisions — see [DATABASE_IMPLEMENTATION.md](docs/DATABASE_IMPLEMENTATION.md)
D-1 for why. The down migration is generated from the up migration:

```bash
python3 scripts/gen_down_migration.py           # regenerate
python3 scripts/gen_down_migration.py --check   # fail if stale (runs in CI)
```

### Tests

```bash
make test                # everything
make test-unit           # no services needed
make test-security       # the safety invariants, needs postgres + roles
make verify-schema       # offline conformance check, needs nothing
make check-shadowing     # offline SQLAlchemy name-shadowing audit
pytest -m safety         # every safety-marked test, unit and integration
```

### Architecture summary

**Grounding is the point.** Every sentence a generated resume contains must
trace to a row in `profile_facts`, and only a human can put one there. That is
enforced in three independent places:

| Layer | Mechanism |
|---|---|
| Grant | `app_agent` holds `SELECT` and nothing else on `profile_facts` |
| Constraint | `resume_claims` requires a non-empty `fact_ids` array |
| Trigger | every cited fact must exist and be **verified**, checked at write time |

**Human approval is a database rule, not a code path.** An application cannot
enter `approved`, `submitting`, `submitted` or `manual_handoff` without a row
in `approvals` decided by a real user — and `app_agent` has no write access to
`approvals` and no `UPDATE` on the approval columns of `applications`.

**Table groups** (37 total, full detail in
[DATABASE_DESIGN.md](docs/DATABASE_DESIGN.md)):

| Group | Tables |
|---|---|
| Identity & truth | `users`, `profiles`, `experiences`, `educations`, `certifications`, `skills`, `projects` |
| Grounding | `profile_facts`, `fact_evidence` |
| Job domain | `sources`, `site_policies`, `companies`, `jobs`, `job_raw_snapshots`, `job_evaluations` |
| Application domain | `applications`, `resumes`, `resume_claims`, `cover_letters`, `cover_letter_claims`, `application_questions`, `application_answers`, `approvals`, `submissions`, `application_status_history`, `artifacts` |
| Operations | `events`, `processed_events`, `agent_runs`, `agent_run_steps`, `llm_calls`, `daily_quotas`, `metrics_snapshots`, `alerts`, `notifications`, `audit_logs`, `settings` |

**Roles:** `app_api` (full CRUD, no history rewrites) · `app_agent` (broad read,
narrow write, zero fact writes) · `app_monitor` (read-only plus metrics) ·
`app_migrate` (DDL only). No grant uses `WITH GRANT OPTION`, so no role can
widen its own access.

**Append-only:** `events`, `audit_logs`, `application_status_history` and
`job_evaluations`. `DELETE` is revoked from every role and blocked by trigger.

---

## Safety

This system writes documents that go to real employers under your name. The constraints that follow from that are in [docs/SECURITY.md](docs/SECURITY.md); the short version:

- **Never invents experience, education, certifications, or skills.** Enforced by five independent layers, one of which is a database grant: agent processes connect as a role with no write access to the facts table.
- **Never fabricates application answers.** Uncertain, judgement-based, and sensitive questions are human-only, with no code path for an agent to answer them.
- **Never bypasses CAPTCHA, bot detection, authentication, or rate limits.** The capability does not exist in the codebase, and CI fails the build if a solver or stealth dependency is ever added.
- **Respects site terms.** A default-deny policy registry gates every external interaction; an unknown domain is blocked.

Never commit `.env`, real profile data, or generated resumes. `.gitignore` and the pre-commit hooks block the obvious paths, but the hooks only help if you install them:

```bash
pip install pre-commit && pre-commit install
```

---

## Documentation

Read these before changing anything structural:

| Document | Contents |
|---|---|
| [DATABASE_IMPLEMENTATION.md](docs/DATABASE_IMPLEMENTATION.md) | What Phase 2 actually built, and every deviation from the design |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, API surface, state machines, dashboard, testing, observability |
| [AGENT_DESIGN.md](docs/AGENT_DESIGN.md) | The three agents: steps, prompts, guardrails, capability matrix |
| [DATABASE_DESIGN.md](docs/DATABASE_DESIGN.md) | Entities, DDL, enums, indexes, roles and grants |
| [EVENTS.md](docs/EVENTS.md) | Event envelope, catalogue, delivery semantics, versioning |
| [SECURITY.md](docs/SECURITY.md) | Threat model, controls, compliance, incident response |

If a value appears in two documents and they disagree, the canonical document listed in ARCHITECTURE.md §15 wins and the other is a bug.

---

## Phase status

| Phase | Deliverable | Status |
|---|---|---|
| 0 | Architecture documents | done |
| 1 | Project foundation | done |
| 2 | Schema, migrations, profile CRUD, fact index | done |
| **3** | **Ingestion, normalization, dedupe** | **done** |
| 4 | Agent 1 — fit evaluation | next |
| 5 | Agent 1 — tailoring + claim verifier | planned |
| 6 | Agent 2 — packet, questions, approvals | planned |
| 7 | Agent 3 — monitoring, digests | planned |
| 8 | Permitted-channel submission, Terraform, AWS | planned |
