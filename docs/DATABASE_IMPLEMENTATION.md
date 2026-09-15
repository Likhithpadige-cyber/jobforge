# DATABASE_IMPLEMENTATION.md

**Project:** Jobforge — AI Job Application Platform
**Phase:** 2 — database layer
**Status:** Implemented; schema verified statically, behaviour verified only where PostgreSQL was available
**Source of truth:** [DATABASE_DESIGN.md](./DATABASE_DESIGN.md)

---

## 1. What was implemented

| Area | Result |
|---|---|
| Tables | 37 of 37 from DATABASE_DESIGN.md §4–§7 |
| Enum types | 17 of 17, values and ordering identical |
| Columns | 467, matched model ↔ migration ↔ design |
| CHECK constraints | 34 |
| Unique constraints / indexes | 12 unique (5 partial), 26 supporting indexes |
| Foreign keys | 48 inline + 2 deferred (the `applications ↔ resumes` cycle) |
| Triggers | 30 (19 generated `updated_at` + 11 explicit) |
| Functions | 9 plpgsql, 3 of them `SECURITY DEFINER` |
| Database roles | 4 (`app_api`, `app_agent`, `app_monitor`, `app_migrate`) |
| Migrations | 2 new revisions on top of the Phase 1 baseline |
| Repositories | 8 |
| API endpoints | 27 (profile CRUD ×5 collections, fact lifecycle, evidence) |

### Layout

```
packages/core/models/         enums · identity · facts · jobs · applications · operations
packages/core/repositories/   base · profile · facts
packages/core/services/       profile_service
apps/api/routers/             profile · facts
apps/api/schemas/             profile · facts
migrations/versions/          0002_core_schema · 0003_roles_and_grants
migrations/sql/               0002 up/down · 0003 up/down
scripts/                      verify_schema · check_model_shadowing · gen_down_migration
tests/unit/                   test_schema_conformance · test_enums
tests/integration/            test_constraints · test_security_invariants ·
                              test_profile_crud · test_migrations
```

---

## 2. Deviations from DATABASE_DESIGN.md

Five. Four are implementation mechanics; one is a security gap found during implementation and closed. **No constraint was weakened.**

### D-1 — DDL is versioned SQL, executed by Alembic (mechanism)

The design specifies the schema in SQL. The migration therefore *is* SQL: `migrations/sql/0002_core_schema.up.sql`, executed by a thin Alembic revision.

Rationale: generated columns, partial and expression indexes, HNSW index options and plpgsql triggers are all clearer and more reviewable as SQL than as `op.create_table()` argument trees, and the file can be diffed line-by-line against the design document. The down migration is *generated* from the up migration by `scripts/gen_down_migration.py`, so the pair cannot drift; `--check` mode fails CI if it does.

The SQLAlchemy models mirror this schema and `scripts/verify_schema.py` fails the build if the two disagree on any table, column or enum value.

**Trade-off, stated honestly:** `alembic revision --autogenerate` is no longer the primary authoring path for this revision. The conformance checker is what replaces the safety autogenerate would have given.

### D-2 — `uq_facts_source` hashes the `text` column (mechanism)

Design: `UNIQUE(source_table, source_id, text)`.
Implemented: `UNIQUE(source_table, source_id, md5(text)) WHERE deleted_at IS NULL`.

A btree index entry is capped near 2704 bytes and fact text is free-form, so the literal form would fail at insert time on a long fact. The uniqueness semantics are unchanged.

### D-3 — `citext` extension added (addition)

`users.email` is `CITEXT` in the design, but the Phase 1 baseline enabled only `pgcrypto`, `pg_trgm`, `btree_gin` and `vector`. Migration 0002 adds `citext`.

### D-4 — The human-approval CHECK was insufficient on its own (**gap found and closed**)

This is the one worth reading carefully.

`ck_applications_approved_requires_human` requires `approved_by IS NOT NULL` when an application enters an approved state. DATABASE_DESIGN.md §8 also grants `app_agent` `INSERT, UPDATE ON applications`.

Those two together leave a hole: **an agent holding table-level UPDATE could satisfy the CHECK by writing any user id into `approved_by`.** The constraint tests that a value is present; it cannot test that a human decided anything. The architecture's central claim — "a bug in application code cannot produce a submitted application without a recorded human approver" — was not actually true as specified.

Closed with three layers, none of which relaxes the original:

1. **Column-level grants.** `app_agent` gets `UPDATE` on a named column list that excludes `approved_by` and `approved_at`. It can drive the pipeline; it cannot touch the approval columns.
2. **`trg_applications_require_real_approval`.** Entering `approved`, `submission_ready`, `submitting`, `submitted` or `manual_handoff` requires a row in `approvals` with `status='approved'` and a non-null `decided_by` — and `applications.approved_by` must match that approver. `app_agent` holds no write grant on `approvals` at all.
3. The original CHECK, unchanged.

Tested by `test_application_cannot_be_submitted_without_approval_row`, `test_approved_by_must_match_the_actual_approver` and `test_agent_cannot_write_approval_columns_on_applications`.

### D-5 — Additional constraints and triggers beyond the design (**strengthening**)

All are additive. Each closes a route to an outcome the architecture forbids.

