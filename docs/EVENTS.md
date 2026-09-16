# EVENTS.md

**Project:** AI Job Application Platform (`jobforge`)
**Status:** Design — v1.0
**Transport:** Redis Streams (v1) → SQS/EventBridge (optional, later)
**Durability:** Postgres `events` table (outbox) is the record of truth
**Parent document:** [ARCHITECTURE.md](./ARCHITECTURE.md)

---

## 1. Principles

1. **Events are facts about the past.** Names are past tense. An event never commands; a consumer decides what to do about it.
2. **Postgres first, bus second.** Every event is written to `events` in the same transaction as the state change it describes. The outbox relay publishes to Redis afterwards.
3. **Payloads are thin.** An event carries identifiers and just enough context for routing and display. Consumers re-read current state from Postgres — never trust the payload as state, because by the time a message is consumed the world may have moved on.
4. **At-least-once delivery.** Every consumer is idempotent on `(event_id, consumer_group)`.
5. **Transport-agnostic envelope.** Nothing in the envelope is Redis-specific, so moving to SQS is an adapter swap.
6. **The event log is the audit trail.** It is append-only, never deleted, and directly powers the `/events` dashboard page.

---

## 2. Envelope

Every event, on every transport, has this shape:

```json
{
  "id": "01927f3a-...",
  "type": "resume.generated",
  "version": 1,
  "occurred_at": "2026-09-08T14:23:11.482Z",
  "aggregate_type": "application",
  "aggregate_id": "01927f2b-...",
  "correlation_id": "01927f10-...",
  "causation_id": "01927f39-...",
  "actor": { "type": "agent", "id": "resume_optimizer" },
  "trace": { "trace_id": "4bf92f...", "span_id": "00f067..." },
  "payload": { }
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | UUIDv7 | Idempotency key; matches `events.id` |
| `type` | string | From the catalogue in §3. Unknown types are DLQ'd, never guessed at |
| `version` | int | Schema version of `payload` (§5) |
| `occurred_at` | ISO-8601 UTC | When the fact became true |
| `aggregate_type` | enum | `job` \| `application` \| `resume` \| `approval` \| `submission` \| `system` |
| `aggregate_id` | UUID \| null | Null only for system-wide events |
| `correlation_id` | UUID | Constant for a whole pipeline run: one job's journey from discovery to submission |
| `causation_id` | UUID \| null | The `id` of the event that caused this one — lets the UI draw a causal chain |
| `actor` | object | `type` ∈ `agent` \| `human` \| `system`; `id` is the agent name or user id |
| `trace` | object | W3C trace context, propagated across process and human-delay boundaries |
| `payload` | object | Type-specific; validated against a JSON Schema in `packages/core/events/schemas/` |

**Naming convention:** `<domain>.<entity_or_action>` in lower snake, past tense for facts, `.requested` for intents. Requests are the only imperative-flavoured events, and they are still facts ("a request was made").

---

## 3. Event catalogue

Legend — **P** = producer, **C** = consumers.
`ING` ingestion service · `A1` Resume Optimizer · `A2` Job Application · `A3` Monitoring · `API` FastAPI/human · `RND` render service · `DLV` delivery service · `REL` outbox relay

### 3.1 Job domain — stream `evt.jobs`

| Event | P | C | Payload |
|---|---|---|---|
| `job.discovered` | ING | ING, A3 | `{job_id, source_id, source_slug, url, external_id, title}` |
| `job.normalized` | ING | **A1**, A3 | `{job_id, company_id, normalized_title, seniority, location, is_remote, requirement_count}` |
| `job.duplicate_detected` | ING | A3 | `{job_id, duplicate_of_id, match_reason, similarity}` |
| `job.evaluation.requested` | API, ING | **A1** | `{job_id, reason: "new"\|"reevaluation"\|"manual", profile_version}` |
| `job.evaluated` | A1 | A3, API(SSE) | `{job_id, evaluation_id, score, recommendation, matched_count, missing_count, gaps[], agent_run_id}` |
| `job.shortlisted` | A1, API | A3, API(SSE) | `{job_id, evaluation_id, score, by: "agent"\|"human"}` |
| `job.discarded` | A1, ING, API | A3 | `{job_id, reason, stage: "prefilter"\|"evaluation"\|"human", score?}` |
| `job.expired` | ING, A3 | A3 | `{job_id, detected_via: "posting_closed"\|"age"}` |

### 3.2 Resume domain — stream `evt.resumes`

| Event | P | C | Payload |
|---|---|---|---|
| `resume.generation.requested` | A2, API | **A1** | `{application_id, job_id, reason: "initial"\|"rework"\|"manual", instructions?}` |
| `resume.generated` | A1 | **RND**, A3, API(SSE) | `{resume_id, application_id, version, claim_count, verified_count, agent_run_id, cost_usd}` |
| `resume.verification.failed` | A1 | A3, API(SSE) | `{resume_id, application_id, failures: [{path, claim_text, verdict, reason, fact_ids}]}` |
| `resume.render.requested` | A1 | **RND** | `{resume_id, formats: ["pdf","docx"]}` |
| `resume.rendered` | RND | **A2**, A3 | `{resume_id, application_id, artifacts: [{artifact_id, kind, size_bytes}]}` |
| `cover_letter.generated` | A2 | A3 | `{cover_letter_id, application_id, claim_count, verified_count}` |

`resume.verification.failed` is the fabrication tripwire. It carries the full failure detail because a human needs to see exactly which sentence failed and why, without opening a log.

### 3.3 Application domain — stream `evt.applications`

| Event | P | C | Payload |
|---|---|---|---|
| `application.created` | API | **A1**, A3 | `{application_id, job_id, evaluation_id, score, priority}` |
| `application.questions.extracted` | A2 | A3, API(SSE) | `{application_id, total, by_class: {factual_safe, derived, uncertain, judgement, sensitive, unanswerable}}` |
| `application.answers.drafted` | A2 | A3, API(SSE) | `{application_id, auto_answered, human_required, blanks}` |
| `application.ready` | A2 | A3, API(SSE) | `{application_id, submission_method, target_domain}` |
| `application.status.changed` | state machine (any) | A3, API(SSE) | `{application_id, from_status, to_status, actor_type, actor_id, reason?}` |
| `application.outcome.updated` | API, A3 | A3, API(SSE) | `{application_id, from_outcome, to_outcome, source: "human"\|"email_signal"\|"aging"}` |
| `application.abandoned` | API | A3 | `{application_id, reason, at_status}` |

`application.status.changed` is emitted for **every** transition without exception. It is what makes the timeline view complete, and it is the event the dashboard leans on most.

### 3.4 Approval domain — stream `evt.approvals`

| Event | P | C | Payload |
|---|---|---|---|
| `approval.requested` | A2 | A3, API(SSE), notifications | `{approval_id, application_id, kind, guardrail_flags[], human_required_answers, requested_at}` |
| `approval.granted` | API (human) | **A2**, A3 | `{approval_id, application_id, kind, decided_by, edits_applied?, decision_note?}` |
| `approval.rejected` | API (human) | **A2**, A3 | `{approval_id, application_id, kind, decided_by, reason}` |
| `approval.changes_requested` | API (human) | **A1**/**A2**, A3 | `{approval_id, application_id, kind, decided_by, instructions, target: "resume"\|"answers"\|"cover_letter"}` |
| `approval.superseded` | A2, API | A3 | `{approval_id, application_id, reason}` |

**No agent may produce `approval.granted`.** This is enforced three ways: the emitting code path exists only in the API layer; `actor.type` must be `human` and is validated by the consumer; and the `approvals` table requires a non-null `decided_by` referencing `users`. An `approval.granted` with `actor.type = "agent"` is dropped to the DLQ and raises a **critical** alert, because its existence would mean something is badly wrong.

### 3.5 Submission domain — stream `evt.submissions`

| Event | P | C | Payload |
|---|---|---|---|
| `submission.requested` | API, A2 | **A2** | `{application_id, requested_by, method}` |
| `submission.started` | A2 | A3 | `{submission_id, application_id, attempt, method, target_domain, policy_id}` |
| `submission.succeeded` | A2, DLV, API | A3, API(SSE) | `{submission_id, application_id, method, external_reference?, confirmed_by: "system"\|"human"}` |
| `submission.failed` | A2, DLV | A3, API(SSE) | `{submission_id, application_id, attempt, failure_reason, http_status?, will_retry}` |
| `submission.handoff.required` | A2 | A3, notifications | `{submission_id, application_id, reason, packet_artifact_id, apply_url}` |

`submission.handoff.required` carries `reason` values including `policy_requires_human`, `captcha_encountered`, `bot_challenge_detected`, `login_required`, `unsupported_form`. These are recorded honestly and terminate automation for that attempt. There is no downstream consumer that treats any of them as something to work around.

### 3.6 System domain — stream `evt.system`

| Event | P | C | Payload |
|---|---|---|---|
| `agent.run.started` | A1, A2, A3 | A3 | `{run_id, agent, pipeline, aggregate_type, aggregate_id, trigger_event_id}` |
| `agent.run.finished` | A1, A2, A3 | A3 | `{run_id, agent, status, duration_ms, input_tokens, output_tokens, cost_usd, steps_completed}` |
| `agent.run.failed` | A1, A2, A3, REL | A3, API(SSE) | `{run_id?, agent, error_type, error_message, event_id?, will_retry, attempt}` |
| `guardrail.violation.detected` | A1, A2 | **A3**, API(SSE) | `{kind, agent, severity, aggregate_type, aggregate_id, detail, action_taken}` |
| `policy.blocked` | ING, A2 | A3, API(SSE) | `{domain, action: "ingest"\|"submit", policy_id?, reason}` |
| `quota.exhausted` | API, A2 | A3, notifications | `{quota_type, date, limit_value, used_value}` |
| `metrics.snapshot.created` | A3 | API(SSE) | `{snapshot_id, grain, bucket_start}` |
| `alert.raised` | A3 | notifications, API(SSE) | `{alert_id, kind, severity, title, aggregate_type?, aggregate_id?, auto_paused}` |
| `alert.resolved` | API, A3 | A3 | `{alert_id, resolved_by, note?}` |
| `notification.dispatched` | A3 | — | `{notification_id, channel, kind}` |
| `schedule.tick` | scheduler | ING, A3 | `{tick_id, schedule_name, scheduled_for}` |

**Revision-attempt diagnostics (C3).** Written by the audit boundary on its
own connection, in its own transaction, so they survive the rollback of the
domain transaction they describe - which is usually the point. Every one of
them records the orchestrator noticing something wrong and continuing anyway,
so an absent diagnostic and a healthy run look identical in the domain tables
and are told apart only here. Producer is the API in every case; `actor_type`
is `system`, and they are filed against the **application** aggregate so they
interleave with its status transitions rather than needing a second query.

| Event | P | C | Payload |
|---|---|---|---|
| `revision.attempt.failure_record_failed` | API | A3 | `{attempt_id, application_id, approval_id, lease_epoch, detail: {owner, bookkeeping_error, original_cause}}` |
| `revision.attempt.fenced_out_before_failure` | API | A3 | `{attempt_id, application_id, approval_id, lease_epoch, detail: {owner, original_cause}}` |
| `revision.attempt.failure_not_recorded` | API | A3 | `{attempt_id, application_id, approval_id, lease_epoch, detail: {owner, cause}}` |
| `revision.attempt.success_not_recorded` | API | A3 | `{attempt_id, application_id, approval_id, lease_epoch, detail: {owner, result_resume_id, result_version}}` |
| `revision.attempt.recovery_failed` | API | A3 | `{attempt_id, application_id, approval_id, lease_epoch}` |
| `revision.attempt.wait_timed_out` | API | A3 | `{attempt_id, application_id, approval_id, lease_epoch: null, detail: {waited_seconds, holder_status}}` |
| `revision.attempt.reconciled` | API | A3 | `{attempt_id, application_id, approval_id, lease_epoch: null, detail: {resume_id, version, outcome}}` |
| `revision.attempt.reconcile_skipped` | API | A3 | `{attempt_id, application_id, approval_id, lease_epoch: null, detail: {resume_id, version, outcome}}` |

**`lease_epoch` is `null` on three of these**, for two different reasons, and
in both cases writing `0` instead would put a plausible-looking lie in a
column operators reason about:

* `wait_timed_out` - the caller never acquired, so it holds no epoch at all.
* `reconciled` and `reconcile_skipped` - the reconciler takes its own epoch
  inside a single guarded statement and never surfaces it, precisely so no
  caller can fence with a value it did not acquire under.

The other five are emitted by a caller holding a live lease and carry the
epoch it was acquired at.

**Inference audit failures (Batch 3B-1A).**

These diagnostics use the independent AuditBoundary connection and a separate
`INFERENCE_AUDIT_EVENTS` allowlist. They are application-scoped, system-authored
(`actor_id=inference_audit`), with application correlation. They describe evidence
failures, not committed domain outcomes or stage lifecycle transitions.

| Event | Producer | Consumer | Meaning |
|---|---|---|---|
| `inference.audit.start_failed` | Inference audit writer | A3 | Required pre-call evidence failed; do not invoke the model. |
| `inference.audit.evidence_not_recorded` | Inference audit writer | A3 | Call/response/output/guardrail evidence failed; do not repair or invoke another call. |
| `inference.audit.outcome_not_recorded` | Inference audit writer | A3 | Final stage outcome could not be persisted. |

Payload: `{attempt_id, lease_epoch, application_id, approval_id,
detail: {operation, error_type, run_id, step_id}}`. No exception text, prompt or
private input is included. Diagnostics are best-effort and cannot mask the
primary evidence failure. Evidence operations have no retries; after failure the
writer stops. Model execution must never be repeated to repair missing evidence.

The full immutable context is stored at
`agent_run_steps.guardrail_results.c3_execution` from stage creation onward.
`run_id` and the call's `run_id`/`step_id` link all evidence to that context.
Request/guardrail updates preserve the reserved namespace. Existing guardrail
payloads and job aggregate/correlation/actor semantics remain unchanged: the
optional existing `guardrail.violation.detected` envelope is inserted once in the
same transaction as step findings, with no additional lifecycle event. The caller
must use this writer instead of also emitting that same event through its domain
session. No stages are wired to this contract in 3B-1A.

**`guardrail.violation.detected` `kind` values:**

| Kind | Meaning | Default severity |
|---|---|---|
| `fabrication` | A claim could not be grounded in verified facts | **critical — auto-pauses generation** |
| `numeral_invented` | A numeral appeared that no cited fact contains | critical |
| `overreach` | A claim strengthens a real fact beyond what it supports | high |
| `prompt_injection` | Injection pattern detected in untrusted input | high |
| `sensitive_auto_answer_attempt` | An agent tried to answer a `sensitive` question | critical |
| `schema_violation` | Model output failed schema after repair | medium |
| `pii_in_prompt` | Redaction layer caught unredacted PII pre-send | high |
| `cost_ceiling` | Run exceeded its cost budget | medium |

---

## 4. Streams, consumer groups, and routing

| Stream | Max length (approx trim) | Consumer groups |
|---|---|---|
| `evt.jobs` | 50,000 | `cg.optimizer`, `cg.monitor` |
| `evt.resumes` | 20,000 | `cg.application`, `cg.render`, `cg.monitor` |
| `evt.applications` | 20,000 | `cg.optimizer`, `cg.application`, `cg.monitor` |
| `evt.approvals` | 20,000 | `cg.application`, `cg.optimizer`, `cg.monitor` |
| `evt.submissions` | 20,000 | `cg.monitor` |
| `evt.system` | 50,000 | `cg.monitor` |
| `evt.dlq` | unbounded | `cg.monitor` (alerting only; drained by a human) |

Streams are trimmed with `XTRIM ... MAXLEN ~`. Trimming is safe because Postgres holds the durable log — Redis is a delivery buffer, not an archive.

### 4.1 Consumer contract

Every handler follows the same skeleton:

```
1. INSERT INTO processed_events (event_id, consumer_group) ON CONFLICT DO NOTHING
   → 0 rows affected means already handled → XACK and return
