# Milestone changelog

## M3 — complete (verified 2026-09-20; history below)

### Diagnosis

The resume-rework scenario directly overrides get_redis with
 tests.unit.test_auth_sessions.FakeRedis, bypassing integration/conftest.py's
api_sessions fixture and its FakeLimiterRedis. The original shared fake had
only set/get/delete, with no pipeline method.

Recovered suppressed traceback:

```text
packages/core/cache/rate_limit.py:124, in consume
    async with redis.pipeline(transaction=True) as pipe:
AttributeError: 'FakeRedis' object has no attribute 'pipeline'
```

consume translates this to DependencyUnavailableError at line 133, using
`from None`; the original exception was recovered through __context__ without
changing production exception handling. The installed redis-py supports the
pipeline and PEXPIRE NX signatures. A temporary diagnostic key on local Redis
7.4.11 successfully returned count=1, allowed=True, retry_after=60 and was deleted.
Compose specifies redis:7-alpine. This AttributeError is a test-double defect.

### Files changed

- tests/unit/test_auth_sessions.py: shared fake now models counters, a controlled
  clock, expiration, PEXPIRE NX, PTTL sentinel values, and non-interleaved pipeline
  execution. Session/counter inspection interfaces remain available.
- tests/unit/test_rate_limit.py: removes the duplicate implementation in favor of
  an alias to the shared fake; corrects the concurrency-test description.
- docs/MILESTONE_CHANGELOG.md: this record.

### Decisions and security implications

Production code, rate-limit thresholds, authentication, authorization, CSRF,
fail-closed handling, UI, and three-cycle rules were not changed. Existing test
assertions were not weakened. No database/schema or API changes. No GitHub push.
The fake models the operations needed here, not the entire Redis command set.
No new tests added yet; broader validation remains pending.

### Exact execution results

- Initial sandboxed limiter run: 2 passed, 13 skipped (PostgreSQL inaccessible).
- Baseline limiter run with local-service access: 15 passed in 5.60s.
- First shared-fake limiter run: 4 failed, 11 passed in 5.57s; existing inspection
  interfaces were missing/incompatible, then restored.
- Latest required limiter suite: 15 passed in 6.83s.
- Latest required resume-rework suite: 1 failed, 19 passed in 10.63s.
- Broad make lint/typecheck/test-unit/test-integration/test-security targets not
  run: the required focused gate is not green.

### Blocking policy conflict

In test_three_human_cycles_survive_failures_and_third_revision_is_approvable,
three failed generation attempts plus three successful generations consume six
requests. The seventh request replays the completed third revision. Line 188
expects 200; the endpoint-level limiter returns 429 (count=7). Later assertions
also expect the three-cycle 409 response beyond the budget.

Resolving this requires either explicit test clock progression across a rate
window while preserving assertions and the production policy, or an explicitly
approved production policy distinguishing generation attempts from completed
replays/domain refusals. The latter changes rate-limit coverage and response
precedence. No choice was made implicitly. M3 is incomplete; M4-M10 not started.


### Approved M3 follow-up — 2026-09-19

The user approved advancing fake time before the seventh request. The test now
advances exactly by the revision counter's remaining TTL, after asserting that
six requests were counted. Existing replay/idempotency and three-cycle assertions
are unchanged. A before/after snapshot proves that cycle counts, attempt IDs,
statuses/counts, resume IDs/versions, and status-history count do not change merely
because the fake Redis clock advances. The production limit remains 6/user/hour.

Files additionally changed for the regression fix:
- tests/integration/test_resume_rework_api.py: approved clock progression and state assertion.
- tests/integration/test_auth_endpoints.py: supplies the existing request_with()
  helper in 15 direct login calls (16 parametrized cases). M3 added a required
  Request argument; the obsolete calls raised TypeError before any auth checks.
  No existing assertions were removed or weakened, and limiting remains active.

Verification:
- Required rate-limit suite: 15 passed in 6.37s.
- Required resume-rework suite: 20 passed in 10.49s.
- Authentication endpoint focused suite after signature correction: 31 passed in 4.31s.
- Initial make test-unit: 1339 passed, 1036 deselected in 4.47s.
- Latest make test-unit: 1339 passed, 1036 deselected in 5.17s.
- Initial make test-integration: 16 failed, 796 passed, 1563 deselected in 81.97s;
  all failures were the obsolete direct login calls described above.
- make test-security: 868 passed, 1507 deselected in 41.31s.
- Initial make lint: 97 errors. Mechanical cleanup reduced this to 13 errors.
- Latest make typecheck: 22 errors in 13 files (110 source files checked).

Mechanical cleanup already applied: import ordering/removal, unused noqa removal,
line wrapping, equivalent any()/dict-literal/contextlib.suppress expressions,
Python 3.12 type-alias syntax, and isinstance union syntax. No lint rules were
relaxed. Existing security assertions remain intact. No schema or API changes.
The frozen UI and existing uncommitted UI work were not modified.

Automatic approval review rejected a further proposed batch of production/test
lint and typing edits, citing its broad scope and potential behavior changes,
particularly a deduplication-interface rewrite. That rejected batch did not run.
Explicit approval for the broader cleanup is pending. M3 is not complete, and
M4 has not started. No GitHub push.

- Latest make test-integration after signature correction: 812 passed, 1563 deselected in 86.03s.

Mechanical cleanup file inventory (in addition to the M3 fixes above):
- apps/api/routers/health.py
- apps/api/schemas/profile.py
- migrations/env.py
- packages/agents/resume_optimizer/context.py
- packages/agents/resume_optimizer/evidence.py
- packages/core/cache/health.py
- packages/core/cache/rate_limit.py
- packages/core/db/health.py
- packages/core/models/identity.py
- packages/core/models/jobs.py
- packages/core/repositories/__init__.py
- packages/core/repositories/facts.py
- packages/core/repositories/profile.py
- packages/services/ingestion/__init__.py
- packages/services/ingestion/connectors/manual.py
- packages/services/ingestion/normalize.py
- packages/services/ingestion/pipeline.py
- packages/shared/logging.py
- packages/shared/types.py
- scripts/diagnose_roles.py
- scripts/verify_scaffold.py
- tests/integration/conftest.py
- tests/integration/test_authorization_matrix.py
- tests/integration/test_evaluation_persistence.py
- tests/integration/test_evidence_integration.py
- tests/integration/test_idempotent_claim.py
- tests/integration/test_ingestion.py
- tests/integration/test_rate_limits.py
- tests/integration/test_rationale_integration.py
- tests/integration/test_security_invariants.py
- tests/integration/test_tailoring_context.py
- tests/unit/test_config.py
- tests/unit/test_event_envelope.py
- tests/unit/test_policy_gate.py
- tests/unit/test_prefilter_and_scoring.py
- tests/unit/test_rate_limit.py
- tests/unit/test_tailoring_context.py
- tests/unit/test_timezone_awareness.py


### Final M3 gate — COMPLETE, 2026-09-20

All requested gates actually executed and passed after the scoped cleanup:

| Check | Exact result |
| --- | --- |
| make lint | All checks passed; zero findings |
| make typecheck | Success; no issues in 110 source files |
| pytest tests/unit/test_rate_limit.py -v | 17 passed in 0.34s |
| pytest tests/integration/test_rate_limits.py -v | 15 passed in 5.80s |
| pytest tests/integration/test_resume_rework_api.py -v | 20 passed in 12.54s |
| make test-unit | 1339 passed, 1036 deselected in 3.96s |
| make test-integration | 812 passed, 1563 deselected in 146.44s |
| make test-security | 868 passed, 1507 deselected in 36.73s |

No failures or skips in the final runs. Deselections are expected marker filters.
The 13 lint / 22 type findings and all chosen corrections are recorded in
M3_CHECK_CLASSIFICATION.md. No broad automatic transformation was used in this
scoped cleanup. No new suppression comments were added. Redis protocols document
existing awaitable operations; runtime limiter/session handling is unchanged.
Narrow casts document verified SQLAlchemy, raw-SQL, JSON serialization, or dynamic
logging boundaries. The attempted read-only dedupe protocol was reverted; the
original protocol remains. One constraint assertion was strengthened to require
its actual IntegrityError and CHECK name. No new test cases added.

Additional files touched by this final cleanup, beyond the earlier inventory:
- packages/core/auth/sessions.py
- packages/core/repositories/base.py
- packages/core/events/outbox.py
- packages/agents/llm/runtime.py
- packages/agents/resume_optimizer/persistence.py
- packages/agents/resume_optimizer/prefilter.py
- packages/agents/resume_optimizer/requirements.py
- tests/integration/test_profile_crud.py
- docs/M3_CHECK_CLASSIFICATION.md

No database/schema, API behavior, UI, limits, or approval-policy changes.
Authentication, authorization, CSRF, fail-closed Redis behavior, and the three-cycle
rework protection remain tested. The shared fake covers the Redis operations used
here, not the complete Redis API. Two cleanup proposals were rejected by automatic
review due to logging-runtime concerns; the safe quoted type-only annotation was
used instead. Those review concerns are resolved; no approval remains pending.
No GitHub push. M4 may now begin as a separate milestone.


## M4 — COMPLETE, verified 2026-09-21

Files added:
- packages/core/events/runtime.py: relay, transactional consumer, durable retries,
  interruption recovery, DLQ references, invalid-reference quarantine, reconciliation.
- packages/core/events/monitoring.py: idempotent scheduler ticks and monitoring snapshots.
- workers/runtime.py, workers/outbox_relay.py, workers/monitor.py, workers/scheduler.py:
  explicit least-privileged process entrypoints and bounded graceful shutdown.
- tests/integration/test_worker_runtime.py: 22 real-store integration/security cases.
- tests/unit/test_worker_lifecycle.py: 8 lifecycle/routing/bounds cases.
- docs/M4_RUNTIME.md: runtime contracts, operations and limitations.

Files updated:
- packages/core/events/envelope.py: register worker.delivery.started.
- docs/EVENTS.md: document the attempt event and align retry table with five total attempts.
- docs/ARCHITECTURE.md: align the four retry delays with five total attempts.
- docker-compose.yml, infra/docker/Dockerfile.worker, .env.example: opt-in worker
  services and explicit app_agent/app_monitor DSNs. Real .env was not modified.
- docs/MILESTONE_CHANGELOG.md: this independently reviewable milestone record.

Architecture: Redis Streams transport only event IDs; PostgreSQL remains the
source of truth. An advisory transaction lock serializes each event/group.
Independent append-only starts make process interruptions consume the retry budget.
Savepoints protect domain writes plus completion claims; failure records commit
only after rollback of the domain work. ACK follows commit. Source replay after
Redis loss consults durable completion records. The first actual domain operation
is scheduled monitoring snapshots; expensive HTTP generation stays synchronous.

Database changes: none. Existing tables and grants are used. A relay polling test
creates an isolated schema inside a transaction and rolls back its creation; it
never publishes unrelated live outbox rows. API/UI changes: none. Provider spend
and external submissions: none. No persistent worker processes started. No push.

Verification so far:
- Initial M4 focused integration suite: 13 passed in 1.75s.
- Expanded lifecycle/integration suite: 27 passed in 2.05s.
- Added durable-start recovery plus event contract: 37 passed in 2.69s.
- Final focused M4 plus event-contract suite: 40 passed in 3.20s.
- make lint: zero findings.
- make typecheck: zero errors in 116 files.
- Compose config with core/workers profiles: valid (quiet validation; no secrets dumped).
- make test-unit: 1347 passed, 1058 deselected in 3.84s.

Security: explicit role checks forbid owner/superuser fallback; no new grants.
Unsupported versions and forged human-only actor labels never invoke handlers.
Failure payloads contain exception classes, not messages or source content.
All existing M1–M3 paths and frozen UI remain intact.

