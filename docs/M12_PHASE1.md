# M12 Phase 1: operational safety and generation pause

Implementation for review; no Phase 2/3 features or frontend changes.
Baseline: M11 closeout `6fb23e8659e384795f69159f2e7e78028e0cdfe4`.

## Persistent authority and policy

The existing `alerts` table is the only pause authority. Generation is paused iff
an Alert has `auto_paused=true` and `status != 'resolved'`. Both open and
acknowledged incidents block generation; resolving one of several does not resume
it. There is no cached/process-local/Redis boolean or additional table.

Existing `guardrail.violation.detected` events route to `evt.system` and are now
consumed by the existing `cg.monitor` worker beside its snapshot handler.
Critical `fabrication`, `numeral_invented`, and `sensitive_auto_answer_attempt`
incidents auto-pause (SECURITY.md §4.7). Other recognized guardrails persist an
incident without auto-pause. Unknown kinds/severities are ignored rather than
inventing a policy. Current producers are B1 selection/injection detection, B2
resume drafting, B3 summary, B5 verification, and cover-letter drafting. Some
numeral violations currently emit `fabrication`; both documented kinds are handled.

Each alert has a deterministic UUID derived from the immutable source event ID.
The primary key plus INSERT ON CONFLICT DO NOTHING prevents duplicate incidents,
even outside DeliveryRuntime. Redelivery cannot reopen a resolved incident.
DeliveryRuntime's existing advisory lock and ProcessedEvent transaction remain
responsible for delivery idempotency. The monitor never resolves a guardrail or
generation-pause alert (M12 Phase 2 auto-resolves only its own operational kinds).

Only kind, severity, a fixed category/title, source event UUID, a valid optional
run UUID, the envelope's entity reference, and timestamps are retained. Raw detail,
prompts, generated text, tokens, credentials and arbitrary payload fields are not
copied. No new event vocabulary or notifications were added.

## Inference protection

`packages/core/generation_safety.py` provides the shared database authority.
`StructuredClient.complete` checks it before replay, record or live inference.
Every production stage passes its current database session. A missing session
fails closed. The concrete AnthropicTransport also checks immediately before its
provider path, so a direct transport caller cannot omit the authority.

This covers resume first drafts (B1/B2/B3/B5), synchronous revisions/rework, and
cover-letter drafting/verification, including schema-repair calls. Evaluation,
answer preparation, rendering and submission are deterministic and have no LLM
provider boundary. There are no other production LLM/provider entry points in
the inspected tree.

First draft and cover-letter generation also check before domain changes.
Revision rework checks before T1: an already-paused request creates no decision or
attempt and leaves existing attempts/leases untouched. After lease acquisition
and before `begin_generation`, T2 re-checks the pause; if paused it rolls back
the uncommitted acquisition (no lease, no count). A pause detected in T3 is not a
technical failure and is never classified: T3 rolls back, then one fenced
transaction (`release_for_generation_pause`, current owner and epoch only)
returns the attempt `running -> accepted` with no failure class and returns the
application to `changes_requested` with the human decision intact. A stale owner
or epoch changes nothing. The next `acquire()` continues that interrupted
acquisition without charging `attempt_count` (monotonic by trigger) or applying
the ceiling, so pauses cannot exhaust the retry budget. After the final incident
is resolved, re-posting the same decision produces exactly one new version.
Existing independent inference evidence and crash-window reconciliation are
preserved.

Checks govern admission to each call. They do not cancel a provider request
already in flight, and monitor consumption is asynchronous: a guardrail event
becomes pause authority when its Alert transaction commits. No claim is made
that merely emitting an event retroactively cancels running inference.

## Paused worker deliveries and recovery

DeliveryRuntime rolls back the domain savepoint on GenerationPausedError and
stores `blocked:generation_paused:<delivery-attempt>` in ProcessedEvent. Transport
completion means ACK, not generation success; no generated event or workflow
advancement survives. The source event, delivery-start evidence and block remain
in PostgreSQL across restarts.

Reconciliation excludes blocked work while any auto-pause incident is unresolved.
After final human resolution, existing reconciliation can republish that same
source event. The same event/group ledger is updated rather than deleted. The
saved blocked attempt number distinguishes intentional pause from a crash after
recovery began: such a crash retains the existing interruption/budget/dead-letter
behavior and cannot trigger unlimited inference. No new worker, scheduler,
notification, DLQ monitor or retry-budget exception was introduced. The monitor
worker never resolves an incident or dispatches submissions.

## Minimal API and authorization

- GET `/api/v1/alerts/generation-pause`: typed global pause status.
- GET `/api/v1/alerts`: typed unresolved incidents, including acknowledged ones.
- GET `/api/v1/alerts/{alert_id}`: typed incident, including resolved history.
- POST `/api/v1/alerts/{alert_id}/resolve`: explicit owner resolution.

All four routes require the existing authenticated owner role. Router-level
session/CSRF checks precede authorization and lookup. Reviewer/anonymous callers
cannot enumerate incident IDs. Resolution locks the existing row, preserves prior
acknowledgment when present, records the resolving owner UUID in safe JSON metadata,
and sets resolved_at. It never deletes the row. Repeated resolution preserves the
first resolution metadata. Pause is always recomputed from remaining incidents.

The actual M1-M11 product is a shared workspace, not a multi-tenant operator
system (SECURITY.md threat model and the existing global submission setting).
Owner is the narrowest existing human role for system-wide resolution; no admin
role or new cross-tenant authority was invented. APIs omit raw alert detail,
free-text titles and entity references. Existing candidate-isolation checks are
unchanged and remain covered by the full integration suite.

## Schema, privileges and tests

No migration or grant changes. Head remains `0010_submission_retry_schedule`.
Existing app_monitor SELECT plus INSERT/UPDATE on alerts and processed_events,
and INSERT on events, suffice. It gains no writes to applications, submissions,
approvals, resumes, cover letters or facts. Human-only resolution is enforced in
the API/service; the pre-existing monitor UPDATE grant on alerts is unchanged.

New tests cover policy, persistence, acknowledgment vs resolution, multiple
incidents, duplicate/concurrent handling, new-session pause visibility, each
protected generation path, revision fencing, blocked delivery recovery and its
crash window, API authorization/non-enumeration, safe payload projection and
monitor privileges. The existing authorization matrix classifies every new route.

Unit and offline-eval fixtures explicitly model an unpaused synthetic database;
the safety tests exercise the real check. A suite-wide trap forbids the real
external LLM transport in automated tests. Injected fake transports and replay
continue exercising grounding, budgets and verification without provider calls.

Phase 2/3 work remains out of scope: broader submission/queue/heartbeat/DLQ
monitoring, notifications, metrics/dashboard/history APIs, and frontend work.
