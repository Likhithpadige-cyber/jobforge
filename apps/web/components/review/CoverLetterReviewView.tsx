"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { StatusBadge } from "@/components/StatusBadge";
import { VerdictBadge } from "@/components/VerdictBadge";
import type { VerificationStatus } from "@/lib/api";
import { VERIFICATION_LABEL, VERIFICATION_TONE, formatTimestamp } from "@/lib/resumes";
import {
  review,
  reviewError,
  type CoverLetterContent,
  type CoverLetterReview,
  type CoverLetterReviewStatus,
  type ReviewError,
} from "@/lib/review";
import { DecisionForm } from "./DecisionForm";
import { ReviewErrorState, ReviewLoading, SectionCard } from "./ReviewStates";

const REVIEW_LABEL: Record<CoverLetterReviewStatus, { label: string; tone: "review" | "strong" | "risk" | "neutral" }> = {
  not_started: { label: "Not generated yet", tone: "neutral" },
  in_review: { label: "Waiting for your review", tone: "review" },
  approved: { label: "Approved", tone: "strong" },
  changes_requested: { label: "Changes requested", tone: "review" },
  failed_verification: { label: "Failed verification", tone: "risk" },
};

export function CoverLetterReviewView({ applicationId }: { applicationId: string }) {
  const [state, setState] = useState<CoverLetterReview | null>(null);
  const [content, setContent] = useState<CoverLetterContent | null>(null);
  const [error, setError] = useState<ReviewError | null>(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [nextState, nextContent] = await Promise.all([
        review.coverLetterReview(applicationId),
        review.coverLetter(applicationId).catch((cause) => {
          // No cover letter yet is a state, not an error.
          if (reviewError(cause).kind === "not_found") return null;
          throw cause;
        }),
      ]);
      setState(nextState);
      setContent(nextContent);
    } catch (cause) {
      setError(reviewError(cause));
    } finally {
      setLoading(false);
    }
  }, [applicationId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) return <ReviewLoading label="Loading cover letter" />;
  if (error && !state) return <ReviewErrorState error={error} onRetry={load} backHref={`/applications/${applicationId}`} />;
  if (!state) return null;

  const status = REVIEW_LABEL[state.review_status] ?? { label: state.review_status, tone: "neutral" as const };
  const verification = state.verification_status as VerificationStatus | null;
  // The displayed text must be the version the decision applies to.
  const stale = content !== null && state.cover_letter_version !== null && content.version !== state.cover_letter_version;

  return (
    <div className="space-y-5">
      <Link href={`/applications/${applicationId}`} className="inline-flex min-h-11 items-center text-sm font-semibold text-accent hover:underline">← Application</Link>
      {notice && <p role="status" className="rounded-card border border-success bg-inset px-4 py-3 text-sm text-ink">{notice}</p>}
      {error && <p role="alert" className="rounded-card border border-attention bg-inset px-4 py-3 text-sm text-ink">{error.message}</p>}

      <header className="rounded-[1.25rem] border border-edge bg-surface p-5 shadow-raised sm:p-7">
        <p className="text-xs font-semibold uppercase tracking-[.12em] text-accent">Cover letter</p>
        <h2 className="mt-1 text-2xl font-semibold tracking-tight text-ink">
          {state.cover_letter_version ? `Version ${state.cover_letter_version}` : "No version yet"}
        </h2>
        <div className="mt-3 flex flex-wrap gap-2">
          <StatusBadge label={status.label} tone={status.tone} />
          {verification && VERIFICATION_LABEL[verification] && <StatusBadge label={VERIFICATION_LABEL[verification]} tone={VERIFICATION_TONE[verification]} />}
        </div>
        {state.changes_requested && (
          <div className="mt-4 rounded-card border border-edge bg-inset p-3 text-sm">
            <p className="text-xs font-semibold text-ink-soft">Changes requested{state.decided_by_email ? ` by ${state.decided_by_email}` : ""}</p>
            <p className="mt-1 whitespace-pre-wrap text-ink">{state.changes_requested}</p>
          </div>
        )}
        {state.decided_at && <p className="mt-3 text-xs text-ink-soft">Last decision {formatTimestamp(state.decided_at)}</p>}
      </header>

      <SectionCard title="Text and evidence">
        {!content && <EmptyState title="No cover letter yet" body="A cover letter is generated after application questions are prepared. If you requested changes, the new version appears here once it has been generated and verified." />}
        {stale && <p role="alert" className="mb-3 text-sm text-attention">A newer version exists than the one shown. Reload before deciding.</p>}
        {content && (
          <ol className="space-y-4">
            {content.claims.map((claim) => (
              <li key={claim.id} className="rounded-card border border-edge p-4">
                <p className="whitespace-pre-wrap text-sm leading-6 text-ink">{claim.claim_text}</p>
                <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-ink-soft">
                  <VerdictBadge verdict={claim.verdict} />
                  <span>{claim.fact_ids.length} cited fact{claim.fact_ids.length === 1 ? "" : "s"}</span>
                  {claim.verdict_reason && <span>· {claim.verdict_reason}</span>}
                </div>
              </li>
            ))}
          </ol>
        )}
      </SectionCard>

      <SectionCard title="Your decision">
        <DecisionForm
          subject="cover letter"
          canApprove={state.can_approve && !stale}
          canRequestChanges={state.can_request_changes && !stale}
          blockedReason={stale ? "Reload to see the latest version before deciding." : state.blocked_reason}
          approveAcknowledgement="I have read every paragraph and its cited evidence."
          changesEffect="JobForge records your note and generates a new version from your verified facts. It appears here after verification. If generation is paused for a safety incident, it waits until an owner resolves it."
          onApprove={async (note) => {
            setState(await review.approveCoverLetter(applicationId, note || undefined));
            await load();
            setNotice("Cover letter approved. The application's review state was re-read from the server.");
          }}
          onRequestChanges={async (note) => {
            setState(await review.requestCoverLetterChanges(applicationId, note));
            await load();
            setNotice("Changes requested. A new version will appear here once it is generated and verified.");
          }}
        />
      </SectionCard>
    </div>
  );
}
