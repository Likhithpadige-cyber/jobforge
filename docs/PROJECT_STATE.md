# JobForge project state

Updated: 2026-11-10 (second session, after the overnight run). Keep this short; it is the handover
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

## M17 core workflow UI — in progress

Latest verification (branch tip, 2026-11-10, isolated services): backend
3,554 passed / 0 skipped; safety 1,656; API E2E 6; main's 22 readiness 22/22;
H3 regressions 100/100; offline eval 0 provider calls; frontend 391;
Playwright 28/28 (plus the 3 M17 journeys run separately: 3/3).

Done:

- Owner-pasted job import (approved decision, 2026-11-10): owner-only
  `POST /jobs/manual` records one typed/pasted posting under the internal
  `owner-pasted` source without an employer-domain site policy, because the
  link is never fetched. Bulk CSV/JSON import and connectors keep full
  site-policy enforcement; the exemption is refused for any other source and
  the reserved source name cannot be registered. Also fixed: successful
  `POST /jobs/import` responses previously raised a 500.
- Master résumé import from pasted text or a .txt/.md file with an editable,
  sectioned preview (`POST /profiles/{id}/master-resumes`); nothing verified.

- Job Feed "Add a job": paste a posting or upload CSV/JSON via the existing
  `POST /jobs/import`. Owner-only. The posting link is never fetched.
- Resume Studio export: download the current version's PDF/DOCX through
  `GET /artifacts/{id}/content`, with a client-side SHA-256 check.
- Application workspace: owner enters the employer's questions (or "No
  extra questions") at `resume_ready`; `POST /applications/{id}/prepare`
  grounds answers offline.
- Playwright: pasted import success, validation and reviewer denial, export
  panel with the server's ownership refusal.

Not done yet: first-draft trigger (needs live AI or recorded replay; must
show an honest disabled/demo state); a browser journey for question
preparation (no seeded application reaches `resume_ready`); a successful
contact-bearing download in the browser (the suite never signs in as the
seeded profile owner; covered by API/integration tests); PDF/DOCX text
extraction for the master résumé; M19 tracking.

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

1. First-draft entry point with an honest replay/disabled state (no live AI).
2. A deterministic `resume_ready` demo fixture, then a question-preparation
   browser journey.
3. M19 tracking: applied/interview/rejected/offer, follow-up date and notes,
   due-follow-ups list.

## Resume commands

```sh
cd /Users/padige/Projects/jobforge-c3-design-review/jobforge-overnight
git status && git log --oneline -5
(cd apps/web && npx vitest run)                  # frontend, ~391 tests
../jobforge-overnight-evidence/sco.sh ps         # isolated demo (ports 55883/56883/8383)
bash ../jobforge-overnight-evidence/run_full.sh  # full backend on throwaway services (55721/56721/9721)
bash ../jobforge-overnight-evidence/run_targeted.sh tests/integration/test_owner_pasted_import.py  # targeted
```

Never point tests at the development (5432) or live demo (55433) databases.