2. Load current aggregate state from Postgres
3. If state is no longer compatible with this event (already advanced, terminal, abandoned)
   → log "stale event ignored", XACK, return.  This is normal, not an error.
4. Do the work
5. Persist state + new events in one transaction
6. XACK
```

Step 3 is what makes at-least-once safe in practice: replaying an old `resume.generated` for an application that is already `submitted` is a no-op, not a duplicate resume.

### 4.2 Retry and DLQ

| Attempt | Delay |
|---|---|
| 1 | immediate |
| 2 | 1 s |
| 3 | 4 s |
| 4 | 16 s |
| 5 | 60 s |
| 6 | 300 s → then DLQ |

`XAUTOCLAIM` reclaims messages pending longer than 5 minutes (crashed consumer). Messages that fail with the same exception class 3× are DLQ'd immediately rather than burning the full schedule. A DLQ'd message emits `agent.run.failed` and moves the affected application to `error`, which surfaces it in the UI — nothing fails silently.

### 4.3 Outbox relay

A dedicated process (`workers/outbox_relay.py`) polls every 250 ms:

```sql
SELECT * FROM events WHERE published = false ORDER BY occurred_at LIMIT 200
FOR UPDATE SKIP LOCKED;
```

publishes each to its stream (routing by `aggregate_type` and `type` prefix), then marks `published = true, published_at = now()`. If the relay dies, events accumulate in Postgres and are published on restart — the pipeline pauses rather than losing work.

---

## 5. Versioning

- `version` starts at 1 for every event type.
- **Additive changes** (new optional payload field) do not bump the version. Consumers ignore unknown fields.
- **Breaking changes** (removing/renaming a field, changing a type or its meaning) bump `version`. Both versions are then produced during a transition window, and consumers handle both until the old version stops appearing.
- Event **types are never renamed**. A rename is a new type plus a deprecation of the old one.
- Every event type has a JSON Schema in `packages/core/events/schemas/<type>.v<N>.json`. A CI contract test asserts: every type in this document has a schema, every schema has a producer, and every producer's emitted payload validates.

---

## 6. Correlation model

```
job.discovered            correlation_id = C1   causation_id = null
job.normalized            C1   ← caused by job.discovered
job.evaluation.requested  C1   ← caused by job.normalized
job.evaluated             C1
job.shortlisted           C1
application.created       C1
resume.generation.requested C1
resume.generated          C1
resume.rendered           C1
application.questions.extracted C1
approval.requested        C1
  ── human delay, possibly hours ──
