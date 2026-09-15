"use client";

/**
 * Resume Studio, the read-only view.
 *
 * Everything rendered here comes from one API response describing what B6
 * persisted. Nothing is generated, scored or inferred in the browser.
 *
 * A failed resume renders exactly like a passing one, with its text intact and
 * its problem claims marked. Hiding failed text would defeat the reason B6
 * persists it: a human has to read the sentence that failed.
 */

import { useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { ResumeReviewPanel } from "@/components/ResumeReviewPanel";
import { SectionHeader } from "@/components/SectionHeader";
import { StatusBadge } from "@/components/StatusBadge";
import { TailoringSnapshot } from "@/components/TailoringSnapshot";
import { VerdictBadge } from "@/components/VerdictBadge";
import type { ApiClaim, ApiResume, ApiResumeReview } from "@/lib/api";
import {
  VERIFICATION_LABEL,
  VERIFICATION_TONE,
  applicationLabel,
  applicationTone,
  formatTimestamp,
  groupSections,
  jobLine,
  reviewSummary,
} from "@/lib/resumes";

function ClaimDetails({ claim }: { claim: ApiClaim }) {
  return (
    <dl className="mt-3 space-y-2 rounded-card border border-edge bg-muted/40 p-3 text-xs">
      <div>
        <dt className="font-medium text-ink">Verdict</dt>
        <dd className="mt-0.5 text-ink-soft">
          {claim.verdict_reason ?? "No reason recorded."}
        </dd>
      </div>
      <div>
        <dt className="font-medium text-ink">Cited facts</dt>
        <dd className="mt-0.5 break-all font-mono text-[11px] text-ink-soft">
          {claim.fact_ids.length > 0 ? claim.fact_ids.join(", ") : "None"}
        </dd>
      </div>
      {claim.numerals_used.length > 0 && (
        <div>
          <dt className="font-medium text-ink">Numerals used</dt>
          <dd className="mt-0.5 text-ink-soft">{claim.numerals_used.join(", ")}</dd>
        </div>
      )}
      <div>
        <dt className="font-medium text-ink">Verified</dt>
        <dd className="mt-0.5 text-ink-soft">{formatTimestamp(claim.verified_at)}</dd>
      </div>
    </dl>
  );
}

function ClaimLine({ claim }: { claim: ApiClaim }) {
  const [open, setOpen] = useState(false);
  const failing = claim.verdict === "overreach" || claim.verdict === "unsupported";
  return (
    <li
      className={`rounded-card border px-4 py-3 ${
        failing ? "border-signal-risk/30 bg-signal-risk/[0.04]" : "border-transparent"
      }`}
    >
      <div className="flex items-start justify-between gap-4">
        <p className="text-sm leading-relaxed text-ink">{claim.claim_text}</p>
        <VerdictBadge verdict={claim.verdict} className="shrink-0" />
      </div>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="mt-2 text-xs font-medium text-ink-soft underline-offset-2 hover:text-ink hover:underline"
      >
        {open ? "Hide evidence" : "Show evidence"}
      </button>
      {open && <ClaimDetails claim={claim} />}
    </li>
  );
}

export function ResumeStudioView({
  resume,
  onReviewed,
}: {
  resume: ApiResume;
  /**
   * Called with the review state the API returned after a decision.
   *
   * The page owns the resume, so it re-renders from the server's answer
   * rather than from an optimistic guess made here. An approval that the
   * server refused must never look like one that landed.
   */
  onReviewed?: (next: ApiResumeReview) => void;
}) {
  const sections = groupSections(resume.claims);
  const review = reviewSummary(resume);

  return (
    <div className="space-y-6">
      <header
        data-testid="studio-header"
        className="rounded-card border border-edge bg-surface p-5"
      >
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-lg font-semibold text-ink">{jobLine(resume)}</h1>
            <p className="mt-1 text-sm text-ink-soft">
              Version {resume.version} · Generated {formatTimestamp(resume.created_at)}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <StatusBadge
              label={VERIFICATION_LABEL[resume.verification_status]}
              tone={VERIFICATION_TONE[resume.verification_status]}
            />
            <StatusBadge
              label={applicationLabel(resume.application_status)}
              tone={applicationTone(resume.application_status)}
            />
          </div>
        </div>
        {review && (
          <p className="mt-3 rounded-card border border-signal-risk/25 bg-signal-risk/[0.06] px-3 py-2 text-sm text-signal-risk">
            {review}. The text below is shown in full so you can see exactly what
            failed and why.
          </p>
        )}
      </header>

      <div className="grid gap-6 lg:grid-cols-[1fr_18rem]">
        <div className="space-y-6">
          {sections.length === 0 ? (
            <EmptyState
              title="No claims recorded"
              body="This resume was persisted without any traceable claims."
            />
          ) : (
            sections.map((section) => (
              <section
                key={section.key}
                aria-labelledby={`section-${section.key}`}
                className="rounded-card border border-edge bg-surface p-5"
              >
                <SectionHeader
                  id={`section-${section.key}`}
                  title={section.label}
                  description={`${section.claims.length} ${
                    section.claims.length === 1 ? "claim" : "claims"
                  }`}
                />
                <ul className="mt-3 space-y-2">
                  {section.claims.map((claim) => (
                    <ClaimLine key={claim.id} claim={claim} />
                  ))}
                </ul>
              </section>
            ))
          )}
        </div>

        <aside className="space-y-4">
          <TailoringSnapshot resume={resume} />
          {resume.review && (
            <ResumeReviewPanel
              review={resume.review}
              onReviewed={(next) => onReviewed?.(next)}
            />
          )}
          <div className="rounded-card border border-edge bg-surface p-5">
            <h2 className="text-sm font-semibold text-ink">Next steps</h2>
            <p className="mt-1 text-xs text-ink-soft">
              Editing and export arrive in a later phase. Review and approval
              are live in the panel above.
            </p>
            <div className="mt-3 space-y-2">
              {["Edit resume", "Export PDF"].map((label) => (
                <button
                  key={label}
                  type="button"
                  disabled
                  className="w-full cursor-not-allowed rounded-chip border border-edge bg-muted px-3 py-2 text-xs font-medium text-ink-soft opacity-60"
                >
                  {label} · Coming next
                </button>
              ))}
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
