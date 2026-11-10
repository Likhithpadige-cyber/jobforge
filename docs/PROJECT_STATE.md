# JobForge project state

Updated: 2026-11-10 (overnight session). Keep this short; it is the handover
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
- Full backend suite on the overnight merge: 3,535 passed, 0 skipped.
  Offline evaluation: 0 provider calls. See the overnight report for the
  safety, legacy-readiness, before/after and Playwright results.

## M17 core workflow UI — in progress

Done (frontend only; backend unchanged):

- Job Feed "Add a job": paste a posting or upload CSV/JSON via the existing
  `POST /jobs/import`. Owner-only. The posting link is never fetched.
- Resume Studio export: download the current version's PDF/DOCX through
  `GET /artifacts/{id}/content`, with a client-side SHA-256 check.

Not done yet: master résumé import UI, application-question preparation UI
(`POST /applications/{id}/prepare` is deterministic, safe to wire next),
first-draft trigger (needs live AI or recorded replay; must show an honest
disabled/demo state), Playwright journeys for the new screens.

## Outstanding blockers and decisions awaiting approval

1. Approve integrating M16.6 into `dev`/`main` (merge commit; fast-forward is
   impossible), then hosted CI.
2. Decide whether/when to enable live AI and its budget. Until then tailoring
   of new real jobs cannot run (replay only covers recorded fixtures).
3. Approve cleanup of review/test stacks and scratch worktrees (list in the
   overnight report). Nothing has been deleted.
4. Job sources can only be listed, not created, through the API; a real
   deployment needs a manual-import source row (no UI for this yet).
5. Private deployment prerequisites (FileVault, firewall) are owner actions.

## Next three tasks

1. Application-question preparation UI on the application page + tests.
2. Master résumé import UI (`POST /profiles/{id}/master-resumes`) + review.
3. Playwright journeys for job import, export download and preparation.

## Resume commands

```sh
cd /Users/padige/Projects/jobforge-c3-design-review/jobforge-overnight
git status && git log --oneline -5
(cd apps/web && npx vitest run)                  # frontend, ~380 tests
../jobforge-overnight-evidence/sco.sh ps         # isolated demo (ports 55883/56883/8383)
bash ../jobforge-overnight-evidence/run_full.sh  # full backend on throwaway services (55721/56721/9721)
```

Never point tests at the development (5432) or live demo (55433) databases.
