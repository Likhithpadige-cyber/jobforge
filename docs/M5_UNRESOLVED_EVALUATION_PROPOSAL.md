# M5 unresolved evaluation proposal — approved and implemented

The user approved insufficient evidence leading to human review, never automatic
shortlist/discard, and explicitly required stopping before consequential schema
or state-machine changes. Inspection found no existing unresolved evaluation state.

## Current constraints

- job_evaluations.score is NUMERIC(5,2) NOT NULL, constrained to 0–100.
- recommendation is NOT NULL, with only strong/moderate/weak/no enum values.
- EvaluationRead exposes score as a required float and recommendation as required.
- The documented evaluated job status means a fit score was produced.
- Existing evidence JSON and rationale can preserve reasons, but cannot override
  these required fields without inventing a score/recommendation.

## Proposed bounded change

1. Add job_evaluations.assessment_status, constrained to assessed or
   insufficient_evidence. Existing rows retain their contents and default to
   assessed; historical scores are not recomputed.
2. Make score and recommendation nullable. Add a CHECK requiring both non-null
   for assessed and both null for insufficient_evidence. Retain the score range
   check for assessed rows. Do not add a negative or neutral sentinel score.
3. Store available evidence, dimension-level unknown/match/mismatch results and
   explicit reasons in the existing match_matrix/score_breakdown/rationale JSON
   and text fields. Missing evidence is never converted to a mismatch.
4. Reuse evaluated as the job's hold state, broadening its documented meaning to
   assessment recorded, awaiting automatic or human disposition. No new job enum
   value or transition edge is proposed. Insufficient-evidence outcomes bypass
   automatic threshold disposition, leaving the job evaluated.
5. Extend the read API with assessment_status and nullable score/recommendation.
   Check consumers and make only required compatibility changes to represent
   unresolved results; preserve the frozen visual design. Scored outcomes retain
   the existing numeric API representation and thresholds.
6. Keep evaluation records append-only. Re-evaluation produces a new row. Any
   eventual downgrade must refuse while unresolved rows exist, rather than invent
   values or delete audit records to restore NOT NULL constraints.

The user explicitly approved this proposal. Migration 0007 and the M5 implementation
now implement it. See M5_EVALUATION.md and MILESTONE_CHANGELOG.md for verification.
