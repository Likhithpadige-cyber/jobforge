# JobForge M20 — private production runtime (design)

Status: **design only** (2026-11-10). Nothing here has been built, started or
deployed. It supersedes the cloud-oriented M15 blueprint for the owner's own
use; M15's migration, backup and security principles still apply.

## 1. Goal and non-goals

Run JobForge with **real personal data** on the owner's Mac, reachable only
from the owner's devices (M22 adds the iPhone over Tailscale), restartable
without data loss, and impossible to confuse with the development, demo or
test stacks.

Not in scope: public internet exposure, multi-user tenancy, high
availability, cloud hosting, live AI (stays `replay`, see §6), automatic
submission (stays `dry_run`), and any paid service.

## 2. Topology

```
iPhone / Mac browser
      │  HTTPS (Tailscale Serve, tailnet-only; M22)   or   http://127.0.0.1:8480 on the Mac
      ▼
 ┌──────────── compose project `jobforge-prod` ───────────────────────────────┐
 │  edge (Caddy)   127.0.0.1:8480 only        network: public, app            │
 │     ├─ /api/v1/*  → api:8000                                               │
 │     └─ everything else → web:3000                                          │
 │  web (Next.js standalone, no secrets)       network: app                   │
 │  api (FastAPI production image)             networks: app, data            │
 │  workers: outbox, scheduler, monitor,       network: data                  │
 │           application, render                                              │
 │  postgres 16 + pgvector   (volume)          network: data                  │
 │  redis (sessions/limits/streams, password)  network: data                  │
 │  minio (private artifact bucket, volume)    network: data                  │
 │  migrate (one-shot job, profile `release`)  network: data                  │
 └────────────────────────────────────────────────────────────────────────────┘
```

- **One published port**: `edge` on `127.0.0.1:8480`. No other service
  publishes a port; postgres, redis, minio, api and web are unreachable from
  the host network and the LAN. The port number is chosen to avoid the dev
  (3000/8000), demo (3100/8100) and test stacks.
- **Networks**: `app` and `data` are `internal: true` (no outbound internet).
  `public` exists only so `edge` can publish its loopback port. `web` is never
  on `data`, so the browser-facing process holds no database, Redis or object
  credentials. With no egress, a misconfigured worker cannot reach a model
  provider, an SMTP server or an employer.
- **Same origin**: the browser only ever talks to the edge origin, which
  keeps the host-only session and CSRF cookies working and avoids CORS. The
  web build must be made with `NEXT_PUBLIC_API_BASE_URL` equal to that origin
  (the dev proxy is not used in production).
- **Edge headers**: HSTS (only after HTTPS works through M22), a CSP tuned
  in report-only mode first, `X-Content-Type-Options`, `Referrer-Policy`,
  frame denial, request-body and time limits, no caching of authenticated
  responses or artifacts.

### Workers that run

| Worker | Runs | Why |
|---|---|---|
| outbox, scheduler, monitor | yes | event delivery, schedules, alerts |
| application, render | yes | deterministic and offline (question grounding, PDF/DOCX) |
| first-draft, optimizer, cover-letter | **no** | model-calling; live AI stays off |
| submission | **no** | submission stays `dry_run`; manual handoff is the path |

## 3. Persistence

| Data | Store | Volume (project-scoped) | Source of truth? |
|---|---|---|---|
| Domain data, audit, events | PostgreSQL | `jobforge-prod_pg_data` | **yes** |
| Résumé PDFs/DOCX, packets | MinIO bucket | `jobforge-prod_object_data` | **yes** (with DB manifest) |
| Sessions, rate limits, streams | Redis (AOF on) | `jobforge-prod_redis_data` | no — rebuildable |

- Volumes are named, never bind-mounted from the repository, and carry a
  `com.jobforge.env=prod` label. Production is never started with
  `docker compose down -v`, `make clean` or `make db-reset`; the runbook
  states this and the preflight (§4) refuses to run from a compose project
  name other than `jobforge-prod`.
- Images are built from a tagged commit and **tagged with the commit SHA
  before any recreate** (this Docker Desktop's containerd store silently
  garbage-collects untagged previous images; verify with `docker image ls`
  before claiming a rollback image exists). Keep the previous release's tags
  until the new one is verified.
- Logging: `json-file` with `max-size`/`max-file` limits; logs never contain
  secrets or résumé text (existing structured logging rules).
- Data at rest lives inside Docker Desktop's VM disk on the Mac. **FileVault
  must be on** before real data is loaded (owner action; not changed by
  Claude). Time Machine is not a database backup (M21).

## 4. Startup safeguards (to build in M20)

