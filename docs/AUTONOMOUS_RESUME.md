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

**M20 private runtime: rehearsed.** Steps 1–3 and step 5 (rehearsal) done;
see `docs/verification/M20_REHEARSAL.md`. Sign-out-everywhere (M22
prerequisite) done. Next: production owner bootstrap (step 4), then M21.

## Completed (locally verified, not integrated)

M16.6 export security · M17 core UI · M19 tracking essentials · integration
review (GO after approval + hosted CI) · M20 config fixes · production
compose/edge/wrapper · schema guard · prod_env generate-env/init/preflight ·
`jobforge-prodtest` rehearsal (3 defects found and fixed) · sign out everywhere.

## Unfinished, in order

1. **Next:** M20 step 4 — owner bootstrap inside the production image
   (interactive: email, password typed by the owner, TOTP enrolment shown
   once; never printed to logs), wrapper command `bootstrap-owner`, tests.
   Check `scripts/setup_demo_auth.py` for reusable TOTP enrolment code.
2. M21: `scripts/backup.py` (pg_dump custom format from a one-shot container
   on the data network + artifact volume tar, encrypted with a public key;
   integrity manifest; retention; disk check) and restore into a NEW project
   only; then a real isolated restore drill on `jobforge-prodtest` data.
   Encryption tool decision pending: `age` is not installed; prefer Python
   `cryptography` (already a dependency) with an RSA/X25519 hybrid, or ask.
3. M22: Tailscale runbook (owner actions), mobile Playwright for tracking.
4. Product: fit score on the job page; master résumé PDF/DOCX extraction.

## Last verified tests (2026-11-10, isolated throwaway services)

Full backend 3,649 passed + 1 flaky failure (fixed in `a9f4458`, then 11/11 ×3);
safety 1,671; API E2E 6; readiness 22/22; H3 100/100; 0 provider calls;
ruff + mypy (172 files) clean. Frontend 410 and Playwright 32/32 were last
run at `70a0eea` (no web changes since).

## Services

- Throwaway test services `m166f-ci-{postgres,redis,minio}` (55721/56721/9721):
  may be running; stop with `docker stop` (never `rm`, never `-v`).
- Rehearsal `jobforge-prodtest` (port 8481): **stopped**, volumes kept
  (synthetic only). Drive it only via
  `../jobforge-overnight-evidence/prodtest.sh` with `JOBFORGE_RELEASE=b98a1383bedc`
  (or rebuild: `JOBFORGE_RELEASE=$(git rev-parse --short=12 HEAD) …prodtest.sh build`).
  Its env file is in the session scratchpad and may be gone in a new session:
  regenerate with `python -m scripts.prod_env generate-env <path outside repo>`
  and set `CORS_ORIGINS=["http://127.0.0.1:8481"]` — a new env means new
  passwords, so the old prodtest volumes would need `init` again.
- Throwaway demo `jobforge-ovn`: stopped.
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
