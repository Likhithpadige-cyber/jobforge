"use client";

/**
 * The job's stored fit evaluation (GET /jobs/{id}/evaluation).
 *
 * Deterministic: the score is arithmetic over the match between the posting's
 * requirements and the owner's verified facts, computed by fixed rules - no
 * model call. Shown exactly as stored; nothing is estimated when there is no
 * evaluation or not enough evidence.
 */

import { useEffect, useState } from "react";
import { FitScore } from "@/components/FitScore";
import { ProvenanceTag } from "@/components/ProvenanceTag";
import { ApiError, api, type ApiEvaluation } from "@/lib/api";

const RECOMMENDATION: Record<NonNullable<ApiEvaluation["recommendation"]>, string> = {
  strong: "Strong fit",
  moderate: "Moderate fit",
  weak: "Weak fit",
  no: "Not a fit",
};

type State = { kind: "loading" } | { kind: "none" } | { kind: "error" } | { kind: "ok"; evaluation: ApiEvaluation };

export function FitScoreCard({ jobId }: { jobId: string }) {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    api
      .jobEvaluation(jobId)
      .then((evaluation) => !cancelled && setState({ kind: "ok", evaluation }))
      .catch((cause) => {
        if (!cancelled) setState({ kind: cause instanceof ApiError && cause.status === 404 ? "none" : "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  return (
    <section aria-labelledby="fit-heading" className="rounded-card border border-edge bg-surface p-5 shadow-raised">
      <h2 id="fit-heading" className="text-sm font-medium text-ink">Fit with your profile</h2>
      {state.kind === "loading" && <p className="mt-3 text-sm text-ink-soft">Loading the evaluation…</p>}
      {state.kind === "error" && <p role="alert" className="mt-3 text-sm text-danger">The evaluation is unavailable right now.</p>}
      {state.kind === "none" && (
        <p className="mt-3 text-sm text-ink-soft">This job has not been evaluated yet. The evaluator scores new jobs in the background; nothing is estimated here in the meantime.</p>
      )}
      {state.kind === "ok" && <Evaluation evaluation={state.evaluation} />}
    </section>
  );
}

function Evaluation({ evaluation }: { evaluation: ApiEvaluation }) {
  if (evaluation.assessment_status === "insufficient_evidence" || evaluation.score === null) {
    const reasons = evaluation.score_breakdown.insufficient_reasons ?? [];
    return (
      <div className="mt-3 space-y-2 text-sm">
        <p className="font-medium text-ink" data-testid="fit-status">Not enough verified evidence to score this job</p>
        {reasons.length > 0 && (
          <ul className="list-disc space-y-1 pl-5 text-ink-soft">
            {reasons.map((r) => <li key={r}>{r}</li>)}
          </ul>
        )}
        <p className="text-xs text-ink-soft">Verify more facts in Profile &amp; Facts; no score is shown rather than a guess.</p>
      </div>
    );
  }
  const matched = evaluation.matched_requirements.count ?? 0;
  const gaps = evaluation.gaps ?? [];
  return (
    <div className="mt-3 space-y-3 text-sm">
      <div className="flex items-baseline justify-between gap-3">
        <div data-testid="fit-score" role="img" aria-label={`Fit score ${Math.round(evaluation.score)} out of 100`}>
          {/* Existing visual; its own band label is off because the stored
              recommendation below is the authority (thresholds may differ). */}
          <FitScore score={evaluation.score} showLabel={false} />
        </div>
        {evaluation.recommendation && <span className="rounded-chip bg-inset px-2 py-1 text-xs font-semibold text-ink">{RECOMMENDATION[evaluation.recommendation]}</span>}
      </div>
      <p className="text-ink-soft">{matched} requirement{matched === 1 ? "" : "s"} met or partly met by verified facts.</p>
      {gaps.length > 0 && (
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-ink-soft">Gaps</p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-ink-soft">
            {gaps.slice(0, 5).map((g) => <li key={g}>{g}</li>)}
          </ul>
          {gaps.length > 5 && <p className="mt-1 text-xs text-ink-soft">and {gaps.length - 5} more</p>}
        </div>
      )}
      <p className="flex items-center gap-2 border-t border-edge pt-3 text-xs text-ink-soft">
        <ProvenanceTag kind="deterministic" />
        Calculated by fixed rules from your verified facts. No AI was used.
      </p>
    </div>
  );
}