Limitations: one in-flight delivery per process, two DB connections required;
no automatic stream trimming or automatic reset of terminal claims. Existing
synchronous workflows are not asynchronously re-run. Future application handlers
must own their state checks/terminal status transitions and paid-call audit and
budget/resumption behavior. See M4_RUNTIME.md for full operational constraints.

Final M4 regression gates:
- make test-integration: 834 passed, 1571 deselected in 86.68s.
- make test-security: 898 passed, 1507 deselected in 41.69s.
All final gates passed without failures or skips. M5 inspection may begin.

## M5 — inspected; consequential scoring policy decision required

Inspection confirmed deterministic extraction, verified-fact evidence mapping,
rationale and evaluation persistence exist, but no production caller supplies
candidate-derived inputs to compute_score or CandidateConstraints. Ingestion
normalizes eligible jobs but does not currently trigger this evaluation workflow.
The profile repository documents a single-candidate system.

Concrete local reproduction (2026-09-21): compute_score([]) returns score 100.00,
recommendation moderate, and an explicit gap that no required requirements could
be extracted. decide_status with the existing 65/55 thresholds returns shortlisted.
Seniority, domain and location alignment all default to 1.0; no existing mapping
from verified profile data supplies them. CandidateConstraints also defaults to
remote accepted, hybrid rejected and senior ceiling without stored user choices
for those fields. Wiring these defaults automatically would invent candidate
preferences or overstate evidence. Existing documentation specifies the weights,
but does not resolve how unknown alignment should affect automated disposition.

No M5 production code, schema, scoring thresholds or tests changed. Proposed
policy for approval: keep insufficiently supported assessments awaiting human
review, without automatic shortlist/discard or paid fallback; implement explicit
verified-data mappings before using alignment inputs for automatic disposition.
This is a product/grounding decision under the roadmap stop rule, not a routine
implementation approval. M4 remains complete; M6–M10 have not begun.

### M5 approved policy; schema checkpoint — 2026-09-21

The user approved unknown/insufficient evidence as unresolved human review,
without positive or negative invented credit, automatic disposition or paid
fallback. Further inspection found required score and recommendation columns
cannot represent this outcome. No existing unresolved recommendation is present;
the evaluated lifecycle documentation explicitly assumes a fit score exists.
M5_UNRESOLVED_EVALUATION_PROPOSAL.md records a concrete minimal migration/API
proposal, including paired null constraints, preserved historical rows, and reuse
of evaluated as the hold state. No migration or implementation applied, per the
user's explicit schema stop rule. No new test results claimed at this checkpoint.

### M5 implementation — authorized schema applied; final gates in progress

The user explicitly approved the nullable, authoritative assessment contract.
Migration 0007 adds assessment_status with assessed historical default and a CHECK
requiring both score/recommendation present for assessed or both NULL for
insufficient_evidence. The live local upgrade succeeded. Isolated transactional
migration tests verify historical backfill, safe downgrade without unresolved
rows, and atomic refusal with unresolved rows. No historical data is deleted or
rewritten; the append-only trigger remains active. Downgrade is intentionally
unavailable once unresolved history exists, rather than fabricating old values.

Files added:
- migrations/versions/0007_evaluation_assessment.py
- migrations/sql/0007_evaluation_assessment.up.sql and .down.sql
- packages/agents/resume_optimizer/assessment.py and evaluation.py
- workers/optimizer.py
- tests/unit/test_evaluation_assessment.py
- tests/integration/test_evaluation_assessment.py and test_automatic_evaluation.py
- docs/M5_EVALUATION.md

Files updated:
- packages/core/models/jobs.py, apps/api/schemas/jobs.py: database/API contract.
- packages/agents/resume_optimizer/persistence.py: validated unresolved persistence;
  only authoritative assessed status reaches automatic thresholds.
- packages/agents/resume_optimizer/context.py: unresolved results cannot enter tailoring.
- packages/services/ingestion/pipeline.py: transactional job.normalized outbox event.
- workers/runtime.py, docker-compose.yml, .env.example: opt-in app_agent optimizer.
- scripts/verify_schema.py: include migration 0007 and inspect ADD COLUMN evolution.
- docs/DATABASE_DESIGN.md, docs/ARCHITECTURE.md, proposal and milestone records.

Architecture: description-only extraction, verified fact evidence, explicit
alignment inputs, existing deterministic weights/thresholds and rationale. No
provider path exists. Missing evidence is recorded as unknown without a score;
known partial support retains existing partial credit. Profile selection fails
closed on missing/ambiguous owner candidates. Job locking plus M4 claims prevent
both redelivery duplicates and distinct normalization-event duplicates. Existing
scored records retain their semantics. No UI edits or grants changes; no push.

Focused checks:
- Initial assessment/schema suite: 22 passed, 1 failed (test SELECT * cache after
  isolated downgrade; changed to explicit column query).
- Initial worker/persistence gate: 62 passed in 4.17s.
- Expanded gate: 68 passed, 1 failed (fixture tried editing verified substance;
  corrected fixture to the enforced draft/edit/reverify lifecycle).
- Final focused M5/legacy persistence gate: 69 passed in 5.49s.
- make lint: zero findings.
- make typecheck: zero errors in 119 source files.
- scripts/verify_schema.py: 12 passed, 0 failed; linear seven-revision chain.
- Compose core/workers configuration validation: passed without dumping secrets.
- make test-unit: 1363 passed, 1084 deselected in 3.92s.

Known limitations and conservative matching/hold behavior are documented in
M5_EVALUATION.md. No workers are persistently running and no paid calls were made.

### Final M5 gate — COMPLETE, 2026-09-21

All requested checks executed and passed:
- Focused M5 and existing persistence tests: 69 passed in 5.49s.
- make lint: zero findings.
- make typecheck: no issues in 119 source files.
- scripts/verify_schema.py: 12 passed, 0 failed.
- Migration upgrade applied successfully; isolated upgrade/backfill/downgrade and
  guarded refusal verified by focused tests.
- make test-unit: 1363 passed, 1084 deselected in 3.92s.
- make test-integration: 860 passed, 1587 deselected in 90.69s.
- make test-security: 936 passed, 1511 deselected in 40.24s.
- Compose configuration and git diff --check: passed.
No skips or failures in the final gates. M6 may begin separately.

## M6 — COMPLETE, verified 2026-09-21

Files added:
- tests/evals/grounding_cases.json: versioned, hand-authored synthetic corpus.
- tests/evals/test_grounding_corpus.py: production guard, prompt, rationale,
  assessment and audited verifier-replay evaluations; network forbidden.
- tests/evals/README.md: methodology, limitations and separate live-study boundary.
- scripts/run_offline_evals.py: in-process pytest runner and per-category/mechanism
  JSON metrics with corpus/prompt hashes; fails on missing/skipped/failed cases.

Files updated:
- Makefile: test-evals target.
- .gitignore: local evaluation reports only.
- .github/workflows/ci.yml: explicit offline gate and JSON artifact.
- docs/MILESTONE_CHANGELOG.md: this record.

No production runtime, schema, API, UI, grants or provider configuration changes.
31 corpus cases span 13 required grounding categories, including positive controls.
10 additional harness/contract checks bring the focused suite to 41 tests.
Semantic-verifier replies are explicitly scripted, not captured model judgments:
replay cases measure enforcement and audit, not live-model semantic accuracy.
Deterministic cases measure numeral/citation/status/PII/schema safeguards, prompt
boundary isolation, M5 unresolved evidence and rationale grounding. Live semantic
accuracy remains unmeasured; no live evaluation was run or implicitly enabled.

Verification:
- Initial corpus suite: 32 passed in 0.88s.
- Initial report command: 40 passed in 0.90s.
- Final focused report command: 41 passed in 1.03s; all 31 corpus cases passed;
  zero provider calls, no missing cases, no skipped tests.
- make lint: zero findings after removing unnecessary subprocess/XML boundaries
  from the new reporter and fixing its import order; no suppressions added.
- make typecheck: zero errors in 119 source files.
- make test-unit: 1404 passed, 1084 deselected in 4.10s.

The CI file was edited locally; no remote CI run or GitHub push occurred.

### Final M6 gate — COMPLETE, 2026-09-21

All final checks actually executed and passed:
- make test-evals: 41 passed in 1.03s; all 31 corpus cases across 13 categories
  passed, no missing/skipped cases, zero provider calls.
- make lint: zero findings.
- make typecheck: no issues in 119 source files.
- make test-unit: 1404 passed, 1084 deselected in 4.10s.
- make test-integration: 860 passed, 1628 deselected in 89.35s.
- make test-security: 977 passed, 1511 deselected in 38.64s.
- git diff --check: passed.
No failures or skips in the final gates. CI integration is configured but remote
CI was not run. No paid inference, UI changes, schema/API changes or GitHub push
in M6. Scripted replay measures verifier enforcement, not live-model accuracy.

The user's latest instruction explicitly stops work after M6. M7 has not been
implemented or tested; M7–M10 are paused pending a subsequent user instruction.

## M7 — first-draft tailoring, authorized implementation

The user approved candidate/evidence isolation while retaining shared M2 review
access. No private workspaces, assignments or schema migration were introduced.
Owner-only structured master imports append base versions without verifying facts;
owner-only first-draft requests bind explicit candidate/master/evaluation/job IDs.
The worker runs B1/B2/B3/B5/B6, persists unapproved v1 drafts and per-claim evidence,
rechecks input freshness, and suppresses M8 render requests for this path only.
Independent audit evidence survives rollback; replay and a single durable attempt
prevent automatic repeat paid inference. Existing review/rework limits are unchanged.

M7_FIRST_DRAFT.md describes the API, private-data import boundary, opt-in worker,
failure visibility and limitations. No real resume data, UI edits, schema changes,
paid provider calls, external submission or GitHub push were introduced by M7.

### Final M7 gate — COMPLETE, 2026-09-21

- Focused first-draft unit/integration plus existing resume-rework API: 52 passed
  in 22.19s (26 first-draft integration, 6 first-draft unit, 20 rework).
- make lint: zero findings.
- make typecheck: zero errors in 124 source files.
- make test-unit: 1419 passed, 1123 deselected in 5.04s.
- make test-integration: 899 passed, 1643 deselected in 105.93s.
- make test-security: 1018 passed, 1524 deselected in 47.97s; no security skips.
- make test-evals: 46 passed in 0.97s; 36 synthetic corpus cases across
  14 categories, no missing/skipped cases, zero provider calls.
- make verify-schema: 12 passed, zero failed; existing seven-revision chain intact.
- git diff --check: clean.
- Combined core/workers Compose configuration validates without launching workers.

During verification, fixed async session refresh before snapshot revalidation and
used the canonical project clock for fact validity. Adversarial fixture setup was
corrected to respect immutable events/evaluations and fact unverification rules;
no database protection or existing assertion was removed.

All final gates are green. Existing M1 authentication, M2 shared reviewer access,
M3 limiter controls, M4 delivery, M5 unresolved evaluations and M6 grounding guards
remain covered. M7 is complete. Stop here per the user's instruction: M8 has not
started. No frozen UI changes or GitHub push were made for M7.


## M8 — COMPLETE, verified 2026-09-22

Rendering and artifacts: `approved structured resume -> deterministic
rendering -> versioned artifact -> authenticated retrieval`. Built across
several passes on this machine (Phases 1-2 below are preserved history from
before pytest/boto3 were confirmed runnable in this environment); this entry
is the final, complete record superseding the two earlier "NOT COMPLETE"
entries it replaces.

### What M8 delivers

