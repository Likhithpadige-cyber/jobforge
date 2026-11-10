# JobForge project state

Updated: 2026-11-10 (fifth session: M20 step 1 done). Resume checkpoint: `docs/AUTONOMOUS_RESUME.md`. Keep this short; it is the handover
for the next session. Verified means executed in this session, not reported.

## Where things are

| Ref | Commit | Meaning |
|---|---|---|
| GitHub `main` (private) / local `dev` | `6945454` | M16.5 complete; hosted CI 6/6 + Playwright 26/26 |
| `feature/p0-resume-export` | `69fca08` | Codex's M16.6 résumé export (10 commits). Not modified here. |
| `overnight/m16.6-m17` (local only) | see `git log -1` | fast-forward of `dev`: M16.6 merge + M17 + M19 + M20–M22 docs |
| local `main` | `cbe0d96` | stale pointer (68 behind GitHub `main`); ignore |

Worktree for the overnight branch:
`/Users/padige/Projects/jobforge-c3-design-review/jobforge-overnight`.
Evidence, runners and logs: `../jobforge-overnight-evidence/` (outside the repo).

## Last verified milestone

**M16.6 résumé export — locally verified (GO for an approved integration).**

- H3 residual fixed: an edited-and-re-verified cited fact (and a
  structured-data-only edit) is now refused for new renders, manual
  packages, cached packages and PDF/DOCX downloads. Unverify, edit,
  expire, retire and delete are refused; unchanged evidence and
  unverify-then-re-verify with identical content still work.
- H1/H2, M1/M2/M4/M5 unchanged from the 2026-11-06 review (fixed).
- On the overnight merge (all executed 2026-11-10, isolated services):
  full backend 3,535 passed / 0 skipped; safety 1,642; API E2E 6; main's
  original 22 readiness tests 22/22 (byte-identical file); Codex's 100 new
  H3/source-repair tests pass (on b9031f6 they fail at import); offline
  evaluation 0 provider calls; frontend 386; Playwright 28/28.

## Integration recommendation

**GO** to fast-forward `dev` and then `main` to this branch, after owner
approval and a green hosted CI run (push the branch first, not `dev`).
**NO-GO** for real personal data until M20 and M21 are built and a restore
drill passes. Full review: `docs/verification/INTEGRATION_REVIEW_M16.6_M17_M19.md`.

## Verification (branch tip, 2026-11-10, isolated throwaway services)

Backend 3,587 passed / 0 skipped; safety 1,659; API E2E 6; `dev`'s original
22 readiness tests 22/22; H3 regressions 100/100 (still fail at import on
b9031f6, as intended); offline eval 0 provider calls; schema verification
12/12; frontend 410 (35 files); Playwright 32/32 against the throwaway
`jobforge-ovn` demo (at 0011). Typecheck, ESLint and ruff clean. Hosted CI
not run (nothing pushed).

## M17 core workflow UI — complete (locally)

- Owner-pasted job import (approved 2026-11-10): owner-only
  `POST /jobs/manual` under the internal `owner-pasted` source, no
  employer-domain policy because the link is never fetched. Bulk CSV/JSON
  and connectors keep full site-policy enforcement.
- Master résumé import from pasted text or .txt/.md, editable preview.
- Résumé Studio export with a client-side SHA-256 check.
- First-draft panel on the job page. `GET /settings/runtime-modes` drives
  an explicit banner: **replay** = "demonstration only, live AI disabled"
  (a draft exists only for recorded fixtures; a new job is refused by the
  worker, nothing invented); **live** = "may be billable". Prerequisites
  (profile, master résumé via `GET /profiles/{id}/master-resumes/latest`,
  evaluation) are listed when missing. Reviewers see owner-only text.
- Question preparation at `resume_ready`. The e2e fixture now builds a
  run-unique synthetic application left at `resume_ready`. The demo runs no
  application worker (it would also need an artifact store), so the browser
  journey covers UI→API; the worker step is covered by an integration test
  that rebuilds questions from the event payload.
