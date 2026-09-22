# M4 runtime

## Implemented boundary

A domain transaction writes an immutable `events` row. The outbox relay selects
unpublished rows in batches of 200 using `FOR UPDATE SKIP LOCKED`, publishes an
`event_id` reference to Redis Streams, and commits only the publication fields.
If Redis fails, the database transaction rolls back. If the process dies after
XADD but before commit, the next relay may publish a duplicate.

Consumers reload the source row from PostgreSQL; Redis never supplies trusted
payload data. The event type chooses the stream even when its aggregate type is
different (for example, resume events attached to an application). Registrations
explicitly specify handled event types. The initial monitor registers only
`schedule.tick`; it does not claim to implement all future monitoring features.

A PostgreSQL advisory transaction lock serializes `(event_id, consumer_group)`.
An independently committed `worker.delivery.started` event records the attempt
before handler work. A handler runs inside a savepoint together with the
`ProcessedEvent` claim. Its domain writes, emitted events and successful claim
commit together; Redis ACK happens afterwards. A duplicate returns without
executing the handler. The handler must use the supplied session and must not
commit, roll back the enclosing transaction, or perform irreversible external
side effects. A process handles one delivery at a time; its pool needs at least
two connections for the independent attempt ledger.

On a handler exception, the savepoint removes domain writes and the claim. The
outer transaction records `agent.run.failed`, including group, source event ID,
attempt number, exception-class counts, and whether retry is allowed. Exception
messages and source payloads are excluded. Five total attempts and delays of
1/4/16/60 seconds implement ARCHITECTURE.md's bound; three occurrences of one
exception class terminate early. A lost process leaves a durable start without
completion: the next consumer records an interrupted attempt and applies the
same budget. Timeouts count as failed attempts. Unsupported event versions and
forged human-only actors terminate without calling a handler.

Terminal failure records a `ProcessedEvent.result` of `dead_letter`, never a
successful result. The failure event's own outbox delivery writes its reference
to both the system stream and `evt.dlq`. Thus failure of Redis during DLQ delivery
cannot erase the failure record. Malformed/missing transport references are
quarantined in PostgreSQL before ACK, with a deterministic transport-reference
claim and without storing the untrusted field value.

Pending messages can be reclaimed after five minutes. Own pending messages are
retried when their durable backoff is due. On startup and every 60 seconds,
published source events lacking this group's completion record are republished
from PostgreSQL. This recovers Redis loss or trimming. Operational replay can
also specify exact event IDs through `DeliveryRuntime.reconcile(..., event_ids=...)`.
Unsubscribed event types are ACKed but not marked processed, preserving future
replay after a handler is added.

## Runnable processes

Set `WORKER_DATABASE_URL` through the process environment, using URL-encoded
credentials. It is deliberately required and has **no owner-DSN fallback**.
The current database role and privileged role flags are checked at startup.
Credentials and DSNs are never logged. Normal Redis/config settings still come
from `Settings`, including the local `.env` if present.

- `python -m workers.outbox_relay` requires `app_agent`.
- `python -m workers.monitor` requires `app_monitor`.
- `python -m workers.scheduler` requires `app_monitor`.

The scheduler creates one deterministic monitoring tick per five-minute bucket;
multiple schedulers/restarts cannot create two source events for that bucket.
The monitor writes job-count snapshots to the existing `metrics_snapshots` table
and emits `metrics.snapshot.created` in the same transaction. Counts contain no
profile data. These commands use the existing worker image dependency set. They
are opt-in and have not been started as persistent processes on the user's stack.

For Compose, set `WORKER_AGENT_DATABASE_URL` and `WORKER_MONITOR_DATABASE_URL`
in the local environment or uncommitted `.env`, then explicitly start:

```sh
docker compose --profile core --profile workers up -d --build worker-outbox worker-monitor worker-scheduler
```

The ordinary core/full profiles do not enable workers. The worker profile mounts
code read-only and allows 40 seconds for container shutdown (the runtime drains
for at most 30). Only `.env.example` was updated; real credentials were untouched.

SIGINT/SIGTERM stops intake, gives the current step up to 30 seconds to finish,
then cancels it. Cancellation rolls back uncommitted writes and leaves delivery
recoverable. PostgreSQL/Redis pools close on exit. Source failure payloads are
inspectable in `events`; terminal completion is explicit in `processed_events`.

## Scope and limitations

No database migration, role/grant changes, API change, UI change, provider calls,
or external submissions are introduced. Existing synchronous resume revision
remains synchronous. Evaluation, first-draft generation, rendering, and
application handlers belong to later milestones and are not registered here.
Application-specific error/status transitions must be implemented in their
own handlers when those workflows are registered; this milestone's actual
consumer is a system-level monitoring workflow.

Automatic stream trimming is intentionally not enabled in M4. PostgreSQL is the
durable archive; retention and terminal-failure replay remain operator decisions.
Terminal failures do not automatically clear completion claims and repeat work.
Recovery scans at most 200 eligible events per interval, and delivery failures
are read from the existing indexed event log rather than a new retry table.
This prioritizes auditability and avoiding a schema change at current scale;
backlog/retention performance should be measured before expanding throughput.
Exactly-once database effects do not imply exactly-once external provider calls;
future expensive handlers must retain the existing inference audit, budgets and
step-level resumption safeguards.