**Rendering.** `DocxResumeRenderer` (python-docx) and `PdfResumeRenderer`
(ReportLab) are byte-deterministic - see "History" below for how that was
measured and the two determinism defects fixed along the way. Rendering
reads only `packages/services/rendering/document.py`'s pure, dependency-free
`ResumeDocument` (no profile, no job, no evaluation, no contact block), so it
structurally cannot rewrite or infer a candidate's claims; a word-for-word
comparison against persisted content plus a separate numeral assertion is
the executable half of that guarantee (`tests/unit/test_render_no_fabrication.py`,
`test_rendering_never_invents_text`).

**Approval binding.** `render_resume` (`packages/core/services/artifacts.py`)
authorises against the exact approved resume id *and* version via
`snapshot_matches` - a regenerated version cannot inherit its predecessor's
approval. Ordering is `revalidate -> render -> upload -> read back and
compare checksums -> revalidate again -> insert the row -> emit`: the second
revalidation runs immediately before the artifact row is written, so a
reviewer requesting changes while rendering is in flight aborts the write
before anything durable claims the stale version was rendered.

**Storage.** Artifact bytes live behind the `ArtifactStore` protocol
(`put`/`get`/`exists`/`verify` only - deliberately no `presigned_url`, no
`public_url`, nothing that mints a link). `S3ArtifactStore` is a real boto3
adapter: `put_object` is called with `ContentType` and never with an `ACL`
argument, so the bucket's privacy is never overridden from application code;
`verify` re-downloads and re-hashes SHA-256 rather than trusting ETag, which
is not a content hash at all for a multipart upload. `artifact_store()` has
exactly two branches (`s3`, `filesystem`) and no fallback between them - an
`s3` deployment that cannot reach its store fails closed through
`ArtifactStoreUnavailableError` rather than writing to local disk.
Storage keys (`resumes/<uuid>/<template>/resume.<ext>`) are opaque UUIDs and
fixed words only; no candidate name, email or other PII ever reaches one.

**Real MinIO verification.** `tests/integration/test_s3_artifact_store.py`
runs the real adapter against a real, version-pinned MinIO
(`quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z`, docker-compose `full`
profile) - `put -> exists/head -> get` with exact-byte and exact-content-type
round-trip, a missing-object path, and an unreachable-endpoint transport
failure. This is MinIO verification, not AWS S3 verification; see
Limitations.

**Idempotency and recovery.** A pure replay of `render_resume` writes no
second artifact row and emits no second `resume.rendered`
(`test_replaying_the_render_writes_nothing_new`). A mixed invocation - one
artifact already existing, one genuinely new - emits exactly one event
naming every artifact the resume now has, not one per newly created row
(`test_a_mixed_invocation_emits_one_event_covering_every_artifact`). Upload
happens before the database row, so a crash between them leaves a harmless
orphan object rather than a row that lies about what exists; recovery reuses
the same deterministic storage key and a further replay after recovery is
still idempotent (`test_the_orphaned_object_is_recoverable_and_replay_stays_idempotent`).

**Event contract.** `resume.rendered` v2 (`packages/core/events/rendered.py`,
`ResumeRenderedV2`, `extra="forbid"`) is validated at emission and re-parsed
from the persisted row in `test_rendered_event_exact_public_v2_contract`. The
delivery runtime's `SUPPORTED_EVENT_VERSIONS` map is per event type:
`resume.rendered` accepts `{1, 2}` so historical v1 producers keep working
unmodified; every other event type accepts only `{1}`, enforced at the same
dispatch point before any handler runs
(`test_unsupported_version_never_calls_handler`, using an unrelated event
type to prove the rejection is general, not `resume.rendered`-specific).
Both v1 and v2 were proven to traverse the real Redis stream and the real
`DeliveryRuntime`, not just construct correctly
(`test_rendered_versions_through_stream_dispatch_are_idempotent`).

**Authorization and non-enumeration.** Artifact listing and download sit in
`OWNER_AND_REVIEWER` in the route-enumerated authorization matrix - a
reviewer who may read a resume may see and download what was rendered from
it. An anonymous caller gets 401 before any identifier is looked up; a
missing artifact and a missing resume answer with an identical 404 body,
including when the underlying store itself is unreachable for a resource
that was never accessible in the first place (existence is never leaked
through which error a caller receives). `/sources` remains owner-only,
unchanged.

**Schema.** Migration `0008_artifact_binding` adds `resume_version`,
`approval_id` and `template_version` to `artifacts`, nullable at the table
level (the table also holds evidence/handoff artifacts with no resume
lineage) and required by a `CHECK` for the two rendered-resume kinds. Grants
`app_agent` `INSERT` on `artifacts` - the render worker's only new
capability - on top of the `SELECT` it already had; `app_agent` remains
SELECT + INSERT, no UPDATE, no DELETE, confirmed against the real deployed
table. The down-migration refuses when rendered artifacts exist
(`Cannot downgrade: rendered resume artifacts...`) rather than silently
dropping the binding a human's approval depends on.

**Dependency/security posture.** `boto3` added as a runtime dependency for
`S3ArtifactStore`. A subsequent `pip-audit` pass found 20 known
vulnerabilities across three pre-existing, non-M8 packages (`python-multipart`,
`cryptography`, and transitively `starlette` via `fastapi`'s own version
ceiling); all three were upgraded to the minimum version covering every
outstanding advisory (verified against real package metadata, not inferred
from version numbers), bringing `make audit` to 0 known vulnerabilities with
zero application-code changes. Separately, `make secrets-scan` was found to
be running `detect-secrets`' baseline-*maintenance* command (always rewrites
the baseline, always exits 0 - never an actual gate, in CI either) instead of
its read-only `detect-secrets-hook` counterpart; both the Makefile target and
CI now run the read-only gate, and the stale baseline (last genuinely
generated against a ~14-file skeleton repo) was regenerated and manually
reviewed finding-by-finding, with zero genuine secrets found.

### Explicit limitations

- Real AWS S3 itself was not exercised - only MinIO, which is wire-compatible
  for the operations this adapter uses but is not the same claim.
- Real AWS credential-chain / IAM-role resolution is unverified; the adapter
  supports it (omitted static credentials fall through to boto3's own
  resolution chain) but nothing in CI or locally exercises an IAM role.
- Multipart S3 upload is neither implemented nor exercised - every artifact
  here is small enough for a single `put_object`, and no code path chunks an
  upload.
- M8 does not implement M9 application preparation.
- M8 does not implement M10 submission/handoff.

### Pre-existing hardening item, not an M8 blocker

Local API connections currently authenticate to PostgreSQL as the cluster
owner `jobforge`, confirmed empirically via `pg_stat_activity` against the
running `jobforge-api` container, rather than the least-privileged `app_api`
role the schema's grants (migration `0003_roles_grants`) and SECURITY.md
§3.2 describe as the second layer of defense. Unlike the render worker,
which has both a per-process `WORKER_DATABASE_URL` and a `verify_role()`
startup check that refuses to run under the wrong role, the API has neither;
`make db-verify-roles` checks only `app_agent`. Table ownership bypasses
grants entirely, so DB-level least privilege is not currently an effective
defense-in-depth boundary for API traffic in any environment, dev or
otherwise. This predates M8 - it is a property of the M1-M3 role model,
applies schema-wide rather than to `artifacts` specifically, and no API code
path was found (or added by M8) that updates or deletes an artifact row
regardless - but it should be revisited during release/security/AWS
hardening, where `DATABASE_URL` would need to be pointed at `app_api`
credentials and a startup check added to match the worker's.

### Final verification counts

- make lint: clean.
- make typecheck: 136 source files, no issues.
- make verify-schema: 12 passed, 0 failed; 8-revision chain, head
  0008_artifact_binding.
- make check-shadowing: clean.
- make test-unit: 1555 passed.
- make test-integration: 948 passed (includes the real-MinIO S3 suite,
  reached over the dockerized network path).
- make test-security: 1054 passed, 0 skipped, under
  REQUIRE_SECURITY_TESTS=1.
- make test-evals: 46 passed, 36 corpus cases, 0 provider calls - the M6
  anti-fabrication/grounding suite, unaffected by M8.
- make audit: 0 known vulnerabilities (post-remediation).
- make secrets-scan: passing, read-only, proven not to mutate the working
  tree across repeated runs.
- Real MinIO integration (`tests/integration/test_s3_artifact_store.py`):
  8 passed, against the pinned `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z`.
- 25/25 M8 invariants individually mapped to production code and executable
  tests with zero GAP and zero PARTIAL (put -> exists/head -> get exact-byte
  round trip; only an approved exact resume/version renders; stale
  approval/version rejected before persistence with a second revalidation
  immediately before the write; deterministic DOCX/PDF; no-PII storage keys;
  checksum verification; authenticated downloads; owner/reviewer
  authorization; non-enumeration; no filesystem fallback; no public
  ACL/URL/presigned path; replay and mixed-invocation idempotency;
  deterministic orphan recovery; v2 payload contract and delivery-runtime
  traversal; v1 compatibility; unrelated-event version rejection;
  `/sources` owner-only).

No remote push occurred for any commit in this milestone; this repository
has no configured remote.

### History (Phase 1 - infrastructure and the determinism harness)

Rendering infrastructure, approval binding, storage abstraction and the
determinism harness were built and schema-verified before pytest, SQLAlchemy,
FastAPI, structlog and boto3 were confirmed runnable in this environment. In
that state: make verify-schema (12 passed), the shadowing audit and the
down-migration freshness check were all clean; a 62-test M8 unit suite
(canonical document, storage keys/filenames, the determinism harness, the
filesystem store) found and fixed two defects - a path traversal in
`storage_key` when `template_version` was `..`, and a determinism harness
that had been asserting over renderers whose bodies raise rather than over
their actual output.

### History (Phase 2 - renderers implemented and measured)

`DocxResumeRenderer` and `PdfResumeRenderer` were implemented and executed
against the real libraries, byte-identical across four separate interpreter
processes 1.2s apart with `PYTHONHASHSEED=random`, for both formats. DOCX
needed exactly one fix - ZIP entry timestamps are the wall clock at save
time - handled by repacking the archive with a fixed timestamp without
parsing or rewriting any entry; `core.xml` dates and `w:rsid` values were
measured and found constant, so no OOXML surgery was performed. PDF needed
`Canvas(invariant=1)`, which pins `/CreationDate`, `/ModDate` and `/ID`. ATS
extraction was verified under both pypdf and pdfminer (no control
characters, no `(cid:)` markers); one real defect was found and fixed - U+2022
in base-14 Helvetica extracted as `\x7f`/`(cid:127)` under both extractors,
so bullets render as ASCII hyphens joined to their line instead.

## M9 — COMPLETE, verified 2026-09-22

Application preparation: `approved resume -> extracted questions ->
classified -> grounded/drafted or left an honest blank -> human review ->
independent answers approval`. Never submits anything - submission is M10's,
and M9 does not implement, emit, or route toward it.

### What M9 delivers

**Question extraction, classification and drafting.** `prepare_application`
(`packages/core/services/applications.py`) is one deterministic pipeline,
called by the new `workers/application.py` worker: extract, classify, ground,
draft, persist, transition `resume_ready -> questions_pending`, emit. No
model call anywhere in it. `classify_question` places every question into one
of six classes (`factual_safe`, `derived`, `uncertain`, `judgement`,
`sensitive`, `unanswerable`) from its text and input type alone; `draft_answer`
then attempts grounding in that order, and returns an honest blank the moment
grounding fails rather than a lower-confidence guess. Questions arrive as an
owner-supplied capture in the `prepare` request body, not a live ATS scrape -
see Limitations.

