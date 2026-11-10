# JobForge — autonomous resume checkpoint

Read this together with `docs/PROJECT_STATE.md`. Written for a fresh session
with no prior conversation. Updated 2026-11-10 after M20 and M21.

## Where

- Worktree: `/Users/padige/Projects/jobforge-c3-design-review/jobforge-overnight`
- Branch: `overnight/m16.6-m17` (local only; never pushed)
- Latest commit: `git log --oneline -1` (this checkpoint follows `b448c56`)
- Base: local `dev` = GitHub `main` = `6945454` (fast-forward ancestor).
  Local `main` (`cbe0d96`) is a stale pointer; ignore it.
- Evidence, runners and logs (outside the repo):
  `/Users/padige/Projects/jobforge-c3-design-review/jobforge-overnight-evidence/`

## Status by milestone (CODE COMPLETE → LOCALLY VERIFIED → INTEGRATED → DEPLOYED → ACCEPTED)

| Milestone | State |
|---|---|
| M16.6 export security, M17 core UI, M19 tracking | locally verified; not integrated |
| M20 private runtime | locally verified in the `jobforge-prodtest` rehearsal (`docs/verification/M20_REHEARSAL.md`); not deployed |
| M21 backups/restore | locally verified by an isolated restore drill incl. real sign-in (`docs/verification/M21_RESTORE_DRILL.md`); no schedule installed |
| M22 private iPhone access | app side: sign-out-everywhere done; Tailscale runbook not written yet; owner actions pending |

Key files: `docker-compose.prod.yml`, `infra/caddy/Caddyfile`,
`scripts/prod.sh` (wrapper), `scripts/prod_env.py` (generate-env, init,
bootstrap-owner, preflight), `packages/core/db/schema_guard.py`,
`scripts/backup_crypto.py`, `scripts/backup.py`, `scripts/restore.py`.

## Unfinished, in order

1. **Next:** M22 documentation — `docs/deployment/M22_TAILSCALE_RUNBOOK.md`:
   install (owner), `tailscale serve --bg --https=443 http://127.0.0.1:8480`
   (never Funnel), MagicDNS/HTTPS certs, ACL to owner devices on 443, set
   `CORS_ORIGINS=["https://<mac>.<tailnet>.ts.net"]` and verify Secure
   cookies over HTTPS, iPhone procedure (Add to Home Screen), lost-device
   steps (remove device → Settings › Sign out everywhere → rotate password),
   troubleshooting, acceptance checklist.
2. M21 operator runbook: `docs/deployment/M21_BACKUP_RUNBOOK.md` (keygen,
   manual backup before releases, offline key + prod.env copy, restore
   steps, launchd plist as a *proposal only* — installing it needs approval).
   Preflight `backup_ready` reads `JOBFORGE_LAST_VERIFIED_BACKUP`; decide how
   the backup runner records it (e.g. a marker the wrapper passes in).
3. Product: show the stored fit score on the job page; master résumé
   PDF/DOCX text extraction if feasible; mobile Playwright for tracking.
4. Full regression + Playwright at the end; update PROJECT_STATE.

## Last verified tests

- Full backend run `run_full_7` (before M21 code): 3,726 passed + 1 error
  (ResourceWarning from an orphaned event loop, fixed in `123801a`);
  safety 1,764 + 1 error (same cause); API E2E 6; readiness 22/22; H3
  100/100; 0 provider calls. `run_full_8` was started after `b448c56`; check
  `../jobforge-overnight-evidence/run_full_8.log`.
- Unit suite after the fix: 2,213 passed. Targeted M20/M21 suites pass.
- Frontend: 416 passed (37 files), typecheck and lint clean (at `805e761`).
- Playwright: 32/32 at `70a0eea`; not re-run since (Settings card and the
  same-origin build mode changed; re-run on the ovn demo is pending).

## Services and data

- All JobForge rehearsal/drill stacks are **stopped**, volumes kept
  (synthetic only): `jobforge-prodtest`, `jobforge-restoretest{,2,3,4}`.
  Removing them needs the owner's approval.
- Throwaway test services `m166f-ci-{postgres,redis,minio}` (55721/56721/9721)
  run for regressions; stop with `docker stop` afterwards (never `rm`/`-v`).
- Images: `jobforge-prod-{api,web,worker}` tags `c11ff6006b3b`, `b98a1383bedc`,
  `62ac2c04134b`, `c72ba2afbf15` (local only).
- Drill secrets (session scratchpad, may vanish):
  `/private/tmp/claude-501/…/scratchpad/{prodtest,drill}/`.
- Never touch: dev stack (`jobforge-*` on 5432), live demo (`jobforge-demo-*`,
  55433), `jobforge-m166-*` review stacks.

## Pending approvals

Push branch / hosted CI · fast-forward `dev` and `main` · live AI and budget ·
removal of rehearsal/drill stacks, volumes, images and scratch worktrees ·
FileVault, firewall, Tailscale install and admin settings · launchd backup
schedule · real deployment and real data.

## Resume commands

```sh
cd /Users/padige/Projects/jobforge-c3-design-review/jobforge-overnight
git status && git log --oneline -8
docker start m166f-ci-postgres m166f-ci-redis m166f-ci-minio   # throwaway only
bash ../jobforge-overnight-evidence/run_targeted.sh tests/unit/test_backup_crypto.py
bash ../jobforge-overnight-evidence/run_full.sh > ../jobforge-overnight-evidence/run_full_N.log 2>&1
(cd apps/web && npx vitest run && npm run -s typecheck && npm run -s lint)
```

Python: `/Users/padige/Projects/jobforge-c3-design-review/jobforge-phase5/.venv/bin`
(no `uv`). zsh does not word-split `$VAR`; use literal paths. Type-check
scripts as modules: `mypy -m scripts.backup -m scripts.restore -m scripts.prod_env`.
Docker's VM has ~4 GB: run one JobForge stack at a time.

## Uncommitted changes

None at checkpoint time.
