# JobForge project state

Updated: 2026-11-10 (third session: M17 completed, M19 started). Keep this short; it is the handover
for the next session. Verified means executed in this session, not reported.

## Where things are

| Ref | Commit | Meaning |
|---|---|---|
| GitHub `main` (private) / local `dev` | `6945454` | M16.5 complete; hosted CI 6/6 + Playwright 26/26 |
| `feature/p0-resume-export` | `69fca08` | Codex's M16.6 résumé export (10 commits). Not modified here. |
| `overnight/m16.6-m17` (local only) | see `git log -1` | `6945454` + merge of `69fca08` + M17 UI work |

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

## M17 core workflow UI — complete (locally)

Latest verification: see "Verification" below.

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

## M19 application tracking — started (first slice done)

- Migration `0011_application_tracking`: `applications.applied_on`,
  `follow_up_on` (DATE); CHECK that an outcome beyond `pending` has an
  applied date; partial index for follow-ups. Not in app_agent's grant.
- Uses the existing orthogonal `outcome` enum (ARCHITECTURE §7.3):
  pending (shown as "Applied" once dated), acknowledged, screening,
  interviewing, offer, accepted, rejected, no_response, withdrawn.
- `GET/PATCH /applications/{id}/tracking` (PATCH owner-only + CSRF; never
  touches pipeline status or `submitted_at`; emits
  `application.outcome.updated` with source "human" on outcome change);
  `GET /follow-ups?on=&within_days=` (excludes accepted/rejected/withdrawn
  and abandoned).
- Web: Tracking card in the application workspace (reviewers read-only);
  Follow-ups card on the dashboard (due now + next 7 days, browser date).

Not built yet: tracking for jobs that have no application row; outcome
filter/column in the applications list; automatic `no_response` aging;
mailbox signals; reminders by email/notification.

## Outstanding blockers and decisions awaiting approval

1. Approve integrating M16.6 into `dev`/`main` (merge commit; fast-forward is
   impossible), then hosted CI.
2. Decide whether/when to enable live AI and its budget. Until then tailoring
   of new real jobs cannot run (replay only covers recorded fixtures).
3. Approve cleanup of review/test stacks and scratch worktrees (list in the
   overnight report). Nothing has been deleted.
4. Bulk-import sources can be registered with `POST /sources` but are always
   created disabled pending a recorded ToS review, and there is no API/UI to
   enable one. Pasted postings do not need a source (internal owner-pasted).
5. Private deployment prerequisites (FileVault, firewall) are owner actions.
6. Resolved 2026-11-10: owner-pasted postings are exempt from site
   policies (approved); everything automated stays policy-gated.

## Next three tasks

1. Approval to integrate `overnight/m16.6-m17` into `dev` (merge, then
   hosted CI); nothing has been pushed.
2. M19 next slice: outcome column/filter on the applications list and
   tracking for applications recorded outside the pipeline.
3. Show the stored deterministic fit score on the job page.

## Resume commands

```sh
cd /Users/padige/Projects/jobforge-c3-design-review/jobforge-overnight
git status && git log --oneline -5
(cd apps/web && npx vitest run)                  # frontend, ~404 tests
../jobforge-overnight-evidence/sco.sh ps         # isolated demo (ports 55883/56883/8383)
bash ../jobforge-overnight-evidence/run_full.sh  # full backend on throwaway services (55721/56721/9721)
bash ../jobforge-overnight-evidence/run_targeted.sh tests/integration/test_owner_pasted_import.py  # targeted
```

Never point tests at the development (5432) or live demo (55433) databases.
