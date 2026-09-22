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
