"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/components/AuthGuard";
import { StatusBadge } from "@/components/StatusBadge";
import { api, type ApiResume, type ApiResumeReview } from "@/lib/api";
import { applicationStatus } from "@/lib/application-view";
import { formatTimestamp } from "@/lib/resumes";
import {
  APPROVAL_KIND_LABEL,
  OUTCOME_LABEL,
  approvalHref,
  gateLabel,
  outcomeTone,
  review,
  reviewError,
  type ApprovalInboxItem,
  type CoverLetterReview,
  type Question,
  type ReviewError,
  type SubmissionAttempt,
  type SubmissionReadiness,
} from "@/lib/review";
import { ReviewErrorState, ReviewLoading, SectionCard } from "./ReviewStates";
import { PrepareQuestionsPanel } from "@/components/review/PrepareQuestionsPanel";
import { TrackingPanel } from "@/components/tracking/TrackingPanel";

interface Snapshot {
  readiness: SubmissionReadiness;
  resume: ApiResume | null;
  resumeReview: ApiResumeReview | null;
  coverLetter: CoverLetterReview | null;
  questions: Question[] | null;
  pending: ApprovalInboxItem[];
  answersApproved: boolean;
  history: SubmissionAttempt[];
  /** Sections whose data could not be read; shown as unavailable, not absent. */
  unavailable?: Section[];
}

type Section = "resume" | "cover_letter" | "answers" | "decisions" | "history";
const UNAVAILABLE = Symbol("unavailable");
type Maybe<T> = T | null | typeof UNAVAILABLE;

/**
 * "Not created yet" and "not available in this state" are states here; any
 * other failure marks only that section unavailable, so one slow request
 * does not take the whole workspace down.
 */
async function optional<T>(call: Promise<T>): Promise<Maybe<T>> {
  try {
    return await call;
  } catch (cause) {
    const kind = reviewError(cause).kind;
    if (kind === "not_found" || kind === "conflict") return null;
    return UNAVAILABLE;
  }
}

async function section<T>(call: Promise<T>): Promise<T | typeof UNAVAILABLE> {
  try {
    return await call;
  } catch {
    return UNAVAILABLE;
  }
}

type Step = { title: string; body: string; href?: string; cta?: string };

export function nextStep(snapshot: Snapshot, isOwner: boolean): Step {
  const { readiness, pending } = snapshot;
  const id = readiness.application_id;
  const waiting = (kind: string) => pending.find((item) => item.kind === kind);
  const missing = snapshot.unavailable ?? [];
  // Never say "nothing to decide" when the decisions could not be read.
  if (missing.includes("decisions") || (missing.includes("answers") && readiness.application_status === "awaiting_approval")) {
    return { title: "Some review information could not be loaded", body: "Retry to see what is waiting for your decision." };
  }
  for (const kind of ["resume", "cover_letter"] as const) {
    const item = waiting(kind);
    if (item) {
      return {
        title: `${APPROVAL_KIND_LABEL[kind]} is waiting for your review`,
        body: "Read it against the cited evidence, then approve it or request changes.",
        href: approvalHref(item),
        cta: `Review ${APPROVAL_KIND_LABEL[kind].toLowerCase()}`,
      };
    }
  }
  // Answers have no pending approval row: they are decided directly.
  if (readiness.application_status === "awaiting_approval" && snapshot.questions?.length && !snapshot.answersApproved) {
    return {
      title: "Application answers are waiting for your review",
      body: "Check every answer, fill in the ones only you can give, then approve the package.",
      href: `/applications/${id}/answers`,
      cta: "Review answers",
    };
  }
  switch (readiness.application_status) {
    case "submission_ready":
      if (readiness.can_submit) {
        return { title: "Ready to submit", body: "Every condition is met. Submitting sends a real email after you confirm.", href: `/applications/${id}/submission`, cta: "Open submission" };
      }
      return isOwner
        ? { title: "Submission is blocked", body: readiness.gate.live ? "Some readiness conditions are not met." : gateLabel(readiness.gate), href: `/applications/${id}/submission`, cta: "See readiness" }
        : { title: "Waiting for the owner", body: "All materials are approved. Only the workspace owner can submit.", href: `/applications/${id}/submission`, cta: "See readiness" };
    case "submitting":
      return { title: "Sending", body: "The submission worker is sending this application.", href: `/applications/${id}/submission`, cta: "See progress" };
    case "submitted":
      return { title: "Applied", body: "The application was sent.", href: `/applications/${id}/submission`, cta: "See history" };
    case "submission_failed":
      return { title: "Submission did not complete", body: "Check the attempt history for what happened. Ambiguous outcomes are never retried automatically.", href: `/applications/${id}/submission`, cta: "See history" };
    default:
      return { title: applicationStatus(readiness.application_status).label, body: "There is nothing for you to decide right now." };
  }
}