approval.granted          C1   actor.type = human
application.ready         C1
submission.requested      C1
submission.handoff.required C1
submission.succeeded      C1
```

One `correlation_id` per job journey. `GET /api/v1/events?correlation_id=C1` returns the entire life of an application in order, which is exactly what the Application Detail timeline renders and exactly what makes debugging tractable.

The W3C `trace_id` rides alongside so the same journey is visible in Jaeger, including the human approval gap — which shows up as a very long span and is genuinely useful for spotting where the human is the bottleneck.

---

## 7. Events the system deliberately does not have

| Non-event | Why |
|---|---|
| `approval.auto_granted` | There is no automatic approval. Not a config option, not a fast path |
| `captcha.solved` / `challenge.bypassed` | The capability does not exist |
| `job.scraped_despite_policy` | Policy-blocked ingestion doesn't run |
| `fact.inferred` / `fact.generated` | Facts come only from the human |
| `resume.published_unverified` | Rendering reads only `verdict='supported'` claims |
| `application.submitted_without_approval` | Blocked by the state machine and by a DB CHECK constraint |

Listing these is deliberate. The event catalogue is a description of what the system is capable of, and these absences are as much a part of the specification as the events above.

---

## 8. Consistency with other documents

- Event names here are canonical; ARCHITECTURE.md §6 mirrors the list, AGENT_DESIGN.md §3–5 mirrors the per-agent producer/consumer sets.
- `events` table columns are defined in DATABASE_DESIGN.md §7.1.
- Guardrail kinds in §3.6 match AGENT_DESIGN.md §2.4 and SECURITY.md §5.
- Alert severities and auto-pause behaviour match ARCHITECTURE.md §14.2.

---

*End of EVENTS.md — v1.0*
