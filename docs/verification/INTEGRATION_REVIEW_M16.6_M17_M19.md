# Integration review — `overnight/m16.6-m17` → `dev` / GitHub `main`

Date: 2026-11-10. Reviewer: Claude (sole development assistant). Everything
below was executed in this session unless marked otherwise. Nothing has been
pushed or merged.

## Recommendation

**GO for integration into `dev`, then `main`, as a fast-forward — subject to
the owner's approval and a green hosted CI run.** **NO-GO for loading real
personal data** until M20 (private runtime) and M21 (first verified backup
and restore drill) exist; integration does not deploy anything.

## Branch relationship

| Ref | Commit | Relation to the branch |
|---|---|---|
| local `dev` | `6945454` | ancestor of the branch tip |
| GitHub `main` (read with `git ls-remote`, not fetched) | `6945454` | same as `dev` |
| local `main` | `cbe0d96` | stale pointer, 68 commits behind GitHub `main`; not used |
| branch tip | see `git log -1` | `dev` + the commits below |

`dev` → branch is a **pure fast-forward**: no rebase, no force, no conflict
resolution needed. The branch contains one merge commit (`98368ac`, Codex's
M16.6 `feature/p0-resume-export` at `69fca08`), which a fast-forward keeps.

Contents (oldest → newest): Codex M16.6 résumé export and H3 fixes; merge;
M17 UI (job import, export panel, question preparation, master résumé,
owner-pasted jobs, first-draft panel, `resume_ready` fixture); M19 tracking
(migration 0011, tracking API/UI, follow-ups, outcome filters, manual-send
tracking); M20–M22 design docs and this review. Diff vs `dev` (before this file): 103 files, +9,580 / −236, 28 commits.

## Migration compatibility

- Only one new migration: `0011_application_tracking` (head moves
  `0010` → `0011`; chain linear, verified by `verify_schema.py` 12/12).
- Additive: two nullable `DATE` columns, one CHECK, one partial index.
- The CHECK (`outcome = 'pending' OR applied_on IS NOT NULL`) validates on
  existing data because no code on `dev` ever writes `applications.outcome`
  (checked with `git grep` on `dev`: no assignments, no `SET outcome`).
  Owner may confirm on any existing database before migrating:
  `SELECT count(*) FROM applications WHERE outcome <> 'pending';` → 0.
- Old application code runs against the migrated schema (new columns are
  nullable and unknown to it), so an app rollback needs no DB downgrade.
- The down migration drops the two columns: **tracking data would be lost**.
  Never run it on a database with real tracking data without a backup and
  separate approval.
- Grants: the new columns are covered by app_api's table-level UPDATE and
  deliberately **not** added to app_agent's column-level grant.
- Existing stacks need migrating after they pick up the code: the dev stack
  (`alembic upgrade head`) and the live demo (`make demo-migrate`). Not done
  here; the live demo and dev databases were not touched.

## Security review

No changes to authentication, sessions, CSRF, role grants (0003/0005),
Compose files, Dockerfiles or CI workflows. Every route is classified in the
authorization matrix test, which fails on any unclassified route.

| New/changed route | Access | Notes |
|---|---|---|
| `POST /jobs/manual` | owner, CSRF | owner-pasted postings, no fetch; exemption only for the internal reserved source (approved 2026-11-10) |
| `GET /profiles/{id}/master-resumes/latest` | owner | shared-workspace owner boundary |
| `GET /settings/runtime-modes` | owner, reviewer | reveals only modes (replay/dry-run/fake) |
| `GET/PATCH /applications/{id}/tracking` | read both, PATCH owner + CSRF | `extra="forbid"`; cannot touch status, `submitted_at` or approval columns; validation before mutation |
| `GET /follow-ups` | owner, reviewer | read-only |
| `GET /applications` (+`outcome`, `applied` filters) | owner, reviewer | enum-validated query params |
| Codex M16.6: export preview, manual packet, packet download, fact source repair | owner (packet download tightened from shared to owner) | reviewed in `M16_6_FINDINGS.md`; H1–H3 fixed and regression-tested |

Considerations (not blockers):

- Reviewers can read tracking **notes**. This matches the existing shared
  review access; the owner should not put anything there they would not show
  a reviewer.
- `PATCH /tracking` has no dedicated rate limit (owner-only, CSRF, small).
- Production config currently requires an Anthropic key even in replay
  mode; fixed in M20 step 1 before any private deployment.

## Test coverage (executed 2026-11-10 on isolated throwaway services)

| Suite | Result |
|---|---|
| Full backend (`pytest`, includes unit, integration, security, matrix) | **3,587 passed, 0 skipped** |
| Safety-marked | 1,659 passed |
| API E2E | 6 passed |
| `dev`'s original readiness tests (byte-identical file) | 22/22 |
| Codex H3 regressions | 100/100 (and fail at import on `b9031f6`, as designed) |
| Offline evaluation | 0 provider calls |
| Schema verification | 12/12 |
| Frontend (vitest) | 410 passed (35 files); typecheck, ESLint clean |
| Playwright (throwaway `jobforge-ovn` demo at 0011) | **32/32** |

Not covered in the browser: a successful contact-bearing download as the
seeded profile owner (covered by API/integration tests); the application
worker inside the demo (no worker there; covered by an integration test).
Hosted GitHub CI has **not** run on this branch (nothing pushed).

## Proposed integration procedure (needs owner approval at each ⚠)

1. ⚠ Push the branch to GitHub as `overnight/m16.6-m17` (not `dev`/`main`)
   and let hosted CI run; require all jobs green.
2. ⚠ Fast-forward `dev` to the branch tip (`git merge --ff-only`) and push.
3. ⚠ Fast-forward `main` to `dev` after CI is green on `dev`.
4. Migrate existing local stacks (dev, live demo) after a backup of the
   demo database; re-run the demo smoke checks.
5. Keep the branch until `main` is verified; branch/worktree cleanup is a
   separate approval.