| Object | Guarantee |
|---|---|
| `trg_resume_claims_facts_verified` | Every id in `resume_claims.fact_ids` exists, is undeleted and is **verified**. A UUID array cannot carry a foreign key, so without this the design's citation requirement was checkable only in application code. Same for `cover_letter_claims`. |
| `trg_profile_facts_protect_verified` | The substance of a verified fact (`text`, `structured`, `fact_type`, provenance) cannot be edited in place by **any** role. Changing it requires returning to `draft` and re-verifying. |
| `ck_profile_facts_verified_requires_actor` | `status='verified'` requires `verified_by` and `verified_at`. "Verified" always names a person and a moment. |
| `ck_certifications_in_progress_has_no_issue_date` | A certification you are studying for can never be rendered as one you hold. |
| `ck_site_policies_automation_requires_review` | `automation_allowed=true` requires a recorded human ToS review. |
| `trg_*_append_only` on `application_status_history`, `audit_logs`, `job_evaluations` | UPDATE and DELETE rejected. |
| `trg_events_publish_only` | `events` rows are immutable except for `published` / `published_at`. |
| Audit triggers on `profiles`, `profile_facts`, `site_policies`, `applications`, `approvals`, `settings` | Changes are captured even when they bypass the application layer. |
| `ck_jobs_not_own_duplicate`, `ck_jobs_duplicate_has_canonical`, salary/date ordering CHECKs | Ordinary data integrity. |

---

## 3. Roles and grants

Implemented in `migrations/sql/0003_roles_grants.up.sql`, in its own revision because roles are cluster-level and need elevated rights.

| Role | Reads | Writes | Cannot |
|---|---|---|---|
| `app_api` | everything | everything except history tables | UPDATE/DELETE `events`, `audit_logs`, `application_status_history`, `job_evaluations` |
| `app_agent` | everything | jobs, evaluations, resumes, claims, cover letters, questions, answers, submissions, runs, steps, llm_calls, processed_events; INSERT on events and status history; **column-limited** UPDATE on applications | **write anything in `profile_facts`, `profiles`, the five profile sub-tables, `fact_evidence`, `site_policies`, `settings`, `users`, `approvals`, `artifacts`** |
| `app_monitor` | everything | `metrics_snapshots`, `alerts`, `notifications`, `processed_events`, INSERT events | mutate any application, approval, fact, job or resume |
| `app_migrate` | — | DDL only | — |

Least-privilege details that matter:

- `REVOKE CREATE ON SCHEMA public FROM PUBLIC` — an agent cannot define a view, table or function to read or write around a grant.
- All roles are `NOINHERIT`, and **no grant anywhere uses `WITH GRANT OPTION`**, so no role can widen access for itself or another. Asserted by `test_no_grant_option_anywhere`.
- `ALTER DEFAULT PRIVILEGES` means a table added by a future migration arrives with the correct grants rather than none.
- `jobforge_increment_fact_usage(UUID)` is the single `SECURITY DEFINER` escape hatch, exactly as DATABASE_DESIGN.md §4.4 specifies: agents can record that a fact was used without holding UPDATE on the table.
- `DELETE` on `events`, `audit_logs` and `application_status_history` is revoked from `PUBLIC` and from every application role.

**Passwords are never set in a migration.** `make db-roles` applies them from the environment.

---

## 4. Profile and fact system

The profile is the human-authored ground truth; the fact index is what generation is allowed to draw on.

**A fact can never be created verified.** `POST /api/v1/profile/facts` has no `status` field — everything lands as `draft`. Verification is a separate endpoint that records a user id and a timestamp. This keeps verification a decision rather than a formality.

**Only `status='verified'` facts are citable.** `GET /profile/facts/citable` is the set generation may use, and the claim trigger independently re-checks it at write time.

**`POST /profile/facts/suggest`** splits a human-authored description into candidate facts. It is deterministic regex sentence-splitting — no model is involved, every suggestion is a substring of what the human wrote, and every one lands as a draft requiring verification. It is a typing shortcut, not a source of new assertions.

**Editing a verified fact is blocked** at the database level. The route is: unverify → edit → verify again. That is deliberate friction: anything that already cited the old wording is now citing a fact that is no longer citable, and that should be visible.

Demo data only. No real personal information is committed anywhere in this repository; fixtures use `example.invalid` addresses and a fictional "Example Cloud Corp".

---

## 5. Verification performed

See the Phase 2 summary for the full pass/fail breakdown. In short:

- **Executed offline and passing:** schema conformance (37 tables, 17 enums, 467 columns matched across models, migration and design), SQLAlchemy name-shadowing audit, down-migration freshness, grant-content assertions, migration-chain linearity, Python syntax and internal-import resolution across 68 modules.
- **Written but not executed:** everything requiring PostgreSQL, SQLAlchemy or Alembic — 61 integration tests including all 16 security-invariant tests. The sandbox this was built in has no database, no Docker and no network.

The security invariants are therefore **asserted in DDL and verified statically**, but their runtime behaviour is unproven until `make test-security` runs against a real PostgreSQL. Do that before trusting them.

---

## 6. Known limitations

1. **No authentication yet.** `FactVerifyRequest.verified_by` is supplied in the request body because there is no session. Phase 6 replaces it with the authenticated user. Until then the API must stay bound to localhost.
2. **The API connects as the database owner in development.** The role separation exists in the schema, but wiring `app_api` / `app_agent` into the runtime connection strings happens when workers arrive in Phase 3. Until then the grants protect against SQL executed as those roles, not against the API process itself.
3. **`embedding` is nullable and unpopulated.** The HNSW index exists; the embedding pipeline is Phase 4.
4. **No partitioning.** At the stated volume (≤5,000 jobs/day) `events` and `job_raw_snapshots` stay comfortably small for well over a year.
5. **Audit triggers write `current_user`, not the application user.** Correct attribution needs a session variable set per request; that lands with authentication in Phase 6.
