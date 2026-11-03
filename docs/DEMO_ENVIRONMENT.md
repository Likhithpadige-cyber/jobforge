# Isolated demo environment

A self-contained JobForge stack with fictional data, for demonstrations and
browser verification. It is a separate Docker Compose project and cannot
reach, reuse or modify the development stack or any other local stack.

## What is isolated

| | Development stack | Demo stack |
|---|---|---|
| Compose project | `jobforge` (`docker-compose.yml`) | `jobforge-demo` (`docker-compose.demo.yml`) |
| Containers | `jobforge-*` | `jobforge-demo-*` |
| PostgreSQL | port 5432, database `jobforge` | `127.0.0.1:55433`, database `jobforge_demo`, own cluster and roles |
| Redis | port 6379 | `127.0.0.1:56380` |
| API / web | 8000 / 3000 | `127.0.0.1:8100` / `127.0.0.1:3100` |
| Volumes | `jobforge_*` | `jobforge-demo_demo_*` |
| Configuration | `.env` | `.env.demo` only (git-ignored, mode 600) |

Use `http://127.0.0.1:3100` (not `localhost`) for the demo. Cookies are
scoped to the host name, so the demo session never shares a cookie with a
development session on `localhost`.

## Safety configuration

`.env.demo` is generated with `ENVIRONMENT=local`, `LLM_MODE=replay`, no
provider API key, `SUBMISSION_MODE=dry_run` and `EMAIL_TRANSPORT_MODE=fake`.

- No generation, submission or render worker is defined in the demo
  compose file. Only the outbox relay, scheduler and monitor run, so the
  stack cannot call a model or send email even if a setting were changed.
- All secrets are random, local-only, and generated on your machine. Nothing
  is copied from `.env`.

### Identity guards

`scripts/demo_env.py` runs every database operation (`check`, `init`, `seed`)
only after verifying all of the following:

- `JOBFORGE_DEMO_ENVIRONMENT=1`, `ENVIRONMENT=local`, replay LLM mode,
  dry-run submission, the fake email transport, and no provider key;
- that the configured and the **connected** database are both `jobforge_demo`;
- for `seed`, that the database carries the demo marker comment. Only `init`
  writes that marker, and only on `jobforge_demo`.

Every Makefile `demo-*` target additionally refuses to run unless `.env.demo`
contains `JOBFORGE_DEMO_ENVIRONMENT=1`. No demo target drops, truncates,
resets or deletes anything, and none removes a volume.

## Demo data (fictional)

- The existing fictional seeds: `scripts/seed.py` (profile, facts, jobs) and
  `scripts/seed_resume_studio_demo.py` (an application with a tailored resume).
- Accounts:
  - `demo.candidate@example.com`: owner;
  - `demo.reviewer@example.com`: reviewer.

  Their passwords are the random `DEMO_OWNER_PASSWORD` and
  `DEMO_REVIEWER_PASSWORD` values in your `.env.demo`.
- Synthetic incidents:
  - Two critical fabrication incidents created through the real Phase 1
    guardrail path, which pause generation.
  - One dead-lettered delivery: a synthetic `resume.render.requested` event
    with a `cg.render` dead-letter record. The seed creates the *delivery*,
    not the alert; the real monitor raises the dead-letter alert on its next
    tick, so the dashboard count and the alert always agree. No render worker
    runs in the demo, so it is never re-delivered.
  - 60 resolved historical alerts (labelled `synthetic: true`), to exercise
    history and pagination.

  No ambiguous-submission incident is seeded, because there is no real
  submission behind one. It will be demonstrable through the submission flow.
- Monitoring snapshots are produced live by the real scheduler and monitor
  workers.

The seed is idempotent; running it again changes nothing.

## Setup

```bash
make demo-env       # create .env.demo (never overwrites an existing one)
make demo-up        # build and start postgres, redis, api, web
make demo-migrate   # identity check, then alembic upgrade head on jobforge_demo
make demo-init      # mark the database, set demo role passwords
make demo-seed      # fictional data and demo accounts
make demo-workers   # outbox relay, scheduler, monitor
```

Then open `http://127.0.0.1:3100` and sign in with an account above.
Monitoring data appears after the first 5-minute scheduler tick.

`make demo-status` and `make demo-logs SVC=api` inspect the stack.

### If the web container is slow to compile

On a Docker Desktop VM with little free memory, the Next.js development
server inside `jobforge-demo-web` can stall on its first compile, and bind
mounts can fail with `ENOMEM`. Containers with `restart: unless-stopped`
recover by themselves. For the web UI, run the same dev server on the host
instead, still talking only to the demo API:

```bash
make demo-web-host  # stops jobforge-demo-web, serves 127.0.0.1:3100 from the host
```

The demo database, Redis, API and workers stay in the `jobforge-demo` project.

## Teardown

```bash
make demo-down      # stop and remove demo containers and network; volumes are kept
```

If you used `make demo-web-host`, stop it with Ctrl+C.

`make demo-up` brings the same data back. Deleting the demo data is
deliberately not a Makefile target. If you decide to discard it, do so
yourself after checking the names:

```bash
docker volume ls --filter name=jobforge-demo_
docker compose -p jobforge-demo -f docker-compose.demo.yml --env-file .env.demo down --volumes
```

That command removes only volumes labelled with the `jobforge-demo` project.
Never run it without `-p jobforge-demo`.

## Limits

- Replay mode without cassettes: generating new resumes or cover letters is
  not available in the demo. Seeded content covers review flows.
- The web container runs the Next.js development server, proxied to the demo
  API through `API_PROXY_TARGET`.
