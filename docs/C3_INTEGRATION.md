# C3 API integration

POST `/api/v1/applications/{id}/resume-review/request-changes` accepts
`{"approval_id": "<approval displayed by GET resume-review>", "note": "..."}`.
The approval ID identifies the resume under review, never the actor. Identity
comes from the session, and every mutation requires CSRF.

The API calls `request_changes_and_regenerate` synchronously. Its durable
boundaries are explicit:

| Boundary | Work | Outcome |
|---|---|---|
| T1 | Record the human decision and resolve one revision attempt | Commit |
| T2 | Acquire a fenced attempt lease and move the application to `resume_generating` | Commit |
| T3 | Generate, verify, and persist the new resume and domain events | Commit or rollback |
| T4 / T4' | Record attempt success or failure | Separate commit |
| T5 | Recover the application after a failed T3 when allowed | Separate commit |

A T3 failure does not undo the committed T1 decision or T2 lease history.
The attempt records the failure, and T5 returns the application to
`changes_requested` when recovery is allowed. A retry with the same approval
and note reuses or resumes the existing attempt; a completed revision is
identified by its fingerprint and is not generated again. A different note or
actor is refused. A terminally failed attempt requires explicit reviewer
release and replacement. Concurrent requests coordinate through the fenced
attempt lease; application locks are taken before attempt locks.

A successful revision stops at `resume_ready`; a human must explicitly start
a new review and approve the new version. A valid negative B5 verdict still
completes the inference and attempt. B6 persists the failed-verification
resume, and the application stops at `verification_failed`. Neither outcome
inherits approval. Provider charges cannot be rolled back with T3.

## Durable inference evidence

C3 supplies one `IndependentInferenceAudit` writer to B1 selection, B2
drafting, B3 summary, and B5 verification. B5 creates a distinct stage and
spend ledger for every claim in the existing claim order; a safe claim index
links its stage to that invocation without recording raw claim prose. Stage,
request, call, and final outcome evidence is committed on an independent
audit connection, outside T3's domain transaction. It remains visible from a
separate connection during model calls and survives a later T3 rollback.
Direct callers outside C3 use `DomainInferenceAudit`, whose savepoints remain
inside their caller's transaction.

Every stage commits its run/step evidence and request digest before invoking
the model. Each measured response is recorded as its own `llm_calls` row,
including usage, cost, and the exact request digest, before output validation
or a possible schema repair. Only validated, redacted output is stored. A
repair uses the existing prompt and at most one additional call. The writer
stores guardrail findings and any `guardrail.violation.detected` event together
on its independent connection, with the existing event payload, actor,
aggregate, and correlation. It does not emit a duplicate domain event.

Audit evidence failure stops that stage before another model call or repair;
failed final-outcome persistence produces a best-effort
`inference.audit.outcome_not_recorded` diagnostic. Provider timeout or task
cancellation propagates without inventing an `llm_calls` row or zero usage for
an unknown outcome. Observed spend is local to B1, B2, and B3 stages and to
each B5 claim. There is no revision-wide inference budget. A well-formed B5
`overreach` or `unsupported` verdict is a completed inference, distinct from
provider, schema, audit, timeout, or cancellation failure. The structured
negative verdict still reaches B6 for failed-verification persistence.

## Model configuration

The injectable API client uses the existing `StructuredClient`, configured
`LLM_MODE`, `LLM_MODEL_TAILORING`, and `LLM_MAX_COST_USD_PER_RUN`. Replay
remains exact cassette-only and never falls back to the network. Live/record
require explicit `LLM_TAILORING_INPUT_PRICE_PER_MILLION` and
`LLM_TAILORING_OUTPUT_PRICE_PER_MILLION` for the configured model, plus the
provider credential. Prices have no guessed defaults. Confirm prices for the
chosen model when configuring a deployment. C3 continues using its existing
client for selection, drafting, summary, and claim verification.

## Remaining limits

- The documented maximum of three rework cycles is not enforced by the existing
  C3 service or this integration. Manual-authoring handling remains future work.
- Generation runs inside the HTTP request. Durable background execution and
  automatic retry/backoff require a worker/consumer implementation; the current
  workers directory has none.
- This change does not automatically resume already-stalled applications. Their
  original reviewer can resubmit the stored approval ID and note to this endpoint.