**Sensitive-question policy.** `packages/core/policies/sensitive_questions.py`
is the hard-coded, escalate-only pre-classifier from AGENT_DESIGN.md §4.2
(work authorization, visa/sponsorship, security clearance, criminal history,
disability, veteran status, race/ethnicity, gender, age, salary *history*).
It runs first and unconditionally inside `classify_question`; nothing
downstream of it can move a question back out of `sensitive` once it has
placed one there - proved by a case where a question matches both a sensitive
pattern and an ordinary factual one and still lands on `sensitive`
(`test_sensitive_cannot_be_shadowed_by_a_factual_pattern`, and the offline
corpus's `deterministic_sensitive_classifier_vs_deescalation` category). A
sensitive answer is always `source = 'blank'`, `value = NULL`,
`requires_human = true` - never agent-answered, never logged.

**Grounding and provenance.** Reuses the existing `AnswerSource`/`fact_ids`
columns rather than adding new ones. `source = 'agent'` cites one or more
real `profile_facts` rows only - verified, not soft-deleted, belonging to
this exact profile (`_load_citable_facts`) - now backed by a database
constraint, `ck_application_answers_agent_requires_facts` (migration 0009,
mirroring `ck_resume_claims_claim_has_facts`): an uncited `agent` answer is a
database error, not just a code-review finding. `source = 'derived'` covers
anything deterministic that is *not* a fact citation - a `profiles` column
(email, city, willing to relocate), a computed span (years of experience from
`experiences` date ranges, outer bound only, never double-counting
overlapping roles), or the employment-type preference below - and records
what grounded it in `value_structured["derived_from"]` instead of a
fabricated citation. Every cited fact is re-checked a second time immediately
before the answers-approval snapshot is built (`revalidate_cited_facts`), the
same immediately-before-persist discipline M8's `render_resume` uses for
resume approval.

**Employment-type candidate preference.** `profiles.accepted_employment_types`
(migration 0009, `TEXT[]`, nullable, no default - `NULL` means no preference
at all, so the filter is opt-in per candidate rather than hard-coded into
JobForge). Values share the exact vocabulary
`packages/services/ingestion/normalize.py`'s `extract_employment_type`
already produces, so no normalizer changes were needed. An explicit accepted
type auto-advances (`requires_human = false`); an explicit type outside the
accepted set is a confident "no" that still requires a human decision
(`judgement`); a missing or unmatched type is `uncertain` and requires a
human - never inferred as full-time merely because a posting looks like an
ordinary salaried role.

**State machine.** `resume_ready -> questions_pending -> awaiting_approval`
replaces the temporary bridge C2 used while question extraction did not
exist. The first edge is `prepare_application`'s and always fires, including
when extraction produced zero questions - the state records that extraction
happened, not how many results it found. The second edge remains
`resume_review.enter_review`, changed only in its precondition
(`questions_pending` instead of `resume_ready`); it still only ever creates
the `resume` approval, so nothing about the existing M7/M8 approval flow was
altered.

**Independent answers approval.** `ApprovalKind.ANSWERS` is a separate,
independently created-and-decided record
(`approve_answers`/`request_changes_answers`), not a replacement for the
`resume` approval and not gated by it - `jobforge_require_real_approval`
accepts any approved kind, so resume approval alone still moves an
application to `approved` exactly as M7/M8 left it. Unlike resume review, the
answers approval needs no separate "open" step: it is created and decided in
one call, because there is no regeneration pipeline waiting on a pending row
the way resume rework needs. `approve_answers` refuses while any
`requires_human` answer is still unresolved, and its `subject_snapshot`
records the full question/answer content (not identifiers only, since -
unlike a versioned resume row - an `application_answers` row can still be
edited after the snapshot is taken).

**API.** Five endpoints (`apps/api/routers/applications.py`):
`POST /applications/{id}/prepare` (owner only, 202, emits
`application.questions.requested` rather than doing the work inline - mirrors
`resume.first_draft.requested`/`resume.render.requested`'s shape exactly),
`GET /applications/{id}/questions`, `PATCH /applications/{id}/answers/{id}`
(always `source = 'human'`, `answered_by` from the session, never from the
request body), `POST .../package/approve`, `POST .../package/request-changes`
(all four owner + reviewer, matching M2's shared resume-review access).
Authentication-before-lookup and an identical `"application not found"` for
missing/inaccessible resources, the same non-enumeration discipline
`artifacts.py` documents.

**Worker and events.** `workers/application.py` (new `"application"` kind in
`workers/runtime.py`, `app_agent` role, `DeliveryRuntime`/`cg.application`),
deterministic and offline like `workers/render.py` - no model, no provider
budget. `application.questions.requested` is a new catalogue entry (the
request/result pair, mirroring the M7/M8 pattern); `application.questions.extracted`
and `application.answers.drafted` are emitted exactly as EVENTS.md already
specified before M9 existed. `application.ready` was **not** implemented -
see Limitations.

**Authorization.** `prepare` is owner-only; `questions`, `answers`,
`package/approve` and `package/request-changes` are owner + reviewer, added
to the route-enumerated authorization matrix (`test_every_route_is_classified`
proves nothing was missed). `/sources` remains owner-only, unchanged.

**Schema.** Migration `0009_application_preferences` adds exactly two things:
`profiles.accepted_employment_types` and
`ck_application_answers_agent_requires_facts`. No grant changes were made or
needed - `app_agent` already had `SELECT`/`INSERT`/`UPDATE` on
`application_questions`/`application_answers` from migration `0003`,
confirmed against the real deployed schema
(`test_deployed_grants_are_unchanged_by_this_migration`). Upgrade, downgrade
and existing-data compatibility were tested in an isolated rollback-only
schema; the primary development database was upgraded once, never
downgraded.

**Idempotency and recovery.** `prepare_application` is idempotent by
*status*, not by row count - a real bug found and fixed during
implementation: gating replay-detection on "do any rows exist" made the
legitimate zero-question case fail to short-circuit, since a zero-question
preparation leaves nothing to find. Fixed via an explicit set of statuses
only reachable once preparation has already committed. Everything runs
inside one atomic transaction the caller commits once - there is no separate
"inference" step with its own durability need the way a paid LLM call has
one, because nothing here calls one.

### Explicit limitations

- M9 does not scrape a live ATS form. Questions are an owner-supplied
  capture, carried in the `prepare` request body and the
  `application.questions.requested` event payload - one of AGENT_DESIGN.md
  §4.1's three named sourcing methods, not the only one.
- **Re-preparing after a resume rework, when the prior round produced
  non-zero questions, is refused rather than silently merged.**
  `application_questions` has no per-round version binding the way
  `resumes.version` does, so a second `resume_ready` episode with real prior
  rows raises `ApplicationStateError` instead of mixing two rounds' answers
  with no way to tell them apart
  (`test_reworked_resume_with_stale_prior_questions_is_refused_not_mixed`). A
  prior round with zero questions is unaffected, since there is nothing to
  conflict with. Versioning prepared questions per resume episode is future
  work.
- Free-text essay/`judgement` questions are never auto-drafted, even as a
  marked-draft suggestion (AGENT_DESIGN.md §4.2 allows this optionally) - kept
  conservative for this milestone.
- `workers/application.py` has no docker-compose service, matching
  `workers/render.py`'s own precedent from M8.
- M9 does not implement `application.ready`, does not choose a submission
  method, and does not submit or hand off an application. Submission belongs
  to M10.
- M9 does not implement cover-letter generation.
- M9 does not implement M10 submission/handoff.

### Final verification counts

- make lint: clean.
- make typecheck: 141 source files, no issues.
- make verify-schema: 12 passed, 0 failed; 9-revision chain, head
  0009_application_preferences.
- make check-shadowing: clean.
- make test-unit: 1622 passed.
- make test-integration: 996 passed (dockerized).
- make test-security: 1093 passed, 0 skipped, under
  REQUIRE_SECURITY_TESTS=1.
- make test-evals: 57 passed, 47 corpus cases (11 new M9 cases across all
  required categories, added via narrow additive dispatch branches that
  touch no existing case's logic), 0 provider calls.
- make secrets-scan: passing, read-only, proven not to mutate the working
  tree with the new files staged and unstaged.
- Authorization matrix: 267 passed, including all five new M9 routes.

No remote push occurred for this milestone; this repository has no
configured remote.

## M10 — COMPLETE, verified 2026-09-22

Submission readiness and human handoff: `grounded cover letter -> human
review/rework -> every current required approval satisfied -> approved ->
submission_ready -> policy evaluation -> submission_ready / manual_handoff /
policy_blocked -> deterministic handoff packet`. Never submits anything -
real submission, retry/backoff execution and Agent 3 monitoring are M11's,
and M10 does not implement, emit, or route toward any of them. Built in two
passes plus a stabilization fix, all landing before this milestone was
frozen: cover-letter generation and review/rework first, then submission
readiness and the handoff packet, then a genuine approval-order bug found
during that second pass's own acceptance check was fixed and re-verified
before commit.

### What M10 delivers

**Grounded cover-letter generation.** `packages/agents/cover_letter/
drafting.py` is B3's sibling, not a fork of resume tailoring: the same
`TailoringContext`/`load_tailoring_context` (B0), the same numeral-provenance
guard, and the same independent Claim Verifier (B5) resumes use, via
`verify_claim_for_handoff` - reused, not re-implemented, per the locked
design decision that cover-letter drafting needed its own prompt and service
but not a competing verifier. Every paragraph must cite a currently verified
fact (`fact_ids` has `min_length=1` on the schema itself - there is no
"connective" paragraph exempt from the citation requirement), and a numeral
absent from every cited fact's structured data is refused before it can ever
reach an approvable version. Only a cover letter whose every claim verifies
`supported` opens review; a version that fails is still persisted in full,
honestly, but is never put in front of a human.

**Cover-letter review and rework.** `cover_letters.py` mirrors
`resume_review.py`'s versioning shape with no schema change: `cover_letters`
already carried the identical `application_id`/`version` shape `resumes`
does, unused, since migration `0002`. Every generation - first draft or a
regeneration after a reviewer requests changes - is a new, append-only row;
nothing UPDATEs an existing version's content. `Approval.subject_snapshot`
binds a decision to `{cover_letter_id, cover_letter_version}` exactly, the
same two-field discipline `resume_review.snapshot_matches` already uses, so
a decision made about an older version can never authorize a newer one
(`test_stale_cover_letter_approval_cannot_satisfy_readiness`,
`test_stale_cover_letter_approval_cannot_complete_review`). The lifecycle -
generate, review, request changes, regenerate, review, approve - lives
entirely inside `awaiting_approval`; no `ApplicationStatus` edge was added
for it, matching `ApprovalKind.ANSWERS`'s precedent rather than resume
rework's `changes_requested` path.

**Event-driven, idempotent regeneration.** `cover_letter.generation.
requested` (the one new event this milestone adds; `cover_letter.generated`
was already reserved and is reused unchanged, now carrying
`cover_letter_version`) is requested automatically the moment human review
opens (`resume_review.enter_review`, no owner action needed the way M9's
question capture needs one) and again on every reviewer request-changes.
Runs on a dedicated `"cover-letter"` worker kind (`workers/cover_letter.py`,
mirroring `"first-draft"`'s audit-engine/pricing shape, not the deterministic
`"application"`/`"render"` workers') rather than the existing `"application"`
worker, because it is the one M10 pipeline that calls a model. Idempotent by
an existing-row check keyed to the triggering event id
(`_cover_letter_for_event`), the same pattern `generate_first_draft` already
established - a redelivered request produces no second version.

**Review completion - the approval-order bug and its fix.** A new shared
gate, `packages/core/services/review_completion.py`, owns
`awaiting_approval -> approved` exclusively via
`maybe_mark_application_approved`, called from all three approval-granting
functions (`resume_review.approve`, `cover_letters.approve_cover_letter`,
`applications.approve_answers`) after each records its own decision. This
replaces an earlier version of this milestone in which `resume_review.
approve` alone transitioned the application the instant the resume approval
was granted, regardless of the other two - discovered during this
milestone's own acceptance check, before it was frozen, and fixed before
commit. The condition is asked fresh every time: current RESUME approved,
current COVER_LETTER approved, and - only if any questions were ever
prepared - current ANSWERS approved (zero-question applications never
require one, preserving M9's own `_decide_answers` behavior exactly). Every
leg is snapshot/version-validated using each source module's own matcher, so
a stale approval of any kind can never complete review, and approval order
no longer matters by construction - `test_approval_order_does_not_matter`
and `test_zero_question_approval_order_does_not_matter` each exercise every
permutation of the three (and two) decisions and assert the application
reaches `approved` only after the last one, whichever kind that is.

**Submission readiness and the policy fan-out.** `packages/core/services/
submission_readiness.py` runs on the *existing* `"application"` worker
(`cg.application`, no new worker kind - readiness needs no model call, only
a snapshot comparison and an already-built policy lookup) reacting to
`approval.granted`. `approved -> submission_ready` fires unconditionally once
review completion's own invariant holds (defense in depth: `submission_
readiness` imports `review_completion`'s checking functions rather than
duplicating them); the *existing*, unmodified `evaluate_submission()` policy
gate then decides the fan-out - `ALLOWED` leaves the application at
`submission_ready` (M10 implements no dispatcher for any channel, so policy
permission is never treated as "a human should act now"), `REQUIRES_HUMAN`
reaches `manual_handoff`, `BLOCKED` reaches `policy_blocked`. `submission_
ready -> submitting` and everything past it is deliberately absent from
`ALLOWED_TRANSITIONS` - M11's, not guessed at here.

**Submission record.** One `submissions` row per application, created
exactly once (`attempt = 1`, guarded by both an application-level idempotency
check and the database's own `uq_submissions_application_id_attempt` index).
`result` stays `pending` for the whole of M10 - nothing is ever actually
sent, so `succeeded`/`failed` would be a lie - with no `http_status` or
`external_reference`. `method` and `policy_id` record what the policy gate
actually decided; `blocked_reason` is populated only on the `BLOCKED` path;
`packet_artifact_id` is populated once a handoff packet exists for the
current source material, and stays `NULL` rather than pointing at anything
incomplete when it does not yet.

**Handoff packet.** A deterministic ZIP (`packages/core/services/
handoff_packet.py`): the current approved resume PDF (the real M8 artifact,
reused rather than re-rendered), the current approved cover letter as plain
text (see Limitations), prepared questions and answers when any exist, job/
target metadata, and a status-specific checklist that is the one place a
human reads what M10 actually decided - `manual_handoff` states human action
is required, `policy_blocked` states automated submission is blocked and
will not be attempted, `submission_ready` states plainly that no automated
submission has been attempted or will be by this version of JobForge and
that the packet's existence is not permission to auto-submit. Bound to
current source material with no new column and no migration: the storage key
itself is content-addressed from the current resume id+version, cover-letter
id+version, and a digest of the current answers content, so two calls with
unchanged material reuse the same artifact (replay-safe, no duplicate
logical packet) and any source change - a new cover-letter version, an
edited answer - addresses a different key by construction, never a stale
one silently served. If the resume PDF is not rendered yet when readiness
runs (`resume.render.requested` fires the instant the resume is approved,
but the render worker consuming it is asynchronous), readiness completes
regardless and `packet_artifact_id` stays `NULL` rather than pointing at
incomplete bytes; the *existing* `resume.rendered` event, subscribed to on
the same `"application"` worker, is the deterministic recovery path
(`retry_packet_generation`) - no new event, and no reliance on a client
repeatedly polling `GET /packet` to repair state. That endpoint
(`GET /api/v1/applications/{id}/packet`) only ever serves what already
exists; owner + reviewer, authentication before lookup, the same
non-enumeration discipline `artifacts.py` documents, no presigned URLs, no
public ACL, no filesystem-store special-casing.

**Events.** `application.ready`, `application.status.changed` (every real
transition, without exception, via the unmodified `transition_application`),
and `policy.blocked` (only when that path is actually entered, payload
matching its existing documented shape) are the events M10 emits. None of
`submission.requested`/`started`/`succeeded`/`failed`/`handoff.required` are
emitted anywhere - M10 attempts no real submission, so none was semantically
required; `cover_letter.generation.requested` is the one genuinely new event
this milestone adds anywhere.

**Authorization and security.** `GET /applications/{id}/packet` and the
cover-letter review routes are owner + reviewer, added to the
route-enumerated authorization matrix. No agent role can write
`site_policies` - `app_agent`'s only interaction with it anywhere in M10 is
one read (`SitePolicyRepository.get_by_domain`), confirmed by inspection, not
just by grant absence. No employer/ATS network call, no email delivery, no
browser automation, and no CAPTCHA/stealth/proxy/bypass capability exists
anywhere in this milestone's code - there is nothing to disable, because
nothing was built.

**Schema.** No migration. Every table, enum, and grant M10 needed -
`submissions`, `site_policies`, `ApprovalKind.COVER_LETTER`,
`SubmissionMethod`, `SubmissionResult`, the `handoff_packet`/
`cover_letter_pdf` artifact kinds, and the relevant `app_agent` grants -
already existed, unused, since migrations `0002`/`0003`.

### Explicit limitations

- The cover letter is included in the handoff packet as deterministic plain
  text (`CoverLetter.body_text`), not a rendered PDF. M8's rendering pipeline
  (`packages/services/rendering/document.py`) is structurally coupled to
  resume section shape, not a linear paragraph sequence; building a second
  rendering pipeline for this milestone was judged out of proportion to it.
- `evaluate_submission()` does not enforce `next_review_due` expiration the
  way `evaluate_ingestion()` does. A `site_policies` row past its review date
  is still honored for a submission decision. Pre-existing in code this
  milestone reused unmodified; not silently fixed during this milestone.
- Applications with an `ALLOWED` policy verdict intentionally remain at
  `submission_ready` indefinitely - a visible, by-design "stuck" state until
  M11 implements a real permitted-channel dispatcher. Policy permission is
  never treated as "a human should act now."
- No cover-letter-PDF-specific binding column exists on `artifacts`; packet
  binding is entirely storage-key/content-addressed rather than
  column-backed, by design, requiring no migration.
- Real submission execution, retry/backoff, and submission-outcome handling
  (`submitting`, `submitted`, `submission_failed`, and the `submission.*`
  events) remain M11+.
- Agent 3 (monitoring/dashboard) remains later work; no code for it exists.

### Final verification counts

- make lint: clean.
- make typecheck: 150 source files, no issues.
- make verify-schema: 12 passed, 0 failed; 9-revision chain, head
  0009_application_preferences (unchanged by this milestone).
- make check-shadowing: clean.
- make test-unit: 1651 passed.
- make test-integration: 1042 passed.
- make test-security: 1134 passed, 0 skipped, under
  REQUIRE_SECURITY_TESTS=1.
- make test-evals: 61 passed, 51 corpus cases, 0 provider calls.
- make audit: no known vulnerabilities.
- make secrets-scan: passing, read-only.

No remote push occurred for this milestone; this repository has no
configured remote.


## M11 — COMPLETE, verified 2026-09-23

### Submission safety foundation

- Enforced expiration of `site_policies.next_review_due` for automated submission decisions; any policy past its review due date fails closed.
- Dual safety switch architecture: real automated submission strictly requires both the environment configuration `SUBMISSION_MODE=enabled` and the persisted database setting `settings.submission_enabled = true`. Disabling either switch halts dispatch with zero external network I/O.
- Fake-by-default email transport boundary (`FakeEmailTransport`), ensuring local tests and default environments perform zero live network calls.
- `SMTPEmailTransport` with conservative delivery outcome classification: `SENT` (250), `TRANSIENT_FAILURE` (4xx, connection error before DATA), `REJECTED` (5xx, recipients refused), `AUTH_FAILED` (credential error), and `AMBIGUOUS` (disconnect during or after DATA, timeout, unclassified error).
- Migration `0010_submission_retry_schedule` adding nullable `next_attempt_after` (`TIMESTAMPTZ`) to `submissions` with index `ix_submissions_retry_schedule` on `(next_attempt_after, attempt)` for efficient scheduler discovery.

### Human-authorized email submission

- Consequential execution boundary: real submission requires an explicit owner-authenticated HTTP request to `POST /api/v1/applications/{id}/submit` with CSRF protection, strict authentication before lookup, and non-enumeration guarantees.
- Zero automatic initial submission: applications reaching `SUBMISSION_READY` never automatically transition to `SUBMITTING`.
- Recipient grounding: destination email is derived exclusively from authoritative persisted `job.url` containing an explicit `mailto:` URI matching the target policy domain or subdomain. The submit request accepts zero caller-supplied recipient fields (`extra="forbid"`).
- State-machine invariant: initial transition `SUBMISSION_READY -> SUBMITTING` is strictly restricted to `human` actors (`ALLOWED_TRANSITION_ACTORS`).
- Dedicated submission worker running under `app_agent` consuming `submission.requested` on stream `evt.submissions` / consumer group `cg.submission`.
- Worker-side execution: worker independently acquires row-level locks on `Application` and `Submission`, revalidates safety switches, current unexpired policy, authoritative recipient, and review completeness before dispatch.
- Exact submission timestamping: `submission.started_at` is set immediately before transport invocation, and `finished_at` is recorded upon return.
- Event audit trail: emits `submission.requested` (human actor), `submission.started` (system actor), and terminal `submission.succeeded` or `submission.failed` (system actor), carrying no credentials, recipient addresses, or candidate content.

### Durable retry and recovery

- Safe retry authority: automatic retry eligibility is persisted exclusively when the transport outcome is classified as `EmailSendStatus.TRANSIENT_FAILURE` at execution time by populating `submission.next_attempt_after = <future timestamp>`. No textual parsing of failure strings or SMTP codes reconstructs retry eligibility.
- Bounded retry budget and schedule: `submission_max_attempts = 3`, with deterministic backoff delays of `60s` after attempt 1 and `300s` after attempt 2. Attempt 3 transient failure exhausts the budget and leaves `next_attempt_after = NULL`.
- Periodic scheduler: runs every 30s under the restricted `app_monitor` database role (read-only on core tables, insert on `events`). Discovers due rows (`next_attempt_after <= now()`) and emits deterministic `submission.retry_requested` events using UUID5 deduplication (`ON CONFLICT (id) DO NOTHING`). The scheduler performs zero mutations on applications or submissions.
- Atomic worker claim: worker consumes `submission.retry_requested`, acquires row-level locks on `Application` and due `Submission` attempt N, and atomically clears `prior_submission.next_attempt_after = None` to seal the claim.
- State-machine recovery: adds `SUBMISSION_FAILED -> SUBMITTING` transition restricted exclusively to `system` actors.
- Dynamic N+1 provenance rebinding: under the claim transaction, worker re-evaluates current policy, destination, review completeness, and resolves a fresh handoff packet. Attempt N+1 is created append-only (`attempt = N + 1`, `result = PENDING`) bound to current provenance rather than stale attempt N attributes.
- Content-addressed packet currentness: preflight 6 matches the bound packet artifact's storage key against the content-addressed digest formula of current approved resume/cover-letter versions and answers. Stale-but-existing packets fail preflight closed with zero sends.
- Historical immutability: attempt N historical rows remain immutable (preserving result, failure reason, HTTP status, timestamps, policy, and packet binding), with only `next_attempt_after` cleared as scheduling metadata upon claim.

### Critical safety behavior

- AMBIGUOUS transport outcomes (such as server disconnects during DATA or timeouts) NEVER automatically retry under any circumstance (`will_retry=False`, `ambiguous=True`).
- REJECTED (5xx) and AUTH_FAILED outcomes never automatically retry.
- Both safety switches (`SUBMISSION_MODE=enabled` and `settings.submission_enabled=true`) and all policy/material gates are revalidated before every external attempt.
- The scheduler cannot initiate an initial submission and has no database privileges to mutate application or submission rows.
- No caller-controlled or LLM-synthesized destination email addresses can authorize external dispatch.

### Explicit limitations

- EMAIL execution currently requires an authoritative persisted `mailto:` destination in job data; ordinary URLs without an authoritative `mailto:` cannot be dispatched via email.
- No ATS/API submission channel has been implemented.
- No browser-based application submission exists.
- No CAPTCHA automation, bypass, or stealth proxies exist anywhere in the codebase.
- Ambiguous delivery outcomes require human investigation and are never automatically retried.
- Maximum of three total email attempts per application; no infinite retries.
- No dedicated manual UI retry or reconciliation workflow exists yet.
- M12 operational monitoring and alerting has not been implemented.
- Broader UI/UX redesign remains outside M11.

### Final verification counts

- make lint: clean.
- make typecheck: 157 source files, no issues.
- make verify-schema: 12 passed, 0 failed; 10-revision chain, head
  0010_submission_retry_schedule.
- make check-shadowing: clean.
- make test-unit: 1915 passed.
- make test-integration: 1096 passed.
- make test-security: 1243 passed, 0 skipped, under
  REQUIRE_SECURITY_TESTS=1.
- make test-evals: 61 passed, 51 corpus cases, 0 provider calls.
- make audit: no known vulnerabilities.
- make secrets-scan: passing, read-only.

No remote push occurred for this milestone; this repository has no
configured remote.

## M12 Phase 1 — COMPLETE, verified 2026-11-03

Commit `4ade88b`. Details: [M12_PHASE1.md](M12_PHASE1.md).

### Generation auto-pause

- A critical-severity `guardrail.violation.detected` of kind `fabrication`, `numeral_invented` or `sensitive_auto_answer_attempt` is consumed by the existing `cg.monitor` worker and opens a durable `alerts` row with `auto_paused = true`. Other recognized guardrails open non-pausing alerts; unknown kinds or severities are ignored.
- Alert identity is `uuid5(source event id)` inserted with `ON CONFLICT DO NOTHING`, so duplicate, concurrent or post-restart delivery cannot create a second incident or reopen a resolved one.
- Generation is paused iff any `auto_paused` alert is not `resolved` (open and acknowledged both pause). There is no cached, Redis or process-local flag.
- Fail-closed checks run at `StructuredClient.complete` and `AnthropicTransport` (a missing session also refuses), and at first-draft, cover-letter and revision entry points.
- Paused worker deliveries roll back all domain writes, are recorded as `blocked:generation_paused:<attempt>` and acknowledged (no poison loop), and are replayed by reconciliation only after the final resolution.
- Revision: the pause is re-checked after lease acquisition (rolling back the uncommitted T2), and a pause during T3 releases the attempt `running -> accepted` through a fenced `release_for_generation_pause` (current owner and epoch only), with no failure class and no retry-budget charge. The same review completes after resolution with exactly one new version.

### Human resolution API

- Owner-only `GET /api/v1/alerts/generation-pause`, `GET /api/v1/alerts`, `GET /api/v1/alerts/{id}` and `POST /api/v1/alerts/{id}/resolve`, behind the existing session, CSRF and role checks; reviewers cannot enumerate incident IDs.
- Responses are typed and omit raw alert detail, titles and entity references. Resolution keeps the row as history.

### Explicit limitations

- Pause checks govern admission to each provider call; they do not cancel a request already in flight. The monitor is asynchronous.
- `numeral_invented` and `sensitive_auto_answer_attempt` have no current producer (numeral violations are emitted as `fabrication`).
- No heartbeat, DLQ, submission-failure, stall or metrics monitoring yet (M12 Phase 2). No frontend for alerts yet (M12 Phase 3).
- No migration, grant change or new event type.

### Final verification counts

- ruff check: clean. mypy: 160 source files, no issues.
- verify-schema: 12 passed, 0 failed; head 0010_submission_retry_schedule.
- check-shadowing: clean. Down migration up to date.
- Unit: 1922 passed. Integration: 1143 passed.
- Safety: 1280 passed, 0 skipped, under REQUIRE_SECURITY_TESTS=1.
- Offline evals: 61 passed, 51 corpus cases, 0 provider calls.
- pip-audit: no known vulnerabilities. Secrets scan: no unbaselined secrets.
- `ruff format --check .`: 174 pre-existing files; M12 added no new violations.

## M12 Phase 2 — COMPLETE, verified 2026-11-03

Commit `4b0dfa4`. Details: [M12_PHASE2.md](M12_PHASE2.md). Preceded by the
formatting-only commit `5d5da3c`, which resolved the 174-file `ruff format`
debt (identical ASTs) and refreshed four `.secrets.baseline` line numbers.

### Operational monitoring

- The existing `cg.monitor` `schedule.tick` handler runs the unchanged job snapshot, then `evaluate_operations` under a transaction-scoped advisory lock, and merges the results into the bucket's `metrics_snapshots.metrics.operations`. Stale ticks (older than 900 s) skip the rules.
- Rules and resolution:
  - `worker_heartbeat_missing` (high): registered instance silent > 90 s; auto-resolves only when the same instance beats again.
  - `dead_letter` (high): one per dead-lettered (group, event); human only.
  - `submission_ambiguous` (high): structured `ambiguous: true` on `submission.failed`; human only.
  - `submission_failure_spike` (high): latest 10 real send attempts (`started_at` set), fires at 4+ failures, never with fewer than 10 attempts; auto-resolves on a full window with 3 or fewer.
  - `application_stalled` (medium): system-owned status > 24 h; auto-resolves when the status changes.
  - `approval_backlog` (low): pending > 30; auto-resolves at 30 or fewer.
  - `consumer_lag` (medium): lag > 500 now and 10 minutes earlier; auto-resolves when known and 500 or fewer.
- Alerts: one unresolved alert per (kind, subject); `uuid5` identities with `ON CONFLICT DO NOTHING`, so repeated evaluation is idempotent and never reopens a resolved alert. Every operational alert has `auto_paused = false`.
- Heartbeats: every worker kind registers in Redis before its first step and a background pulse refreshes it (liveness, not progress). Only a signalled clean stop deregisters.
- Owner-only, typed, read-only `GET /api/v1/monitoring/summary` and `GET /api/v1/monitoring/snapshots`, with no alert detail, worker instance names or entity identifiers.

### Explicit limitations

- Detection granularity is one 5-minute tick. Heartbeat and lag rules depend on Redis and pause, rather than guess, during an outage. The monitor cannot report its own death.
- No notifications, digests, Prometheus endpoint or frontend (M12 Phase 3+).
- No migration, grant change or new event type.

### Final verification counts

- ruff check and `ruff format --check .`: clean (297 files formatted).
- mypy: 163 source files, no issues. verify-schema: 12 passed, 0 failed; head 0010_submission_retry_schedule. check-shadowing: clean. Down migration up to date.
- Unit (`tests/unit`): 1959 passed. Integration: 1169 passed.
- Safety: 1335 passed, 0 skipped, under REQUIRE_SECURITY_TESTS=1.
- Offline evals: 61 passed, 51 corpus cases, 0 provider calls. Frontend: 171 passed.
- pip-audit: no known vulnerabilities. Secrets scan: no unbaselined secrets.

## M12 Phase 3 — COMPLETE, verified 2026-11-03

Commit `1cdc545`. Details: [M12_PHASE3.md](M12_PHASE3.md).

### Operator UI

- Backend, owner only and additive:
  - The pause status adds `unresolved_auto_pause_count`.
  - `GET /api/v1/alerts` gains `status` (open/resolved/all), `kind`, `severity` and `auto_paused` filters and `limit`/`offset` pagination, newest first.
  - Alert reads add a server-derived fixed `title`, `requires_human`, `resolution` (human/monitor), and `application_id`/`job_id` only where the UI can navigate. Raw detail and other entity identifiers remain hidden.
- Frontend:
  - The API client treats HTTP 503 as an error except for the readiness probe, and non-JSON bodies surface as `ApiError`.
  - A server-authoritative `OperationsProvider` drives a global generation-pause banner. It never clears optimistically, keeps the last confirmed state with a stale warning when a refresh fails, and fetches nothing for non-owners.
  - `/alerts` and `/alerts/[id]`: filters, pagination, and explicit, consequence-stating human resolution.
  - `/monitoring`: built from real snapshots, with stale-data and Redis-unavailable states. Missing metrics show "Not available".
  - An owner-only Operations navigation section, and a header bell showing the real unresolved count. The hard-coded "4 items" bell is gone.

### Explicit limitations

- State is polled every 30 s (and on focus), not pushed.
- Verified by tests and the production build. Live-browser verification was deferred to the isolated demo environment.
- No migration, grant change or new event type.

### Final verification counts

- ruff check and `ruff format --check .`: clean. mypy: 163 source files, no issues.
- verify-schema: 12 passed; head 0010_submission_retry_schedule. check-shadowing and down migration: clean.
- Unit: 1959 passed. Integration: 1190 passed. Safety: 1356 passed, 0 skipped, under REQUIRE_SECURITY_TESTS=1.
- Offline evals: 61 passed, 51 cases, 0 provider calls.
- Frontend: lint and typecheck clean, 213 tests passed, production build succeeded.
- pip-audit: no known vulnerabilities. Secrets scan: no unbaselined secrets.

## M13 Phase 1 — COMPLETE, verified 2026-11-03

Commit `68a8b70`.

### Application review and submission UI

- Backend, read-only, owner and reviewer:
  - `GET /api/v1/approvals` (filters, pagination, oldest-first pending).
  - `GET /applications/{id}/submission-readiness`.
  - `GET /applications/{id}/submissions`.
  - `GET /applications/{id}/cover-letter`.
  - Each runs in a `READ ONLY` transaction, returns 404 for unknown applications, and never returns snapshots, failure reasons or recipient addresses.
- `evaluate_submit_preconditions()` is now the single definition of submit readiness:
  - Submit runs it stopping at the first failure, so refusals are unchanged.
  - Readiness runs it to completion, failing closed per check.
  - `can_submit` is owner-only and requires both switches.
- Frontend:
  - Approvals inbox.
  - Application workspace with a server-derived next step. A failed section is shown as unavailable, never as empty.
  - Cover-letter and answers-package review.
  - Submission readiness with an owner-only, confirmed Submit that is disabled in dry-run mode.
  - A prepared attempt reads "Not sent yet" and is never described as queued for sending.
- Demo seed: application A is awaiting approval (resume, cover letter and 3 questions). Application B is `submission_ready` against a `mailto:` job in dry-run mode.

### Explicit limitations

- The answers approval has no pending row (M9 rule): it is opened and decided in one step and does not appear in the inbox.
- Readiness and history are polled (every 10 s while sending), not pushed.

### Final verification counts

- Backend: 3237 passed under REQUIRE_SECURITY_TESTS=1 (unit, integration and safety), 0 skipped. Review status API: 15 passed.
- ruff check and `ruff format --check .`: clean. mypy: 165 source files, no issues.
- verify-schema: 12 passed; head 0010_submission_retry_schedule. check-shadowing and down migration: clean.
- Frontend: lint and typecheck clean, 259 tests passed, production build succeeded.
- Browser checks in the isolated demo, as owner and reviewer, at 1280, 768 and 375 px with no horizontal overflow:
  - Answers saved, refused while incomplete, then approved.
  - Dry-run Submit disabled.
  - Reviewer has no Submit control.
  - An unknown application shows Not found.
- pip-audit: no known vulnerabilities. Secrets scan: no unbaselined secrets.

## M13 Phase 2 — COMPLETE, verified 2026-11-04

Commit `7e0e481`.

### Profile, facts and settings

- Backend:
  - Fact verification and evidence are attributed to the authenticated owner. `verified_by` and `added_by` are no longer taken from the request: they are optional, and any value other than the signed-in user is refused with 422. This closes the Phase-2-era placeholder in `FactVerifyRequest`.
  - `ProfileRepository.get_singleton()` orders its lookup, so the profile served is stable.
  - Fixed a pre-existing defect: fact edit, verify, unverify and retire, and profile and section updates, returned 500. The server-generated `updated_at` was expired after the UPDATE and could not be read during async serialisation. The routers now commit and refresh. These paths had no API-level test, and now they do.
  - `GET /api/v1/profile` now returns `email`, `phone`, `work_authorization`, `min_salary` and `max_salary` as null for non-owners. Reviewers still see the name, headline, sections and facts they review against.
  - `GET /api/v1/settings/submission-gate`, owner and reviewer, read-only. It reports `submission_mode`, the persisted switch and `live`, so the UI can show that the persisted switch alone never sends. The existing `submission-enabled` contract is unchanged.
- Frontend:
  - `/profile`: basics with an owner-only edit form. It sends the full current record plus `version`, because PUT replaces every field, and explains a stale-version conflict. Reviewers see no contact details or work authorization, and the owner sees only "Recorded" for work authorization outside the form.
  - Profile sections, each with an owner-only "Suggest facts" action. Suggestions are deterministic and land as drafts.
  - Facts: summary, status filter, editing drafts, and confirmed verify, return-to-draft and retire. Each is re-read from the server. Evidence is listed and added without any attribution field.
  - `/settings`: deployment mode and the persisted switch, shown side by side. An owner-only toggle with a confirmation whose wording follows the mode: in dry-run it says nothing is sent. Reviewers get a read-only view.
  - 422 messages name the first invalid field.

### Explicit limitations

- Profile sections are read-only in the UI. Section CRUD exists in the API and is not yet exposed here.
- Matching thresholds and quotas are configuration-only, and the Settings page says so instead of showing unconfirmed values.

### Final verification counts

- Backend full run: 3245 passed under REQUIRE_SECURITY_TESTS=1, 0 skipped. Two `test_resume_rework_api` concurrency tests timed out while the host slept mid-run (10.9 h wall clock) and passed on re-run.
- After the final changes:
  - Unit tests plus the 40 integration files touching profiles or facts: 2925 passed.
  - New tests: fact attribution, lifecycle and profile redaction (6), and settings gate (2).
  - `get_singleton()` is now ordered, because in a shared database an unordered `LIMIT 1` returned different profiles across calls.
- ruff check and format: clean. mypy: no issues. check-shadowing: clean. `git diff --check`: clean.
- Frontend: lint and typecheck clean, all tests passed (including 25 new profile and settings tests), and the production build succeeded.
- Browser checks in the isolated demo:
  - The reviewer is read-only, with no contact fields and no switch control.
  - The owner verified a draft fact, and the counts were re-read.
  - The switch dialog's dry-run wording is correct. It was cancelled with Escape, the switch was left off, and focus returned to the trigger.
  - No horizontal overflow at 375 px.
- Secrets scan: no unbaselined secrets, and the baseline is unchanged.

## M13 Phase 3 — COMPLETE, verified 2026-11-04

Commit `89256f1`.

### Real dashboard and agent activity

- Backend, read-only, owner and reviewer, in `READ ONLY` transactions:
  - `GET /api/v1/agent-runs`: filters for `agent`, `status` and `application_id`, `limit` up to 100, `offset`, newest first.
  - `GET /api/v1/agent-runs/summary?days=1..90`.
  - `GET /api/v1/agent-runs/{id}`: the run plus its step names, statuses and timings.
  - Run metadata only. Error messages, step outputs, guardrail results and LLM prompts or responses are never returned. `error_type` is echoed only when it is a class name, otherwise "Error". An unknown run returns 404.
- Frontend:
  - The dashboard reads approvals, applications, job counts, agent runs and the fact summary.
    - Each source degrades on its own: an unknown count shows "—" with a retry, never 0.
    - The priority card never says nothing is waiting when the inbox could not be read.
    - Only the owner is greeted by name, taken from the profile; reviewers never trigger a profile read.
  - `/agent-activity`: a 7-day summary, filters, pagination, and steps loaded on demand.
- Removed:
  - `lib/demo-data.ts` and the components only it fed: `DemoDataNotice`, `JobRow`, `ActivityItem`, `ReviewAlert` and `FitDistribution`.
  - `PlaceholderPage`, and the Interviews and Analytics pages and navigation items, because no data source exists for either.
  - Tests now assert that no app, component or library source references demo fixtures.

### Explicit limitations

- The dashboard reads up to 200 applications for its pipeline counts. A server-side count endpoint is a candidate once volumes grow.
- Analytics stays hidden until there is real analytics data to show.

### Final verification counts

- Integration:
  - Authorization matrix plus agent-runs and review-status APIs: 362 passed, with the 3 new routes classified.
  - Agent-runs API: 4 tests (redaction, filters and pagination, detail, summary).
  - The full backend suite last ran for Phase 2. The Phase 3 backend change is additive: one new router and its registration.
- ruff check and format: clean. mypy: 167 source files, no issues. check-shadowing: clean.
- Frontend: 290 tests passed, typecheck and lint clean, production build succeeded.
- Browser checks in the isolated demo:
  - The dashboard shows real counts: 2 jobs, 2 applications, 1 waiting, and a pipeline with 1 needing attention and 1 ready.
  - Agent activity lists 9 replay runs at $0.00. The failed seed run shows only `ModelOutputError`.
  - No horizontal overflow at 375 px.
- Secrets scan: no unbaselined secrets, and the baseline is unchanged.

## M13 Phase 4 — COMPLETE, verified 2026-11-05

Commit `c882f11`. This closes M13: Phases 1–4 are all committed and regression-tested together.

### Header identity

- The hard-coded "LS" avatar is gone. Initials come from the signed-in user's own session email (`demo.reviewer@…` → "DR"), with no extra request.
- The session has no display name. The profile's `full_name` is the candidate's, so it is never used: a reviewer is never shown the candidate's identity as their own.
- If there are no usable letters, the avatar shows an icon, never someone else's initials.
- Only the owner's avatar links to `/profile`; a reviewer's is a labelled, non-interactive badge.

### Accessibility (WCAG 2.2 AA)

- **Contrast:**
  - Three design tokens were minimally darkened, keeping their hue: `ink-soft` #687082→#636B7C, `signal-strong` #398468→#33765D, `signal-review` #A97A2F→#8B6427.
  - Before, `ink-soft` measured 4.30–4.44:1 on cream and violet-soft surfaces, and the strong and review badges 3.4–3.9:1. Every token now reaches at least 4.5:1 on every surface and on its own badge tint. A test computes this from `tailwind.config.ts`.
  - Translucent `ink-soft/70` and `/75` text was removed from M13 surfaces: the pipeline stage numbers and the search and login placeholders.
- **Headings and landmarks:**
  - Section cards are `h2` regions named by their heading. Settings and Agent activity previously jumped from h1 to h3.
  - The workspace's second "Breadcrumb" navigation is renamed "Application location", so no two landmarks share a name.
- **Mobile navigation drawer:** it is now a modal dialog. Focus moves into it, Tab is trapped, Escape closes it, and focus returns to the control that opened it. Both openers announce `aria-haspopup="dialog"`.
- **Dialogs:** the confirmation dialog's focus trap now covers links, selects and other focusable elements, not only inputs and buttons.
- **Forms:**
  - The required change note and required answers carry `aria-required`.
  - A disabled "Request changes…" button explains why it is disabled, through `aria-describedby`.
  - Server reasons are shown as sentences.
  - The login form declares `method="post"`, so a native submission could never put credentials in a URL. Its button is already disabled before hydration; this is defence in depth.
- **Announcements:** skeleton loading states contain screen-reader text, and on-demand loads (run steps, evidence) are `role="status"`.
- **Targets and readability:**
  - Standalone links that were 16–20 px tall are now at least 24 px (SC 2.5.8).
  - Mobile tab labels are 11 px, up from 10 px.
- **Honesty:** the sidebar no longer always says "Review what is waiting for your approval"; it now points to Approvals neutrally.

### Defect fixed

- `PUT /profile` with an outdated `version` returned 422 (`ValidationError`), so the edit form's "the profile changed since you opened it" guidance never appeared. It now returns 409 (`ConflictError`), and the E2E API journey asserts it.

### End-to-end tests (opt-in, `-m e2e`)

- `tests/e2e/` is deselected from every normal run (`pytest`, `make test`, unit, integration, safety). It is selected only with `-m e2e`.
- A session that mixes the two suites stops with a clear message. Playwright's sync event loop and the async API fixtures cannot share a process.
- **API journeys** (`tests/e2e/test_m13_api_journeys.py`, 6 tests, `make test-e2e`), with real `POST /auth/login`, CSRF and PostgreSQL:
  - sign-in, roles, missing-CSRF refusal and sign-out;
  - cover-letter review;
  - answers: refused while incomplete, then saved and approved;
  - dry-run submission: refused for the owner (409), forbidden for the reviewer (403), body fields rejected (422), never started;
  - profile: edit, stale-version 409, reviewer redaction and 403;
  - facts: spoofed `verified_by` and `added_by` rejected (422), attribution taken from the session, reviewer 403s, retirement;
  - agent runs: error messages, step output and LLM prompts and responses never returned.
  - Both email transports are replaced by tripwires that fail a test if anything tries to send.
- **Browser suite** (`tests/e2e/browser/`, 17 tests, pytest-playwright 0.10.0 with Playwright 1.64.1 and Chromium, `make test-e2e-browser`), against the isolated demo stack:
  - signed-out redirect, wrong password, sign-in and sign-out, each user's own initials;
  - dashboard real counts, plus "—" and Retry on an intercepted failure;
  - agent activity with no leak markers in the page or in the API responses;
  - the full review workflow on a fresh application: cover letter approved; answers refused, answered, saved and approved; readiness incomplete;
  - dry-run submission disabled for the owner and absent for the reviewer, with API 409 and 403;
  - profile edit, always restored afterwards, and reviewer redaction;
  - fact attribution spoof refused, verified as the owner, retired;
  - the settings dialog never implies dry-run can send, and Escape returns focus;
  - every M13 page for owner and reviewer at 1280 px and 375 px: one `main`, an h1, the avatar, no horizontal overflow;
  - the mobile navigation drawer's keyboard behaviour.
- **Safety:** the browser session stops unless `.env.demo` marks a demo environment with dry-run and the running API reports `dry_run` with `live: false`. No test confirms a Submit or turns the switch on.
- **Determinism:** `python -m scripts.demo_env e2e-fixture` creates fresh, uniquely named synthetic rows for each run: an application awaiting review with an unanswered required question, a draft fact, and a throwaway reviewer. It runs on the host against the demo's published ports, through the real services with replayed generation. Read-only checks find seeded rows by fixed titles, never by row order.
- Setup and run commands are documented in the README under Testing → End-to-end. The new Makefile targets are `e2e-install`, `test-e2e` and `test-e2e-browser`.

### Explicit limitations

- The login rate limit (20 per address and 8 per email per 15 minutes) is deliberately left as it is. One browser run signs in 4 times, so at most about four runs fit in 15 minutes.
- Each browser run leaves its synthetic application, fact and reviewer in the demo database. They are clearly named `E2E …` and `e2e-reviewer-…`.
- The 3.8 GiB Docker VM cannot hold the dev stack and the demo stack under load: the demo API failed with `ENOMEM`. With your approval, the dev `jobforge-api`, `jobforge-web` and `jobforge-minio` containers were paused for the browser runs and restarted afterwards.
- `pip-audit -r requirements-dev.txt` reports two advisories in the pre-existing test-only `pypdf==6.16.0` pin (fixed in 6.21.4). This is left for M14's dependency work. `make audit` (`requirements.txt`) is clean.
- `make test-e2e` runs inside the dev API container. In this phase the API journeys were run with the host `.venv` against the same dev PostgreSQL.

### Final verification counts

- **Backend:** full suite (unit, integration and safety) with `REQUIRE_SECURITY_TESTS=1`: 3267 passed, 0 failed, 0 skipped in 38 min. That run included the 6 API journeys before they became opt-in; the normal suite now collects 3261 and deselects the 23 E2E tests.
  - The run included the 409 fix. Code changed after it: the E2E harness, `scripts/demo_env.py` (a new command) and the frontend login form.
- **E2E, API journeys:** 6 passed, 0 failed, 0 skipped, on each of three runs.
- **E2E, browser:**
  - With the dev containers paused: 17 passed, 0 failed, 0 skipped on two consecutive runs.
  - The final run, with the dev containers running: 17 passed, 0 failed, 0 skipped.
  - Earlier runs failed for the infrastructure reasons recorded above (Docker VM `ENOMEM`, the login rate limit after back-to-back runs) and for three harness issues, all fixed: Next.js dev-overlay `status`/`alert` regions, a substring heading match, and the profile restore not being in a `finally`.
- **Frontend:** 321 tests passed (31 new in `accessibility-polish.test.tsx`), typecheck and lint clean, production build succeeded.
- **Static checks:** ruff check and format clean (313 files). mypy: 167 source files, no issues. verify-schema 12 passed. check-shadowing clean. `git diff --check` clean.
- **Secrets scan:** no unbaselined secrets, and the baseline is unchanged. `make audit`: clean.
- **Browser checks, in the in-app browser and the E2E suite:**
  - Owner initials "DC" and reviewer "DR".
  - The dashboard, agent activity, review screens, profile, facts and settings at 1280 px and 375 px, with no horizontal overflow.
  - The mobile drawer: a dialog, focus inside, Escape returns focus to "More navigation".

## M14 Phase 1 — security and dependencies (2026-11-05)

Implementation: `1601d39`.

- Upgraded pypdf 6.16.0 → 6.21.4, AnyIO 4.7.0 → 4.14.2,
  pytest 8.3.4 → 9.0.3 and its compatible asyncio plugin → 1.3.0.
  The pytest major update is required by CVE-2025-71176's first patched version.
- Kept Next.js on 15.x (15.5.29), paired with eslint-config-next; kept React 19.0.0.
  Patched ESLint 9.x and PostCSS 8.x. Vitest/coverage move to 4.1.12 because
  the old test-toolchain major has no fully patched dependency path.
  Vite 6.4.5 and React plugin 4.7.0 retain compatible APIs.
- Scoped overrides fix Next.js's internal PostCSS and TypeScript ESLint's
  minimatch. Refreshed only reviewed public npm SHA-512 checksums in the
  secrets baseline; no scanner exclusions added.
- Rationale, advisory links and reproduction commands: `docs/M14_SECURITY.md`.

Verification on the final dependency set:

- Full backend, Python 3.12.14, replay/fake email/dry-run,
  `REQUIRE_SECURITY_TESTS=1`: **3261 passed, 23 E2E deselected, 0 failed,
  0 skipped**, 292.08 seconds. Includes renderer and authorization regressions.
- Frontend under Node 22.23.4: **321 passed, 24 files**; lint, typecheck and
  production build passed. The same checks also passed under host Node 26.8.2.
- `pip-audit -r requirements-dev.txt`: **0 known vulnerabilities**, down
  from 31 advisories in three packages; `pip check`: no broken requirements.
- Clean `npm ci` and `npm audit`: **0 vulnerabilities**, down from 22 affected
  packages (4 critical, 4 high, 12 moderate, 2 low).
- Ruff check passed; format: 313 files already formatted; mypy: 167 source
  files without issues; schema: 12 passed; model shadowing and down migration
  checks passed; offline grounding: 61 tests passed, 51 corpus cases, zero
  provider calls; changed-file secret scan and `git diff --check` passed.
- No containers modified/restarted, no real messages/applications sent,
  no cloud infrastructure provisioned, no push.

Known maintenance notes: ESLint 9 reports an upstream support/deprecation
warning and `next lint` is deprecated in Next.js 15; both still pass. Audit
results are point-in-time, and the scoped overrides need review on upgrades.
Browser/API E2E remain opt-in and were not run in this dependency phase.

## M14 Phase 2 — CI reliability (2026-11-05)

Implementation: `12f62b4`.

- CI now covers pushes/PRs to `dev` and `main`, plus manual dispatch. Jobs
  have timeouts, read-only repository permissions and non-persisted checkout
  credentials. Replay, fake email and dry-run submission are workflow defaults.
- Preserved backend lint/format, mypy, schema, shadowing and migration checks;
  full backend coverage runs with PostgreSQL, Redis and runner-only MinIO.
  Added the missing app_audit login and fail-fast SQL role setup.
- Safety and six API journeys run in separate processes. The new JUnit gate
  rejects absent, malformed, empty, failing or skipped reports; nine tests
  exercise those failure paths. Reports/coverage are retained as CI artifacts.
- Frontend now requires `npm ci`, audits all dependencies and runs lint,
  typecheck, tests and build. The Dockerfile also rejects missing/broken
  lockfiles rather than falling back to `npm install`.
- Python audit includes development requirements. The tracked-file secrets
  gate remains read-only; baseline additions are reviewed runner-only dummy
  credentials. CAPTCHA/stealth checks scan manifests/lockfile, fixing the
  old scanner self-match. Actionlint checks both workflows in CI.
- Added separate manual `playwright.yml`: ephemeral demo config, isolated
  demo services, health wait, migrations/init/fictional seed, Chromium,
  17 guarded journeys, JUnit gate and runner-only cleanup. No submission or
  generation worker starts. See `docs/CI.md` for operation and local equivalents.

Independent local verification:

- Full backend with coverage and `REQUIRE_SECURITY_TESTS=1`: **3270 passed,
  23 E2E deselected, 0 failed, 0 skipped**, 386.15 seconds. Coverage:
  **9449 / 10330 lines (91.47%)**.
- Separate safety suite: **1392 passed, 1901 deselected, 0 failed, 0 skipped**,
  118.70 seconds. Separate API E2E: **6 passed**, 5.07 seconds.
- Browser E2E against patched Next.js 15.5.29 under Node 22: **17 passed,
  0 failed, 0 skipped**, 45.33 seconds. All three JUnit gate invocations passed.
- New report gate tests: **9 passed**. Ruff check and format passed (315
  files); mypy passed (167 application sources and the new script separately).
- Both workflows passed actionlint 1.7.7 with ShellCheck 0.11.0. All **33**
  workflow shell steps passed Bash syntax parsing. Tracked-file secret scan
  and `git diff --check` passed.
- Phase 1's final Node 22 checks (321 frontend tests, lint, typecheck, build,
  clean install and zero-vulnerability audits) still cover the unchanged
  frontend source/dependency set.

Verification limits and recovered attempts:

- GitHub-hosted Actions and production Docker image builds were not executed
  locally. No push was made; image builds remain configured with `push: false`.
- Initial browser attempt on occupied port 3100: **1 failed, 16 errors**;
  the pre-existing Next.js 15.1.3 process returned server errors. A subsequent
  attempt before the isolated frontend started: **1 failed, 16 skipped**.
  Explicit binary/working-directory fixes and a healthy sign-in check preceded
  the successful final 17-test run. No application code change was needed.
- The final browser run used a separate `/tmp` frontend copy/cache on port
  3114 and the existing isolated demo API/database. It created synthetic E2E
  fixture rows. Temporary frontend processes were stopped afterwards; no
  existing process/container was stopped or restarted.
- Manual workflow dispatch becomes available when the workflow exists on the
  default branch. Hosted clean-runner startup still needs verification there.

## M14 Phase 3 — documentation and repository hygiene (2026-11-05)

Implementation: `75c8f0e`.

- Replaced the stale M3 README with current architecture, ten-revision schema
  status, host/development/demo setup, test/CI commands, troubleshooting and
  explicit limitations. Removed obsolete claims that future agents and UI
  are not implemented and that `make check` represents the whole CI suite.
- Added `docs/OPERATIONS.md` for password/TOTP sessions, CSRF, owner/reviewer
  authorization, shared-workspace visibility, audit/worker role boundaries,
  submission gates and network-capable transport choices.
- Updated SECURITY.md to distinguish implemented behavior from planned
  Cognito/passkey/token/AWS/deployment controls, and corrected the current
  secret scanner/dependency-lock description. No production readiness claimed.
- Added `docs/REPOSITORY_HYGIENE.md`: tracked artifact review, local env history
  check, reviewed baseline policy, ignore/build-context rules and known finding.
- Ignored browser reports, auth state and HARs; excluded local evidence,
  private inputs, nested env/private-key files and Terraform state from Docker
  contexts. Kept source, npm lock and env examples available. No valued local
  files were deleted and no license was added.
- Repaired scaffold secret scanning to enumerate Git-tracked/staged files,
  rather than ignored `.venv` packages. Failure to enumerate Git is a failure;
  script filenames have no secret exemption. Four new regression tests prove
  scope, force-added secret rejection, missing-metadata handling and no script
  filename bypass. The full CI detect-secrets gate is unchanged.

Independent verification:

- **1786 unit tests passed, 1511 deselected, 0 failed, 0 skipped**, 9.40 seconds;
  includes the four new checks. Targeted new tests: **4 passed**, 0.86 seconds.
  The JUnit gate accepted the unit report.
- **24** relative documentation link targets exist; **22** representative
  Git-ignore/source-preservation assertions passed. Tracked sensitive/generated
  artifact path review found none unintended; local refs show no `.env` or
  `.env.demo` commit history. No real ignored credential values were exposed.
- Tiny synthetic Docker scratch build/export: **22** sensitive/output paths
  excluded; source/lock files and root/nested env examples preserved. This was
  not an application image build and changed no running containers.
- Ruff check passed; format: **316 files** already formatted; mypy: **167**
  application source files without issues. Schema: **12 passed**; shadowing and
  generated down migration checks passed. Actionlint/ShellCheck, full tracked
  secret scan and `git diff --check` passed.
- Backend/frontend runtime and dependencies did not change in this phase.
  Phase 2's full backend/coverage, safety, API/browser E2E and Phase 1's final
  frontend/security-audit verification remain applicable; the tooling changes
  received their own full unit regression above.

Unresolved finding (preserved, not waived):

- `make verify` aggregate: **22 passed, 1 failed, 0 warnings**. Its import-direction
  rule reports **31 pre-existing upward import occurrences** from core into
  agents/services. Before this phase it was **21 passed, 2 failed**, including
  ignored virtualenv secret false positives. The scanner defect is fixed; the
  architecture diagnostic remains a real failing diagnostic and is documented
  in README/OPERATIONS/REPOSITORY_HYGIENE. A runtime boundary refactor is outside
  M14's documentation/hygiene scope. No import allowlist or rule weakening added.
- Hosted Actions and production Docker image builds still await a hosted run.
  No push, infrastructure provisioning, real email/application, unrelated
  container restart or M15 implementation occurred in any M14 phase.
