# M7 first-draft resume tailoring

## Authorization and input boundary

Owner-only endpoints (session authentication and CSRF required):
- `POST /api/v1/profiles/{profile_id}/master-resumes`: structured private JSON;
  `schema_version: 1`, `source_label`, `sections` with `name` and `entries`;
  each entry has `text` and optional `fact_ids` referencing existing human facts.
- `POST /api/v1/jobs/{job_id}/first-draft`: explicit `profile_id`,
  `master_resume_id`, `evaluation_id`; returns application/event/profile IDs, 202.

Shared authenticated read:
- `GET /api/v1/applications/{application_id}/first-draft`: pending/completed/failed
  delivery status and resulting resume ID. Completed describes processing;
  inspect the existing resume verification status before human review/approval.

No per-owner privacy or reviewer assignments. Existing review access is unchanged.
Owners can operate on the shared candidate records, but cannot combine candidates.
Missing/foreign IDs receive generic 404 responses; reviewers receive 403 on owner
operations, anonymous requests 401. A separate fail-closed Redis first-draft bucket
uses the configured revision request allowance (default six/hour/user), without
consuming or changing the existing six/hour revision bucket.

Import appends a base-resume version; identical latest content is idempotent.
Private text stays in the database, never fixtures or source. Import does not
extract or verify facts. A later private Master Resume v6 import must map its
entries to human-verified facts. Unlinked prose is retained but cannot support a
claim. Generated prose comes only from the master-linked verified facts. An
unsupported requirement stays an evaluation gap, never new resume evidence.

Generation requires the latest explicitly named base/evaluation, an assessed M5
result bound to the candidate, the current profile version, and current verified,
undeleted, date-valid facts. Every evaluation citation must be among master-linked
facts; mismatches are rejected, never silently trimmed. Legacy evaluations lacking
candidate provenance need reevaluation. Unresolved M5 results remain unresolved
and cannot invoke this provider path.

## Execution and review

The opt-in `worker-first-draft` consumes `resume.first_draft.requested` through
M4 durable delivery. Start only with the established worker profile and explicit
app_agent DSN. Audit writes use a separate connection pool, app_audit when configured,
otherwise app_agent; there is no API-owner DSN fallback. No worker was left running
by M7 verification. Existing model/mode/price and total run budget settings apply.

The worker rechecks actor role, application/candidate/evidence IDs and a canonical
input digest before model calls and rechecks evidence after inference. It runs
existing B1 selection, B2 bullets, B3 summary, per-claim B5 verification and B6
persistence. Job text is untrusted data; it never expands the selectable fact IDs.
The bullet prompt prefers XYZ wording only when all components are supported.
Missing measurements stay missing. Deterministic schema/citation/numeral guards
and the independent semantic verifier remain enabled.

Successful output is a tailored v1 in resume_ready, with no approval, no final
artifact and no render request. Verification failures persist a failed draft in
verification_failed. Every claim carries fact IDs/verdicts; draft provenance
records candidate, master version, evaluation, input digest, gaps and source event.
Existing review/rework and its three-cycle cap remain unchanged.

Inference audit evidence commits independently and survives domain rollback.
Identical owner requests resolve to the same event; redelivery never spends again.
There is one durable delivery attempt, with no automatic retry of failed or
uncertain paid work. Failure remains visible in the status endpoint and event log.
An explicit retry/recovery product flow is not introduced in M7.

## Boundaries and limitations

No schema migration, frontend change, M8 rendering, external submission, or GitHub
push. No real resume or contact data is embedded. Tests use synthetic replay and
forbidden transports. Offline evaluations test guard and scripted-verifier
enforcement, not the live model's semantic accuracy. Human review remains required;
semantic verification is not a proof that a live model can never hallucinate.

Before M8, specify rendering templates/formats and artifact privacy/storage/download
requirements under the existing roadmap. M7 supplies structured drafts and provenance,
not PDFs/DOCX. A live-provider quality/cost evaluation or an explicit failed-run retry
UX would be separate follow-up decisions; neither is silently enabled here.
