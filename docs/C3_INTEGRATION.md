# C3 API integration

POST `/api/v1/applications/{id}/resume-review/request-changes` now accepts
`{"approval_id": "<approval displayed by GET resume-review>", "note": "..."}`.
The approval ID is required; older clients must refresh/update. It identifies
an artefact under review, never the actor. Identity still comes from the session
and every mutation requires CSRF.

The API calls `request_changes_and_regenerate`: lock the application, validate
the approval ID, record the human decision through C2, invoke
`regenerate_after_changes` through C3, read current state, then commit once.
No worker is needed for this synchronous integration. Existing events and status
history remain in the transaction; no fire-and-forget task can lose the work.

A thrown pipeline error rolls back the decision and all database writes. The
application stays awaiting_approval (or changes_requested if that decision was
already committed by the older C2 endpoint). Resubmit the same approval ID and
note to retry. A completed revision is reused by C3's fingerprint; concurrent
requests serialize on the application lock. A changed note or actor is refused.
Once another review has opened, a delayed old command returns 409 without changing
that review. Provider charges cannot be rolled back with the database.

A successful revision stops at resume_ready; a human must explicitly start a new
review and approve the new version. A failed verification persists the failed
version and stops at verification_failed. Neither outcome inherits approval.

## Model configuration

The injectable API client uses the existing StructuredClient, configured
LLM_MODE, LLM_MODEL_TAILORING and LLM_MAX_COST_USD_PER_RUN. Replay remains exact
cassette-only and never falls back to the network. Live/record require explicit
LLM_TAILORING_INPUT_PRICE_PER_MILLION and
LLM_TAILORING_OUTPUT_PRICE_PER_MILLION for the configured model, plus the existing
provider credential. Prices have no guessed defaults. Confirm prices for the
chosen model when configuring a deployment. C3 continues using its existing
client for selection, drafting, summary and claim verification.

## Remaining limits

- The documented maximum of three rework cycles is not enforced by the existing
  C3 service or this integration. Manual-authoring handling remains future work.
- Generation runs inside the HTTP request and holds a database lock throughout
  model calls. Durable background execution and automatic retry/backoff require
  a worker/consumer implementation; the current workers directory has none.
- A failed attempt's database audit and run rows roll back with the transaction,
  as in the existing C3 contract; external provider charges are not undone.
- This change does not automatically resume already-stalled applications. Their
  original reviewer can resubmit the stored approval ID and note to this endpoint.
