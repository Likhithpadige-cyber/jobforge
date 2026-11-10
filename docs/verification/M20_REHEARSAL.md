# M20 rehearsal — `jobforge-prodtest` (2026-11-10)

Throwaway, production-like run of `docker-compose.prod.yml` through
`scripts/prod.sh`, with a generated synthetic env file outside the
repository, project `jobforge-prodtest`, port 8481. No real data; live AI
off; nothing deployed. Images built locally from commit `b98a138`
(`jobforge-prod-{api,web,worker}:b98a1383bedc`; the earlier `c11ff6006b3b`
images are kept).

## Defects the rehearsal found (fixed in `b98a138`)

1. Generated `CORS_ORIGINS` was a bare URL; pydantic-settings decodes list
   settings as JSON → every process failed to start.
2. Worker image lacked `alembic.ini`/`migrations/`, so the new schema guard
   failed closed in every worker.
3. Caddy's binary carries a file capability; with `cap_drop: ALL` it could
   not exec. `NET_BIND_SERVICE` added back for the edge only.

## Results (all executed)

| Check | Result |
|---|---|
| Cold start from empty volumes: `up postgres redis` → `migrate` (empty → 0011) → `init` → `up` | all 10 services up; api, web, edge, postgres, redis healthy |
| Published ports | only `127.0.0.1:8481->8080` (edge); `prod.sh check-ports` passes |
| `prod.sh preflight --bootstrap` | 20/20 fatal checks pass; 1 expected warning (no verified backup yet, M21) |
| Edge routing | `/login`, `/api/health`, `/api/v1/health` 200; `/health`, `/docs`, `/openapi.json` 404 |
| Egress from api, render, application workers | `1.1.1.1:443` unreachable, `api.anthropic.com` does not resolve |
| Egress from web; web → postgres | unreachable; web cannot reach the data network |
| Persistence across full `stop`/`start` | synthetic DB row and synthetic artifact file intact |
| Artifact volume ownership | `drwx------ app app` (inherited from the image) |
| Process exit (TERM to PID 1) in api and render worker | restarted automatically (`unless-stopped`) |
| Missing `TOTP_ENCRYPTION_KEY` in the production image | refused: "Missing required secrets … totp_encryption_key" |
| `LLM_MODE=live` without a key | refused: "… anthropic_api_key" |
| Schema mismatch (alembic_version set to 0010, then restored) | API refuses to serve (502 via edge, healthcheck not healthy); healthy again once restored |
| Redis stopped / restarted | API reports `degraded`; recovers automatically; workers stay up |

Notes:

- `docker kill` is a manual stop to Docker and is deliberately not
  restarted; a crash test must make the process exit by itself.
- With `uvicorn --workers 2`, a refused startup keeps the supervisor alive
  and respawning workers: nothing is served and health is not "healthy",
  but the container shows "running". Accepted; preflight and health show
  the failure.
- Workers have no HTTP healthcheck; liveness is the monitor's heartbeat.

## Not covered yet

Owner bootstrap with TOTP in the production image (needed before real use);
HTTPS via Tailscale Serve (M22, owner setup); backup/restore (M21).

The rehearsal stack is stopped, not removed; its volumes contain only
synthetic data. Removing them needs separate approval.
