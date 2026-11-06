# SMTP dispatch recovery design

Status: design only. No dispatch, transport, scheduler or submission state-machine behavior is changed by the resume export milestone.

## Failure to address

`DeliveryRuntime.process` owns one database transaction and a nested savepoint around the submission handler. `_execute_attempt` flushes `submission.started`, calls SMTP, and only subsequently records the result. A flush is not a commit. SMTP may accept DATA while a process death, cancellation or database commit failure discards the local success. The generic runtime records/retries interrupted deliveries; its committed worker-start ledger does not prove whether SMTP accepted the message.

Current transport handling correctly treats a reported timeout or disconnect as ambiguous. It does not cover a successful external action followed by loss of its local transaction. SMTP and PostgreSQL cannot participate in one atomic commit. SMTP Message-ID is a correlation identifier, not a remote deduplication guarantee. Do not promise exactly-once delivery.

## Proposed durable dispatch boundary

Add an append-only `email_dispatches` ledger and a separate append-only transition history. Each row binds application, submission attempt, human authorization event, owner, exact recipient, sender, subject/body digest, immutable attachment identifiers and checksums, policy revision, consent expiry, transport, stable Message-ID and optional provider idempotency key. Sensitive MIME payloads live encrypted in private storage with retention controls; events and routine logs carry identifiers and hashes only.

Use a uniqueness constraint on submission attempt plus authorization, and unique dispatch IDs. State transitions require compare-and-swap against a monotonic fence. A database migration must define the API, dispatcher, auditor and monitor privileges independently. Generic agent/monitor roles cannot authorize or dispatch email.

1. **Prepare transaction:** validate explicit human authorization, complete current approvals, safety switches, authoritative destination, policy currency, budgets and attachments. Persist exact MIME inputs and their hashes, dispatch `prepared`, the stable Message-ID, authorization binding, and an outbox signal. Commit before any SMTP activity.
2. **Claim transaction:** lock the prepared row and associated application, recheck switches/policy/consent/current materials, and compare immutable payload hashes. Transition to `dispatching` with a fence and start time. Commit before opening a socket. Only the process that durably claimed this fence may make this one send call.
3. **External send:** send the prepared immutable bytes once. It runs outside any domain transaction. No automatic library retries, no regeneration and no fresh Message-ID on replay.
4. **Record transaction:** compare the fence and persist `accepted`, `rejected`, `auth_failed`, `retryable_before_send`, or `uncertain`. Persist attempt result, application transition and outbox events in this transaction. If the write fails after SMTP, recovery must not repeat the send.
5. **Recovery:** an expired `dispatching` claim becomes `uncertain`, including a crash immediately before network activity. A lease is not permission to resend. Duplicate stream deliveries observe the ledger and cannot call SMTP again. Generic DeliveryRuntime retries must not encompass the external send; use a dedicated dispatcher with these explicit boundaries.

A crash after claim but before SMTP can require manual reconciliation even though nothing was sent. That availability cost is intentional: the system cannot infer the external result from a vanished process.

## Retries and reconciliation

Only a positively identified failure before message transmission may create another bounded attempt. Keep authentication/permanent rejection non-retryable, and timeouts, disconnects, cancellation and unclassified errors uncertain. Distinguish queue retry, transport retry and a new human authorization; none should silently substitute for another.

For uncertainty, show the exact intended message, dispatch time and Message-ID to the owner. Check provider logs or the sender's Sent folder through a separately authorized read capability. Record evidence and the deciding human. If delivery cannot be established, a new send requires explicit human reauthorization acknowledging possible duplication. Never mark SMTP acceptance as recipient delivery or ATS receipt. Bounce and reply processing are separate future capabilities.

A provider with a documented, durable idempotency-key contract could improve recovery. Its retention period, payload mismatch rules and account scoping must be verified and tested before relying on it. Do not treat a generic SMTP Message-ID as equivalent.

## Cancellation and operational controls

Turning off submissions or cancelling before claim prevents the send. Once the committed dispatch is in progress, cancellation cannot recall a message; surface that boundary clearly. Resolve the race with the claim transaction, rather than promising a final check can eliminate every in-flight race. Limit attachment sizes and dispatch concurrency; require authenticated TLS and an explicitly configured sender. Keep payloads out of error detail, browser telemetry and test traces.

## Required synthetic verification

Use an isolated PostgreSQL database and a controlled SMTP server, with subprocess kill points: before prepare commit; after prepare; after claim commit but before socket creation; before DATA; after server acceptance but before return; after return but before result commit; during commit failure; cancellation; lease expiry and restart. Assert SMTP call counts and durable states, not only returned statuses.

