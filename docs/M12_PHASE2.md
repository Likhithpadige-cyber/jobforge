# M12 Phase 2: deterministic operational monitoring

Implementation for review; no frontend changes (M12 Phase 3).
Baseline: `5d5da3c` (M12 Phase 1 committed as `4ade88b`).

## What runs, and where

The scheduler already emits one durable `schedule.tick` per 5-minute bucket.
The existing `cg.monitor` worker (role `app_monitor`) now handles it with
`monitor_tick_handler`: the unchanged job snapshot, then
`evaluate_operations` (`packages/core/events/operational_monitoring.py`).

- Evaluations are serialized by a transaction-scoped advisory lock.
- A tick older than `MONITOR_STALE_TICK_SECONDS` (default 900 s) still records
  its job snapshot but skips the rules: a replayed tick must not judge the present.
- The evaluation's results are merged into the bucket's `metrics_snapshots`
  row under `metrics.operations` (counts only).

No migration, no new event type, no grant change. `app_monitor` already holds
SELECT on all tables and INSERT/UPDATE on `alerts` and `metrics_snapshots`;
it has no write access to any domain table.

## Rules

| Kind | Detection | Severity | Resolution |
|---|---|---|---|
| `worker_heartbeat_missing` | A **registered** worker instance whose last heartbeat is strictly older than `MONITOR_HEARTBEAT_MISSING_SECONDS` (90 s) | high | Auto, only when that same instance heartbeats again |
| `dead_letter` | Each `processed_events` row with `result = 'dead_letter'`, one alert per (consumer group, event) | high | Human only |
| `submission_ambiguous` | Each `submission.failed` event whose structured payload has `ambiguous: true` | high | Human only |
| `submission_failure_spike` | See below | high | Auto, when a full window is back at or under the rate |
| `application_stalled` | Application in a system-owned status (`created`, `resume_generating`, `questions_pending`, `submitting`) for strictly longer than `MONITOR_STALL_SECONDS` (24 h), measured from its latest status-history entry into that status | medium | Auto, when the application's status has changed |
| `approval_backlog` | Pending approvals strictly above `MONITOR_APPROVAL_BACKLOG` (30) | low | Auto, when the count is at or under the threshold |
| `consumer_lag` | Redis consumer-group lag strictly above `MONITOR_CONSUMER_LAG` (500) now **and** in the snapshot `MONITOR_CONSUMER_LAG_SUSTAINED_SECONDS` (600 s) earlier | medium | Auto, when the current lag is known and at or under the threshold |

Human-wait states (`awaiting_approval`, `changes_requested`,
`submission_ready`) are deliberately not stalls; they are covered by the
backlog rule.

### Submission-failure spike, precisely

- **Sample:** the `MONITOR_SUBMISSION_FAILURE_WINDOW` (10) most recent
  submission rows that represent a real send attempt: `started_at IS NOT NULL`,
  `finished_at IS NOT NULL`, `result IN ('succeeded', 'failed')`, ordered by
  `finished_at DESC, id DESC`. Preflight refusals never set `started_at` and are
  excluded. Ambiguous sends count as failures.
- **Minimum sample:** exactly the window. With fewer than 10 such attempts the
  rule is not evaluable: it neither opens nor resolves anything.
- **Condition:** `failures / 10 > 0.30`, i.e. **4 or more** of the latest 10.
  Exactly 3 of 10 is not a spike.
- **Recovery:** a full window with 3 or fewer failures.

## Alert lifecycle and safeguards

- Every operational alert is written with `auto_paused = false`. The M12
  Phase 1 pause authority (`generation_is_paused`) counts only `auto_paused`
  rows, so no operational alert can pause or resume generation.
- Nothing in this phase starts, retries or mutates a submission or application.
- **One episode per (kind, subject):** while an alert is unresolved, repeated
  evaluations open nothing. The alert id is `uuid5(kind, subject, anchor)`
  inserted with `ON CONFLICT DO NOTHING`, so the evaluation that produced a
  resolved alert can never reopen it. A new episode needs new evidence (a new
  anchor: a later heartbeat, a new attempt window, a new bucket for
  bucket-anchored rules).
- **Auto-resolution** is a single guarded UPDATE: only unresolved rows with
  `auto_paused = false` and an auto-resolvable kind, and only when recovery is
  positively observed. Unknown data (Redis unavailable, lag undeterminable, an
  unevaluable window) is never treated as recovery. Auto-resolved rows record
  `resolved_by: monitor:recovered`; no human field is filled in.
- **Human-only kinds** (`dead_letter`, `submission_ambiguous`) and every
  Phase 1 guardrail or generation-pause alert are never resolved by the
  monitor; owners resolve them through `POST /api/v1/alerts/{id}/resolve`.
- Alerts are never deleted. Alert detail holds only `category` and a
  `subject` built from identifiers; no payloads, prompts or free text.

## Heartbeats

`packages/core/heartbeats.py`. Every worker kind registers in the Redis hash
`jobforge:worker_heartbeats` (field: instance, value: kind and last-seen time)
once it has verified its database role, before its first step, and a
background task inside `serve()` refreshes it every
`WORKER_HEARTBEAT_INTERVAL_SECONDS` (15 s) for as long as the loop runs. The
heartbeat therefore measures liveness, not progress: a long generation step is
never reported as a missing worker (lack of progress is the stall and lag
rules' job). Settings reject a missing-threshold under two intervals.

- A worker is reported only after it has registered and then missed the
  threshold. An instance that never registered is never reported.
- A signalled, clean shutdown deregisters. A crash (exception or kill) leaves
  the registration to go stale and be reported.
- A Redis failure never stops a worker, and in the monitor it skips the
  heartbeat and lag rules rather than guessing.
- Registrations older than 7 days are removed after their alert exists.
- The monitor cannot report its own death; that needs an external probe
  (deployment phase).

## API (owner only)

Both routes sit behind the existing session, CSRF and owner-role router
dependencies. Reviewers get 403 and anonymous callers 401. Both are typed and
read-only.

- `GET /api/v1/monitoring/summary`: `generation_paused`, unresolved alert
  counts by severity and kind, and the latest snapshot.
- `GET /api/v1/monitoring/snapshots?limit=1..288` (default 12): recent 5-minute
  snapshots, newest first.

Responses carry no alert detail, worker instance names or entity identifiers.

## Tests

- `tests/unit/test_operational_monitoring.py`: exact rule boundaries
  (spike 9/10 samples, 3 vs 4 failures, the rate edge; heartbeat 90 vs 91 s;
  stall, backlog and lag edges), settings validation, the heartbeat writer
  (throttling, failure tolerance, deregistration), the Redis probe, and
  `serve()` beating before each step.
- `tests/integration/test_operational_monitoring.py`: every rule against
  PostgreSQL inside a rolled-back session. Covers:
  - idempotent repeated evaluation;
  - auto-resolution only on verified recovery;
  - human-only kinds that the monitor cannot resolve and that never reopen;
  - operational alerts never pausing generation, and the monitor never
    resolving a generation pause;
  - domain tables unchanged by an evaluation;
  - stale ticks skipped;
  - evaluation under the real `app_monitor` role;
  - API owner-only access and response shape.

## Explicit limitations

- Detection runs every 5 minutes, so alerts can lag the condition by one tick.
- Liveness and lag depend on Redis; a Redis outage pauses those two rules.
- No notifications, digests, Prometheus endpoint or frontend (M12 Phase 3+).
