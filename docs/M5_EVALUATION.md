# M5 automatic deterministic evaluation

New, deduplicated ingestion results emit job.normalized in the domain transaction.
The opt-in worker-optimizer service consumes it through M4 as cg.optimizer.
Redis carries IDs; the handler reads the job and verified, non-deleted facts from
PostgreSQL. No model, provider or paid fallback is used, regardless of LLM_MODE.

WORKER_DATABASE_URL must use app_agent. WORKER_PROFILE_ID can select the candidate;
without it, exactly one owner profile must exist. Missing or ambiguous profiles
fail observably through M4 retry/DLQ handling, never a first-row guess. No tenant
or job ownership model is added to the existing single-candidate architecture.

## Assessment contract

assessment_status is authoritative. Assessed rows require a score and existing
recommendation; insufficient_evidence rows require both NULL. A database CHECK
and persistence validation enforce the contract. Existing rows default to assessed.
Only assessed results reach existing 65/55 automatic disposition thresholds.
Unresolved results remain evaluated with evidence and explicit reasons. Human
review is a hold boundary; no automatic human approval is generated.

Requirements come only from the existing deterministic description parser.
Empty/low-confidence extraction, no required requirements, missing lexical support,
unmatchable terms or missing dates for duration claims cause an unresolved result.
The legacy lexical matrix is retained, alongside an assessment overlay distinguishing
unknown from match/partial support. A lexical not_met without contrary evidence
is unknown for automated disposition, not a proven mismatch. All extracted scored
requirements (including preferred) must have support; unknown salary or absent
preferred requirements are not scored dimensions and do not prevent assessment.
Dated partial support retains the existing partial-credit formula.

All three alignment dimensions must be known. Seniority compares stated job level
with an explicit verified max_seniority preference. Domain uses exact equality to
a human-authored target role; lexical non-equality alone is unknown. Location
requires a known matching candidate/job country and explicit verified accepts_remote,
accepts_hybrid or allowed_states preferences, using CandidateConstraints vocabulary.
No CandidateConstraints defaults are inherited. Conflicting preferences are unknown.
Differences between known preferences and job attributes are mismatches, scored
with existing weights; unstated values receive no positive or negative credit.
Authorization/sponsorship/citizenship/clearance language forces human review;
work authorization is never inferred or sent to a model. Conservative matching
will deliberately leave many jobs for review.

The assessment stores candidate ID/version, source event date, matched fact IDs,
alignment reasons and all available evidence. Profile-version changes detected
during reads fail for retry. Facts outside their explicit validity dates are not
used. The legacy prefilter's hardcoded candidate assumptions are not applied to
automatically discard jobs; grounded matching and arithmetic reuse existing libraries.

## Durability and compatibility

M4 owns transactional claims and retries. The handler also locks the job so
separate normalization events cannot duplicate an evaluation. Only normalized jobs
enter this initial automatic flow; retries after completion do not re-evaluate.
Explicit re-evaluation remains the existing persistence contract, not a new worker
subscription. An unresolved latest evaluation cannot enter scored tailoring context.

GET /jobs/{id}/evaluation adds assessment_status and allows null score/recommendation
for unresolved records only. Current frontend pages do not consume this response;
no visual or frontend changes were needed. Historical assessed rows retain semantics.

Migration 0007 backfills with a constant column default, preserving append-only
records. Downgrade takes an exclusive lock and refuses if any unresolved record
exists. It never deletes history or synthesizes old scores. Operators must retain
the newer schema when such records exist; no lossy rollback is supported.