Also cover simultaneous consumers, duplicate outbox delivery, stale fences, changed approvals/policy/recipient/attachments, switch shutdown races, corrupted MIME bytes, expired consent, exhausted budgets, SMTP timeouts/disconnects, denied roles, and secret redaction. The key acceptance criterion is that any dispatch which could have reached SMTP is never automatically resent after loss of its final result.

## Rollout proposal

Implement and test behind a disabled capability flag. Migrate existing pending attempts into a non-dispatchable state requiring fresh authorization; never reconstruct consent from a general resume approval. Run fake transport, then an explicitly approved controlled SMTP trial with synthetic addresses. Require a separate approval before enabling real recipients. Keep a documented rollback that disables new claims while preserving all uncertain/accepted ledger records.

## Failure states requiring human reconciliation before a resend

These are future design requirements, not new behavior in M16.6. The current
send/commit window remains unresolved and blocks approval to enable live sending.

| Observed state | Why the external result is unknown | Required action before another send |
| --- | --- | --- |
| `dispatching` claim expired after process death, including death before opening the socket | A committed claim proves permission, not whether transmission occurred | Mark uncertain; owner reconciles provider evidence against the stable Message-ID |
| Timeout, disconnect, cancellation or unclassified failure during/after DATA, without a definitive rejection | The server may have accepted the complete message while its reply was lost | No automatic resend; reconcile, then explicitly reauthorize if duplication remains possible |
| SMTP returned acceptance, but result flush/commit failed or the process died before recording success | Remote acceptance outlives a rolled-back local transaction | Treat as uncertain until provider acceptance evidence is recorded by the owner |
| Database commit acknowledgement was lost after a send | The result transaction itself may have committed | Re-read the durable ledger first; unresolved outcome still requires human reconciliation |
| Worker restart, stream redelivery or exhausted lease finds no final result for an already claimed dispatch | Retry machinery cannot infer what the vanished sender did | Observe the same fence; never call SMTP again merely because the queue retried |
| Recipient-level partial acceptance or ambiguous recipient results | Retrying the entire envelope can duplicate delivery to accepted recipients | Reconcile per recipient; a new authorization must name the exact remaining destinations |
| Provider accepted the message, but no delivery/ATS acknowledgement exists | Acceptance does not establish delivery, nor establish non-delivery | Do not resend automatically; inspect separately authorized provider/bounce evidence |

A positively proven failure before transmission may be eligible for a bounded
retry only under a still-valid authorization and unchanged payload. Authentication
failures and definitive permanent rejections require correction and a new human
decision, rather than reconciliation of an unknown acceptance. Never interpret a
missing Sent-folder entry or a missing local success row as proof of non-delivery.

## Independent-review refinements (still design only)

- Enforce **at most one prepared, dispatching or uncertain dispatch per
  application**, including when authorizations differ. Attempt/authorization
  uniqueness alone cannot prevent two active sends. A new authorization never
  bypasses an unresolved uncertain row.
- Before the external call, hash the exact immutable MIME bytes again and compare
  them with the prepared digest. Refuse to begin if the committed claim has too
  little remaining lease time for the bounded send timeout and receipt write.
  An expired claim cannot authorize another caller to resend.
- Append every observed transport outcome to independent receipt evidence even
  when the domain result transaction or fence update is rejected. Include the
  dispatch/fence, payload hash, observation time and exact classification; do not
  discard a late success merely because it lost a compare-and-swap. If durable
  receipt evidence cannot be written, the dispatch remains uncertain. This does
  not make SMTP and the database atomic.
- Keep one Message-ID for retries proven to precede transmission within the same
  logical dispatch. A separately authorized resend is a **new dispatch with a
  new Message-ID**, linked to the earlier uncertain/accepted dispatch and the
  owner's duplication acknowledgement. Neither identifier is a deduplication
  promise by receiving systems.
- Define the transport boundary explicitly: distinguish a positively proven
  failure before complete message-data transmission, complete transmission with
  an unknown response, and reported server acceptance. If the transport cannot
  prove which boundary was crossed, classify it as uncertain. Acceptance still
  does not establish recipient delivery or ATS receipt.
- Use per-dispatch encryption keys wrapped by a master key held outside the
  database and repository, in an OS credential store or managed key service.
  Record key versions; rotate wrapping keys without losing historical receipts.
  Require an approved retention/expiry policy for encrypted MIME, receipts,
  backups and key recovery before enabling dispatch. No implicit retention
  period or plaintext backup is authorized by this design.