export function ApplicationWorkspace({ applicationId }: { applicationId: string }) {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<ReviewError | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setError(null);
    try {
      // Readiness is required (it also proves the application exists); the
      // rest may legitimately be absent for an application at an early step.
      const [readiness, resume, resumeReview, coverLetter, questions, pending, answersDecided, history] = await Promise.all([
        review.readiness(applicationId),
        optional(api.latestResume(applicationId)),
        optional(api.resumeReview(applicationId)),
        optional(review.coverLetterReview(applicationId)),
        optional(review.questions(applicationId)),
        section(review.approvals({ application_id: applicationId, status: "pending" })),
        section(review.approvals({ application_id: applicationId, kind: "answers", status: "approved" })),
        section(review.submissions(applicationId)),
      ]);
      const unavailable: Section[] = [];
      const take = <T,>(value: T | typeof UNAVAILABLE, name: Section, fallback: T): T => {
        if (value !== UNAVAILABLE) return value;
        if (!unavailable.includes(name)) unavailable.push(name);
        return fallback;
      };
      setSnapshot({
        readiness,
        resume: take(resume, "resume", null),
        resumeReview: take(resumeReview, "resume", null),
        coverLetter: take(coverLetter, "cover_letter", null),
        questions: take(questions, "answers", null),
        pending: take(pending, "decisions", []),
        answersApproved: take(answersDecided, "answers", []).length > 0,
        history: take(history, "history", []),
        unavailable,
      });
    } catch (cause) {
      setError(reviewError(cause));
    } finally {
      setLoading(false);
    }
  }, [applicationId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) return <ReviewLoading label="Loading application" />;
  if (error && !snapshot) return <ReviewErrorState error={error} onRetry={load} backHref="/applications" />;
  if (!snapshot) return null;

  const { readiness, resume, resumeReview, coverLetter, questions, pending, history } = snapshot;
  const status = applicationStatus(readiness.application_status);
  const step = nextStep(snapshot, isOwner);
  const latest = history[history.length - 1];
  const passed = readiness.checks.filter((check) => check.passed).length;
  const unavailable = snapshot.unavailable ?? [];
  const failed = (name: Section) => unavailable.includes(name);
  const needsHuman = questions?.filter((question) => question.answer?.requires_human && !question.answer.value).length ?? 0;

  return (
    <div className="space-y-6">
      <nav aria-label="Application location" className="flex flex-wrap items-center gap-2 text-sm text-ink-soft">
        <Link href="/applications" className="inline-flex min-h-6 items-center hover:text-accent">Applications</Link>
        <span aria-hidden="true">›</span>
        <span className="text-ink">{resume?.job_title ?? "Application"}</span>
      </nav>

      {unavailable.length > 0 && (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-card border border-attention bg-inset px-4 py-3 text-sm text-ink">
          <span>Part of this application could not be loaded. The sections marked unavailable may be out of date.</span>
          <button type="button" onClick={() => void load()} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 font-semibold text-accent hover:border-control">Retry</button>
        </div>
      )}

      <header className="rounded-[1.5rem] border border-edge bg-surface p-6 shadow-raised sm:p-8">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase tracking-[.14em] text-accent">{resume?.job_company ?? "Company unavailable"}</p>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight text-ink sm:text-3xl">{resume?.job_title ?? "Untitled role"}</h2>
          </div>
          <StatusBadge label={status.label} tone={status.tone} />
        </div>
        <div className="mt-6 rounded-card border border-edge bg-surface p-5" data-testid="next-step">
          <p className="text-xs font-semibold uppercase tracking-[.12em] text-accent">Next step</p>
          <p className="mt-2 text-lg font-semibold text-ink">{step.title}</p>
          <p className="mt-1 text-sm text-ink-soft">{step.body}</p>
          {step.href && step.cta && (
            <Link href={step.href} className="mt-4 inline-flex min-h-11 items-center rounded-chip bg-action px-5 text-sm font-semibold text-action-ink hover:bg-action-hover">{step.cta} <span aria-hidden="true" className="ml-2">→</span></Link>
          )}
        </div>
      </header>

      <div className="grid gap-5 lg:grid-cols-2">
        <SectionCard title="Resume" action={resume && <Link href={`/resume-studio/${applicationId}`} className="inline-flex min-h-6 items-center text-sm font-semibold text-accent hover:underline">Open</Link>}>
          {failed("resume") ? (
            <Unavailable />
          ) : resume ? (
            <p className="text-sm text-ink">Version {resume.version} · {resumeReview ? reviewLabel(resumeReview.review_status) : "Review not started"}</p>
          ) : (
            <p className="text-sm text-ink-soft">No tailored resume yet.</p>
          )}
        </SectionCard>

        <SectionCard title="Cover letter" action={coverLetter?.cover_letter_version && <Link href={`/applications/${applicationId}/cover-letter`} className="inline-flex min-h-6 items-center text-sm font-semibold text-accent hover:underline">Open</Link>}>
          {failed("cover_letter") ? (
            <Unavailable />
          ) : coverLetter?.cover_letter_version ? (
            <p className="text-sm text-ink">Version {coverLetter.cover_letter_version} · {reviewLabel(coverLetter.review_status)}</p>
          ) : (
            <p className="text-sm text-ink-soft">No cover letter yet.</p>
          )}
        </SectionCard>

        <SectionCard title="Application answers" action={questions && <Link href={`/applications/${applicationId}/answers`} className="inline-flex min-h-6 items-center text-sm font-semibold text-accent hover:underline">Open</Link>}>
          {failed("answers") ? (
            <Unavailable />
          ) : questions && (questions.length > 0 || readiness.application_status !== "resume_ready") ? (
            <p className="text-sm text-ink">{questions.length} question{questions.length === 1 ? "" : "s"}{needsHuman > 0 ? ` · ${needsHuman} need your answer` : ""}{snapshot.answersApproved ? " · approved" : readiness.application_status === "awaiting_approval" && questions.length > 0 ? " · waiting for review" : ""}</p>
          ) : (
            <p className="text-sm text-ink-soft">Questions have not been prepared yet.</p>
          )}
          {isOwner && readiness.application_status === "resume_ready" && (
            <PrepareQuestionsPanel applicationId={applicationId} onPrepared={() => void load()} />
          )}
        </SectionCard>

        <SectionCard title="Submission" action={<Link href={`/applications/${applicationId}/submission`} className="inline-flex min-h-6 items-center text-sm font-semibold text-accent hover:underline">Open</Link>}>
          <p className="text-sm text-ink">{passed} of {readiness.checks.length} readiness conditions met</p>
          <p className="mt-1 text-sm text-ink-soft">{gateLabel(readiness.gate)}</p>
          {failed("history") ? (
            <p className="mt-2 text-sm text-ink-soft">Attempt history is unavailable right now.</p>
          ) : latest && (
            <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
              <span className="text-ink-soft">Latest attempt {latest.attempt}:</span>
              <StatusBadge label={OUTCOME_LABEL[latest.outcome]} tone={outcomeTone(latest.outcome)} />
            </div>
          )}
        </SectionCard>
      </div>

      <SectionCard title="Tracking">
        <TrackingPanel applicationId={applicationId} />
      </SectionCard>

      <SectionCard title="Waiting for a decision">
        {failed("decisions") ? (
          <Unavailable />
        ) : pending.length === 0 ? (
          <p className="text-sm text-ink-soft">Nothing on this application is waiting for a decision.</p>
        ) : (
          <ul className="space-y-2">
            {pending.map((item) => (
              <li key={item.approval_id}>
                <Link href={approvalHref(item)} className="flex min-h-11 flex-wrap items-center justify-between gap-2 rounded-chip border border-edge px-3 py-2 text-sm hover:border-control">
                  <span className="font-medium text-ink">{APPROVAL_KIND_LABEL[item.kind]}</span>
                  <span className="text-ink-soft">waiting since {formatTimestamp(item.requested_at)} →</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </SectionCard>
    </div>
  );
}

function Unavailable() {
  return <p className="text-sm text-danger">Unavailable right now — retry to load it.</p>;
}

function reviewLabel(status: string): string {
  const labels: Record<string, string> = {
    not_started: "Review not started",
    in_review: "Waiting for review",
    approved: "Approved",
    changes_requested: "Changes requested",
    failed_verification: "Failed verification",
  };
  return labels[status] ?? status;
}