- Workspace shows "Questions have not been prepared yet" (not "0
  questions") at `resume_ready`; the job page's fit-score notice no longer
  claims the evaluation endpoint is missing (the score is just not shown).

Still open: showing the fit score/matched requirements on the job page;
PDF/DOCX text extraction for the master résumé; a browser download as the
seeded profile owner (covered by API/integration tests).

## M19 application tracking — essentials done

- Migration `0011_application_tracking`: `applied_on`, `follow_up_on`;
  CHECK outcome-beyond-pending needs an applied date; follow-up index.
- Uses the orthogonal `outcome` enum (ARCHITECTURE §7.3).
- `GET/PATCH /applications/{id}/tracking` (PATCH owner + CSRF; never
  touches pipeline status or `submitted_at`; outcome change emits
  `application.outcome.updated`, source "human"); `GET /follow-ups`.
- `GET /applications?outcome=&applied=` server-side filters; rows carry
  outcome, applied date, `applied_via` and follow-up.
- Manual submissions: `applied_via` = "jobforge" only when the approved
  pipeline set `submitted_at`, "manual" when only the owner's date exists; a
  manual handoff packet is never shown as sent. "I sent it today (follow up
  in 1 week)" on the Tracking card; recording an outcome on a
  JobForge-submitted application fills the applied date from `submitted_at`.
- Follow-ups: presets (3 days / 1 week / 2 weeks / none); dashboard Done and
  Snooze 1 week (owner-only); reviewers read-only.

Not built: tracking for jobs without an application row; automatic
`no_response` aging; reminders by notification; mailbox signals.

## M20 step 1 — production configuration (done, locally verified)

Staging/production: `postgres_password`, `session_secret`, valid
`totp_encryption_key` (strict base64, 32 bytes) always required; the
Anthropic key only for `record`/`live`. Startup errors no longer echo config
input (`hide_input_in_errors`). API and workers refuse to start on bad
secrets without leaking values; replay starts with no provider key
(process-level tests). Local/test unchanged.

## M20–M22 (design; only step 1 built)

- `docs/deployment/M20_PRIVATE_RUNTIME_DESIGN.md`: `jobforge-prod` compose
  project, single loopback edge port, internal no-egress networks, named
  volumes, fail-closed preflight. Two config fixes required first: no
  Anthropic key needed in replay; `TOTP_ENCRYPTION_KEY` required outside
  local.
- `docs/deployment/M21_M22_PLAN.md`: encrypted (age) pg_dump + artifact
  mirror, verification, monthly restore drill into a new project; Tailscale
  Serve (never Funnel) with "sign out everywhere" as a prerequisite.

## Outstanding blockers and decisions awaiting approval

1. Integration: approve pushing the branch for hosted CI, then
   fast-forwarding `dev` and `main` (procedure in the integration review).
2. Live AI stays off. Enabling it later needs a budget decision.
3. Cleanup of review/test stacks and scratch worktrees. Nothing deleted.
4. M20/M22 owner actions: FileVault, macOS firewall, Tailscale install and
   admin-console settings; launchd backup schedule (M21) needs approval.
5. Bulk-import sources stay disabled pending ToS review; pasted postings
   need no source.

## Next three tasks

1. M20 step 2: production compose, edge and wrapper with static tests.
2. M20 step 3: preflight + API revision guard.
3. Sign out everywhere (M22 prerequisite).

## Resume commands

```sh
cd /Users/padige/Projects/jobforge-c3-design-review/jobforge-overnight
git status && git log --oneline -5
(cd apps/web && npx vitest run)                  # frontend, ~410 tests
../jobforge-overnight-evidence/sco.sh ps         # isolated demo (ports 55883/56883/8383)
bash ../jobforge-overnight-evidence/run_full.sh  # full backend on throwaway services (55721/56721/9721)
bash ../jobforge-overnight-evidence/run_targeted.sh tests/integration/test_owner_pasted_import.py  # targeted
```

Never point tests at the development (5432) or live demo (55433) databases.
