"use client";

import Link from "next/link";
import { useCallback, useEffect, useId, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { StatusBadge } from "@/components/StatusBadge";
import type { ApplicationStatus } from "@/lib/api";
import {
  review,
  reviewError,
  type ApprovalInboxItem,
  type Question,
  type QuestionClass,
  type ReviewError,
} from "@/lib/review";
import { DecisionForm } from "./DecisionForm";
import { ReviewErrorState, ReviewLoading, SectionCard } from "./ReviewStates";

const CLASS_LABEL: Record<QuestionClass, { label: string; tone: "strong" | "review" | "risk" | "neutral" }> = {
  factual_safe: { label: "Answered from your facts", tone: "strong" },
  derived: { label: "Derived from your facts", tone: "strong" },
  uncertain: { label: "Needs your check", tone: "review" },
  judgement: { label: "Your judgement", tone: "review" },
  sensitive: { label: "Sensitive — never auto-answered", tone: "risk" },
  unanswerable: { label: "No grounded answer", tone: "neutral" },
};

export function AnswersPackageView({ applicationId }: { applicationId: string }) {
  const [questions, setQuestions] = useState<Question[] | null>(null);
  const [status, setStatus] = useState<ApplicationStatus | null>(null);
  const [decision, setDecision] = useState<ApprovalInboxItem | null>(null);
  const [error, setError] = useState<ReviewError | null>(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      // The answers approval has no pending stage: it is opened and decided
      // in one step while the application awaits approval (M9 service rule).
      const [nextQuestions, readiness, approved, changes] = await Promise.all([
        review.questions(applicationId),
        review.readiness(applicationId),
        review.approvals({ application_id: applicationId, kind: "answers", status: "approved" }),
        review.approvals({ application_id: applicationId, kind: "answers", status: "changes_requested" }),
      ]);
      setQuestions([...nextQuestions].sort((a, b) => a.display_order - b.display_order));
      setStatus(readiness.application_status);
      const latest = [...approved, ...changes].sort((a, b) => Date.parse(b.decided_at ?? b.requested_at) - Date.parse(a.decided_at ?? a.requested_at));
      setDecision(latest[0] ?? null);
    } catch (cause) {
      setError(reviewError(cause));
    } finally {
      setLoading(false);
    }
  }, [applicationId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) return <ReviewLoading label="Loading answers" />;
  if (error && !questions) return <ReviewErrorState error={error} onRetry={load} backHref={`/applications/${applicationId}`} />;
  if (!questions) return null;

  const needsHuman = questions.filter((question) => question.answer?.requires_human && !question.answer.value).length;
  const approved = decision?.status === "approved";
  const open = status === "awaiting_approval" && questions.length > 0 && !approved;
  return (
    <div className="space-y-5">
      <Link href={`/applications/${applicationId}`} className="inline-flex min-h-11 items-center text-sm font-semibold text-violet-deep hover:underline">← Application</Link>
      {notice && <p role="status" className="rounded-card border border-signal-strong/25 bg-signal-strong/10 px-4 py-3 text-sm text-ink">{notice}</p>}
      {error && <p role="alert" className="rounded-card border border-signal-review/25 bg-signal-review/10 px-4 py-3 text-sm text-ink">{error.message}</p>}

      <header className="rounded-[1.25rem] border border-edge bg-surface p-5 shadow-raised sm:p-7">
        <p className="text-xs font-semibold uppercase tracking-[.12em] text-violet-deep">Application answers</p>
        <h2 className="mt-1 text-2xl font-semibold tracking-tight text-ink">{questions.length} question{questions.length === 1 ? "" : "s"}</h2>
        <div className="mt-3 flex flex-wrap gap-2">
          <StatusBadge label={approved ? "Approved" : open ? "Waiting for your review" : decision?.status === "changes_requested" ? "Changes requested" : "Not open for review"} tone={approved ? "strong" : open ? "review" : "neutral"} />
          {needsHuman > 0 && <StatusBadge label={`${needsHuman} need your answer`} tone="review" />}
        </div>
      </header>

      <SectionCard title="Questions">
        {questions.length === 0 ? (
          <EmptyState title="No questions for this application" body="Some applications ask nothing beyond the resume and cover letter. Nothing needs answering here." />
        ) : (
          <ol className="space-y-4">
            {questions.map((question) => (
              <QuestionItem key={question.id} applicationId={applicationId} question={question} editable={open} onSaved={async () => { await load(); setNotice("Answer saved."); }} />
            ))}
          </ol>
        )}
      </SectionCard>

      <SectionCard title="Your decision">
        <DecisionForm
          subject="answers package"
          canApprove={open}
          canRequestChanges={open}
          blockedReason={approved ? "These answers are approved." : questions.length === 0 ? "This application has no questions to answer." : "Answers can be reviewed while the application is awaiting approval."}
          approveAcknowledgement={needsHuman > 0 ? `I have checked every answer. (${needsHuman} still need an answer; approval will be refused until they have one.)` : "I have checked every answer, including any I wrote myself."}
          changesEffect="JobForge records your note and returns the package for rework. Nothing is submitted."
          onApprove={async (note) => {
            await review.approvePackage(applicationId, note || undefined);
            await load();
            setNotice("Answers approved. The review state was re-read from the server.");
          }}
          onRequestChanges={async (note) => {
            await review.requestPackageChanges(applicationId, note);
            await load();
            setNotice("Changes requested on the answers package.");
          }}
        />
      </SectionCard>
    </div>
  );
}

function QuestionItem({ applicationId, question, editable, onSaved }: { applicationId: string; question: Question; editable: boolean; onSaved: () => Promise<void> }) {
  const fieldId = useId();
  const answer = question.answer;
  const [value, setValue] = useState(answer?.value ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const kind = CLASS_LABEL[question.question_class];
  const dirty = value !== (answer?.value ?? "");

  async function save() {
    if (!answer || saving) return;
    setSaving(true);
    setError(null);
    try {
      await review.editAnswer(applicationId, answer.id, value.trim() ? value : null);
      await onSaved();
    } catch (cause) {
      setError(reviewError(cause).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <li className="rounded-card border border-edge p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <label htmlFor={fieldId} className="font-medium text-ink">
          {question.question_text}
          {question.is_required && <span className="ml-1 text-signal-risk" aria-label="required">*</span>}
        </label>
        <StatusBadge label={kind.label} tone={kind.tone} />
      </div>
      {answer ? (
        <>
          <textarea id={fieldId} value={value} aria-required={question.is_required || undefined} disabled={!editable || saving} maxLength={question.max_length ?? 4000} onChange={(event) => setValue(event.target.value)} rows={3} className="mt-3 w-full rounded-chip border border-edge bg-surface px-3 py-2 text-sm text-ink disabled:bg-muted" />
          <p className="mt-1 text-xs text-ink-soft">
            {answer.source === "human" ? "Written by a person" : answer.source === "agent" ? `Drafted from ${answer.fact_ids.length} verified fact${answer.fact_ids.length === 1 ? "" : "s"}` : answer.source === "derived" ? "Derived from your facts" : "Left blank"}
            {answer.requires_human && " · needs your answer"}
          </p>
          {editable && dirty && (
            <button type="button" onClick={save} disabled={saving} className="mt-2 inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-violet-deep hover:border-violet/40 disabled:opacity-50">
              {saving ? "Saving…" : "Save answer"}
            </button>
          )}
          {error && <p role="alert" className="mt-2 text-sm text-signal-risk">{error}</p>}
        </>
      ) : (
        <p className="mt-2 text-sm text-ink-soft">No answer has been prepared for this question.</p>
      )}
    </li>
  );
}
