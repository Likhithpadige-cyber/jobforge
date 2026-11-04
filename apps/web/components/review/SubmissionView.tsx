"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/components/AuthGuard";
import { EmptyState } from "@/components/EmptyState";
import { StatusBadge } from "@/components/StatusBadge";
import { applicationStatus } from "@/lib/application-view";
import { formatTimestamp } from "@/lib/resumes";
import {
  OUTCOME_HELP,
  OUTCOME_LABEL,
  gateLabel,
  outcomeTone,
  review,
  reviewError,
  type ReviewError,
  type SubmissionAttempt,
  type SubmissionReadiness,
} from "@/lib/review";
import { ConfirmDialog } from "./ConfirmDialog";
import { ReviewErrorState, ReviewLoading, SectionCard } from "./ReviewStates";

const SENDING_REFRESH_MS = 10_000;

/**
 * Readiness, the human-authorized Submit action and the attempt history.
 *
 * Every state here is read from the server, and it is read again after any
 * action. The Submit control exists only for the owner, is enabled only when
 * the server reports `can_submit` (which already requires live mode), and
 * asks for explicit confirmation. The server re-validates everything at
 * execution time regardless of what this page shows.
 */
export function SubmissionView({ applicationId }: { applicationId: string }) {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const [readiness, setReadiness] = useState<SubmissionReadiness | null>(null);
  const [history, setHistory] = useState<SubmissionAttempt[]>([]);
  const [error, setError] = useState<ReviewError | null>(null);
  const [loading, setLoading] = useState(true);
  const [confirming, setConfirming] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextReadiness, nextHistory] = await Promise.all([
        review.readiness(applicationId),
        review.submissions(applicationId),
      ]);
      setReadiness(nextReadiness);
      setHistory(nextHistory);
      setError(null);
    } catch (cause) {
      setError(reviewError(cause));
    } finally {
      setLoading(false);
    }
  }, [applicationId]);

  useEffect(() => {
    void load();
  }, [load]);

  // While the worker is sending, keep re-reading the outcome.
  const sending = readiness?.application_status === "submitting";
  useEffect(() => {
    if (!sending) return;
    const timer = window.setInterval(() => void load(), SENDING_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [sending, load]);

  if (loading) return <ReviewLoading label="Loading submission readiness" />;
  if (error && !readiness) return <ReviewErrorState error={error} onRetry={load} backHref="/applications" />;
  if (!readiness) return null;

  const status = applicationStatus(readiness.application_status);
  const passed = readiness.checks.filter((check) => check.passed).length;
  const gate = readiness.gate;

  return (
    <div className="space-y-5">
      <Link href={`/applications/${applicationId}`} className="inline-flex min-h-11 items-center text-sm font-semibold text-violet-deep hover:underline">← Application</Link>
      {notice && <p role="status" className="rounded-card border border-signal-strong/25 bg-signal-strong/10 px-4 py-3 text-sm text-ink">{notice}</p>}
      {error && <p role="alert" className="rounded-card border border-signal-review/25 bg-signal-review/10 px-4 py-3 text-sm text-ink">The latest refresh failed: {error.message} Showing the previous state.</p>}

      <section aria-label="Submission safety switch" data-testid="submission-gate" className={`rounded-[1.25rem] border p-5 sm:p-6 ${gate.live ? "border-signal-risk/30 bg-signal-risk/5" : "border-edge bg-muted"}`}>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge label={gate.submission_mode === "dry_run" ? "Dry-run mode" : "Live mode configured"} tone={gate.submission_mode === "dry_run" ? "neutral" : "risk"} />
          <StatusBadge label={gate.persisted_enabled ? "Submission switch on" : "Submission switch off"} tone={gate.persisted_enabled ? "risk" : "neutral"} />
        </div>
        <p className="mt-3 text-sm font-medium text-ink">{gateLabel(gate)}</p>
        <p className="mt-1 text-xs text-ink-soft">Real email requires both the deployment&apos;s live mode and the submission switch. Either one off means nothing is sent.</p>
      </section>

      <SectionCard
        title="Submission readiness"
        action={<button type="button" onClick={() => void load()} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-3 text-sm font-semibold text-violet-deep hover:border-violet/40">Refresh</button>}
      >
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge label={status.label} tone={status.tone} />
          <span className="text-sm text-ink-soft">{passed} of {readiness.checks.length} conditions met</span>
        </div>
        <ul className="mt-4 space-y-2" aria-label="Readiness conditions">
          {readiness.checks.map((check) => (
            <li key={check.key} className="flex items-start gap-3 text-sm">
              <span aria-hidden="true" className={`mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-bold text-white ${check.passed ? "bg-signal-strong" : "bg-signal-risk"}`}>{check.passed ? "✓" : "!"}</span>
              <span className="text-ink"><span className="sr-only">{check.passed ? "Met: " : "Not met: "}</span>{check.summary}</span>
            </li>
          ))}
        </ul>

        <div className="mt-6 border-t border-edge pt-5">
          {!isOwner && <p data-testid="reviewer-no-submit" className="text-sm text-ink-soft">Only the workspace owner can submit an application. You can review its materials and readiness.</p>}
          {isOwner && (
            <>
              <button type="button" onClick={() => setConfirming(true)} disabled={!readiness.can_submit} className="inline-flex min-h-11 items-center rounded-chip bg-signal-risk px-5 text-sm font-semibold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40">
                Submit application…
              </button>
              {!readiness.can_submit && (
                <p className="mt-2 text-sm text-ink-soft" data-testid="submit-unavailable">
                  {!gate.live ? gateLabel(gate) : "Submit becomes available when every condition above is met."}
                </p>
              )}
            </>
          )}
        </div>
      </SectionCard>

      <SectionCard title="Submission history">
        {history.length === 0 ? (
          <EmptyState title="No submission attempts yet" body="Attempts appear here after an owner submits. Each one shows its outcome; ambiguous outcomes are never retried automatically." />
        ) : (
          <ol className="space-y-3" aria-label="Submission attempts">
            {[...history].reverse().map((attempt) => (
              <li key={attempt.submission_id} className="rounded-card border border-edge p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="font-medium text-ink">Attempt {attempt.attempt} · {attempt.method} to {attempt.target_domain}</p>
                  <StatusBadge label={OUTCOME_LABEL[attempt.outcome]} tone={outcomeTone(attempt.outcome)} />
                </div>
                <dl className="mt-2 grid gap-1 text-xs text-ink-soft sm:grid-cols-3">
                  <div><dt className="inline">Started: </dt><dd className="inline">{attempt.started_at ? formatTimestamp(attempt.started_at) : "Not started"}</dd></div>
                  <div><dt className="inline">Finished: </dt><dd className="inline">{attempt.finished_at ? formatTimestamp(attempt.finished_at) : "—"}</dd></div>
                  <div><dt className="inline">Mail server code: </dt><dd className="inline">{attempt.smtp_status ?? "None"}</dd></div>
                </dl>
                {attempt.next_attempt_after && <p className="mt-1 text-xs text-ink-soft">Next automatic retry after {formatTimestamp(attempt.next_attempt_after)}</p>}
                {attempt.outcome === "pending" && (
                  <p className="mt-2 text-sm text-ink-soft">
                    {readiness.application_status === "submitting"
                      ? "Authorized by the owner; the submission worker is sending it."
                      : "Prepared when the materials were approved. Nothing is sent until the owner submits."}
                  </p>
                )}
                {OUTCOME_HELP[attempt.outcome] && <p className={`mt-2 text-sm ${attempt.outcome === "ambiguous" ? "font-medium text-signal-risk" : "text-ink-soft"}`}>{OUTCOME_HELP[attempt.outcome]}</p>}
              </li>
            ))}
          </ol>
        )}
      </SectionCard>

      {confirming && (
        <ConfirmDialog
          title={gate.live ? "Send this application now?" : "Submission is not available"}
          acknowledgement="I authorize JobForge to send this application now."
          confirmLabel="Send application"
          busyLabel="Requesting…"
          danger
          onCancel={() => setConfirming(false)}
          onConfirm={async () => {
            if (!gate.live) throw new Error("not live");
            const result = await review.submit(applicationId);
            setConfirming(false);
            setNotice(`Submission requested (attempt ${result.attempt}). The submission worker sends it; this page refreshes until the outcome is known.`);
            await load();
          }}
        >
          {gate.live ? (
            <>
              <p className="font-medium text-signal-risk">This sends a real email to the employer. It cannot be recalled.</p>
              <p>Destination: an authoritative address at <strong className="text-ink">{readiness.destination_domain ?? "the job's domain"}</strong>, taken only from the job&apos;s own record. Attempt {readiness.attempt ?? 1}.</p>
              <p>If the mail server&apos;s response is lost, the outcome is recorded as ambiguous and is never retried automatically.</p>
            </>
          ) : (
            <p>{gateLabel(gate)} Nothing will be sent.</p>
          )}
        </ConfirmDialog>
      )}
    </div>
  );
}
