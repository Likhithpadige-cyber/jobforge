# JobForge — autonomous resume checkpoint

Read this together with `docs/PROJECT_STATE.md`. Written for a fresh session
with no prior conversation. Update at every milestone.

## Where

- Worktree: `/Users/padige/Projects/jobforge-c3-design-review/jobforge-overnight`
- Branch: `overnight/m16.6-m17` (local only; never pushed)
- Latest commit: run `git log --oneline -1` (checkpoint written at `ca8af44`
  + this file's commit)
- Base: local `dev` = GitHub `main` = `6945454` (fast-forward ancestor).
  Local `main` (`cbe0d96`) is a stale pointer; ignore it.
- Evidence and runners (outside the repo):
  `/Users/padige/Projects/jobforge-c3-design-review/jobforge-overnight-evidence/`

## Current milestone

**M20 private runtime.** Steps 1–3 done and tested statically/integration-level
(config fixes; compose + edge + wrapper; schema guard + env/init/preflight).
Next: step 5 rehearsal on `jobforge-prodtest` (images never built or started yet).

## Completed (locally verified, not integrated)

M16.6 export security · M17 core UI · M19 tracking essentials · integration
review (GO after approval + hosted CI) · M20 design · M21/M22 plan ·
M20 step 1 (no AI key in replay, valid TOTP key required, startup errors do
not echo config values).

## Unfinished, in order

1. ✅ M20 step 2: `docker-compose.prod.yml`, `infra/caddy/Caddyfile`, `scripts/prod.sh`
   (31 static/wrapper tests). Artifacts use the filesystem store on a named
   volume (no MinIO: unpatched advisories).
2. ✅ M20 step 3: `packages/core/db/schema_guard.py` (API + workers refuse a
   schema mismatch in staging/production); `scripts/prod_env.py`
   generate-env / init / preflight; wrapper `init`, `preflight`, `check-ports`.
   **Next (step 5):** rehearsal: `python -m scripts.prod_env generate-env
   /tmp/…/prodtest.env` (outside repo), then with
   `JOBFORGE_PROD_PROJECT=jobforge-prodtest JOBFORGE_PROD_PORT=8481`:
   `prod.sh build`, `up -d postgres redis`, `migrate`, `init`, `up`,
   `preflight --bootstrap`; verify cold start, restart, persistence, egress
   probe, missing-secret refusal. Synthetic data only.
3. M22 prerequisite: "sign out everywhere" (per-user session revocation) +
   Settings UI + tests.
4. M20 step 5: rehearsal on a throwaway `jobforge-prodtest` project with
   synthetic data (cold start, restart, persistence, egress probe).
5. M21: backup/restore scripts (age-encrypted pg_dump + artifact mirror),
   verification, isolated restore drill.
6. Product: show stored fit score on the job page; master résumé PDF/DOCX
   extraction if feasible.

## Last verified tests (2026-11-10, isolated throwaway services)

Full backend 3,649 passed + 1 flaky failure (fixed in `a9f4458`, then 11/11 ×3);
safety 1,671; API E2E 6; readiness 22/22; H3 100/100; 0 provider calls;
ruff + mypy (172 files) clean. Frontend 410 and Playwright 32/32 were last
run at `70a0eea` (no web changes since).

## Services

- Throwaway test services `m166f-ci-{postgres,redis,minio}` (55721/56721/9721):
  may be running; stop with `docker stop` (never `rm`, never `-v`).
- Throwaway demo `jobforge-ovn` (via evidence `sco.sh`): stopped.
- Never touch: dev stack (`jobforge-*`, 5432), live demo (`jobforge-demo-*`,
  55433), `jobforge-m166-*` review stacks.

## Pending approvals (do not do without explicit owner approval)

Push branch / hosted CI · fast-forward `dev` and `main` · live AI and budget ·
cleanup of stacks/worktrees/volumes · FileVault, firewall, Tailscale install
and admin settings · launchd backup schedule · any real deployment.

## Resume commands

```sh
cd /Users/padige/Projects/jobforge-c3-design-review/jobforge-overnight
git status && git log --oneline -5
docker start m166f-ci-postgres m166f-ci-redis m166f-ci-minio   # throwaway only
bash ../jobforge-overnight-evidence/run_targeted.sh tests/unit/test_config.py
bash ../jobforge-overnight-evidence/run_full.sh > ../jobforge-overnight-evidence/run_full_N.log 2>&1
(cd apps/web && npx vitest run && npm run -s typecheck && npm run -s lint)
```

Python: `/Users/padige/Projects/jobforge-c3-design-review/jobforge-phase5/.venv/bin`
(no `uv` on PATH). zsh does not word-split `$VAR`; use `${=VAR}` or literal paths.

## Uncommitted changes

None at checkpoint time. If any exist on resume, inspect with `git diff`
and commit or stash them; never discard.