A `scripts/prod_preflight.py` run by the `migrate` job and by the API and
worker entrypoints before they serve. Each check fails closed:

1. `ENVIRONMENT=production`, compose project `jobforge-prod`, and the
   database carries a `jobforge_prod` marker comment (mirrors the demo
   marker), so a production process can never attach to the dev, demo or
   test databases, and vice versa.
2. Secrets present and not placeholders: postgres/runtime role passwords,
   `SESSION_SECRET`, `TOTP_ENCRYPTION_KEY` (currently **not** in the required
   list — fix), Redis password, object-store keys (not `minioadmin`).
3. `LLM_MODE=replay` unless a separately approved budget flag is present;
   `SUBMISSION_MODE=dry_run`; `EMAIL_TRANSPORT_MODE` fake/disabled.
   **Required code change:** `_require_secrets_outside_local` demands
   `anthropic_api_key` in production even in replay mode; it must require it
   only when `llm_mode != replay`, so production runs with no paid API key.
4. Alembic revision equals the code's head; the API refuses to start on a
   mismatch instead of running against an unmigrated schema. Migrations run
   only in the one-shot `migrate` job after a verified backup (M21), never
   on API startup.
5. Runtime roles are not superusers and the negative privilege checks pass
   (app_agent cannot write approvals or tracking columns, app_audit is
   column-scoped) — reuse the existing privilege tests' queries.
6. Owner account has TOTP enrolled (TOTP is mandatory outside local); the
   bootstrap creates the owner once, interactively, with the password typed
   by the owner, never printed or stored.
7. Object bucket exists, is private (no anonymous policy) and versioned.
8. Persisted submission-dispatch switch is false.

## 5. Secrets and configuration

- `prod.env` lives **outside the repository** (for example
  `~/Library/Application Support/JobForge/prod.env`, mode 600), generated by a
  script with `secrets.token_urlsafe`, never echoed, never committed. Compose
  reads it with `--env-file`. `TOTP_ENCRYPTION_KEY` and `SESSION_SECRET` are
  independent values.
- Recovery copies of `prod.env` (and the M21 backup key) are kept by the
  owner offline. Losing `TOTP_ENCRYPTION_KEY` makes TOTP secrets unreadable;
  losing the backup key makes backups useless.

## 6. Live AI and paid APIs

Live AI stays disabled: `LLM_MODE=replay`, no `ANTHROPIC_API_KEY` in
`prod.env`, model workers not started, and no egress from containers. In
replay mode new jobs cannot get AI drafts (the UI already says so); the
deterministic paths — import, evaluation, master résumé, question grounding,
review, export, manual handoff, tracking — all work. Enabling live AI later
is a separate owner decision with a budget, and needs an egress path for the
first-draft worker only.

## 7. Operations

- Start/stop with a wrapper (like `sco.sh`) that pins `-p jobforge-prod`,
  the prod compose file and the external env file, and refuses otherwise.
- Release: tag commit → build and tag images → verified backup (M21) →
  `migrate` job → recreate api/workers/web → smoke test through the edge
  (login with TOTP, CSRF-protected write, artifact checksum, dry-run refusal).
- Rollback: previous image tags; schema changes stay expand/contract so an
  application rollback needs no database downgrade. A database downgrade
  is a separately approved, restore-based procedure (M15 §4).
- Restart policy `unless-stopped`; Docker Desktop starting at login is an
  owner setting, not changed by Claude.

## 8. Security requirements checklist

- [ ] Only `127.0.0.1:8480` published; verified with `lsof -iTCP -sTCP:LISTEN`.
- [ ] `app`/`data` internal; a container egress probe fails.
- [ ] HTTPS origin in M22 (Secure cookies require it off-loopback).
- [ ] TOTP enrolled; reviewer accounts only if needed, also with TOTP.
- [ ] No source bind mounts, non-root images, read-only root FS where possible.
- [ ] FileVault on; macOS firewall on (owner actions).
- [ ] Preflight passes; failing any check stops the stack.
- [ ] "Sign out everywhere" exists before phone access (M22 §4).

## 9. Implementation steps (each a separate, reviewed change)

1. Config fix: API key only when `llm_mode != replay`; `TOTP_ENCRYPTION_KEY`
   required outside local; tests.
2. `docker-compose.prod.yml` + Caddyfile + wrapper; built only from
   production image targets.
3. `scripts/prod_preflight.py` + API startup revision check; tests.
4. Owner bootstrap script (interactive, TOTP enrolment).
5. Rehearsal on a throwaway `jobforge-prodtest` project with synthetic data,
   including restart, image rollback and the egress probe. Real data only
   after M21's first verified backup and restore drill.
