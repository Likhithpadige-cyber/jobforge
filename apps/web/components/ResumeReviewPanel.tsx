"use client";

/**
 * Resume review and approval (C2).
 *
 * The panel renders one thing: the review state the API returned. Which
 * actions appear is decided by the server's `can_start_review`, `can_approve`
 * and `can_request_changes` booleans, never re-derived here from the
 * application status. Two copies of that rule would eventually disagree, and
 * the copy that matters is the one the endpoint enforces.
 *
 * Approving is a claim a human makes about text they have read, so nothing
 * here is one click away: approve asks for confirmation, and requesting
 * changes will not submit without a note. Neither is friction for its own
 * sake — an approval with no reader and a rework request with no instructions
 * are both worse than no action at all.
 */

import { useState } from "react";
import { useAuth } from "@/components/AuthGuard";
import { StatusBadge } from "@/components/StatusBadge";
import { ApiError, api, type ApiExportPreview, type ApiResumeReview } from "@/lib/api";
import {
  REVIEW_LABEL,
  REVIEW_TONE,
  decisionLine,
  isValidChangeNote,
} from "@/lib/resumes";

type Pending = "start" | "approve" | "changes" | null;

const BUTTON =
  "rounded-chip border px-3 py-2 text-xs font-medium transition disabled:cursor-not-allowed ";
const PRIMARY = `${BUTTON} border-edge bg-inset text-attention hover:bg-inset`;
const SECONDARY = `${BUTTON} border-edge bg-surface text-ink hover:bg-inset`;

export function ResumeReviewPanel({
  review,
  onReviewed,
}: {
  review: ApiResumeReview;
  /** Hand the new state back so the page re-renders from the server's answer. */
  onReviewed: (next: ApiResumeReview) => void;
}) {
  const { user } = useAuth();
  const reviewer = user?.role === "reviewer";
  const [pending, setPending] = useState<Pending>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [changesOpen, setChangesOpen] = useState(false);
  const [note, setNote] = useState("");
  const [approvalNote, setApprovalNote] = useState("");

  const [exportPreview, setExportPreview] = useState<ApiExportPreview | null>(null);
  const [exportConsent, setExportConsent] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  async function openApproval() {
    setConfirming(true);
    setExportPreview(null);
    setExportConsent(false);
    setPreviewLoading(!reviewer);
    setError(null);
    if (reviewer) return;
    try { setExportPreview(await api.exportPreview(review.application_id)); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setPreviewLoading(false); }
  }
  const busy = pending !== null;
  const decided = decisionLine(review);

  async function run(
    kind: Exclude<Pending, null>,
    action: () => Promise<ApiResumeReview>,
  ) {
    setPending(kind);
    setError(null);
    try {
      const next = await action();
      onReviewed(next);
      setConfirming(false);
      setChangesOpen(false);
      setNote("");
      setApprovalNote("");
    } catch (cause) {
      // A 409 here is normal, not a crash: someone else moved the
      // application, or a newer resume version landed while this page was
      // open. The server's own message says which, so it is shown as written
      // — and `onReviewed` is deliberately not called, so a refusal can never
      // look like a decision that landed.
      setError(errorMessage(cause));
    } finally {
      setPending(null);
    }
  }

  return (
    <section
      aria-labelledby="review-heading"
      data-testid="review-panel"
      className="rounded-card border border-edge bg-surface p-5 shadow-raised"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="review-heading" className="text-sm font-semibold text-ink">
          Review
        </h2>
        <StatusBadge
          label={REVIEW_LABEL[review.review_status]}
          tone={REVIEW_TONE[review.review_status]}
        />
      </div>

      <p className="mt-1 text-xs text-ink-soft">
        Version {review.resume_version} of this resume.
      </p>

      {decided && (
        <p className="mt-3 text-xs text-ink-soft" data-testid="decision-line">
          {decided}
        </p>
      )}

      {review.changes_requested && (
        <blockquote
          data-testid="changes-note"
          className="mt-3 rounded-card border border-attention bg-inset px-3 py-2 text-sm text-ink"
        >
          {review.changes_requested}
        </blockquote>
      )}

      {review.review_status === "approved" && !review.changes_requested &&
        review.decision_note && (
          <blockquote
            data-testid="decision-note"
            className="mt-3 rounded-card border border-edge bg-inset px-3 py-2 text-sm text-ink"
          >
            {review.decision_note}
          </blockquote>
        )}

      {error && (
        <p role="alert" className="mt-3 text-xs text-danger">
          {error}
        </p>
      )}

      {/* --- resume_ready: nothing has been submitted for review yet ------- */}
      {review.can_start_review && !confirming && (
        <div className="mt-4 space-y-2">
          <p className="text-xs text-ink-soft">
            Ready for review. Every claim is supported and verification passed.
          </p>
          <button
            type="button"
            className={PRIMARY}
            disabled={busy}
            onClick={() =>
              void run("start", () =>
                api.startResumeReview(review.application_id),
              )
            }
          >
            {pending === "start" ? "Starting review…" : "Start Review"}
          </button>
        </div>
      )}

      {/* --- awaiting_approval: the decision itself ------------------------ */}
      {(review.can_approve || (!reviewer && review.export_consent_required)) && !changesOpen && !confirming && (
        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="button"
            className={PRIMARY}
            disabled={busy}
            onClick={() => void openApproval()}
          >
            {review.export_consent_required ? "Review Export Details" : "Approve Resume"}
          </button>
          <button
            type="button"
            className={SECONDARY}
            disabled={busy || !review.can_request_changes}
            onClick={() => setChangesOpen(true)}
          >
            Request Changes
          </button>
        </div>
      )}

      {confirming && (
        <div
          data-testid="approve-confirm"
          className="mt-4 rounded-card border border-edge bg-inset p-3"
        >
          <p className="text-sm text-ink">
            Approve version {review.resume_version}? This records your name
            against the resume as it stands.
          </p>
          {previewLoading && <p role="status">Loading export details…</p>}
          {reviewer && <p className="mt-3 text-sm">Approve the resume text only. The owner can separately approve contact details for export.</p>}
          {exportPreview && <div className="mt-3 space-y-3" aria-label="Exact resume export details">
            {exportPreview.rendered_lines && <div aria-label="Formatted export preview" className="whitespace-pre-wrap break-words">{exportPreview.rendered_lines.map((line, index) => <p key={index}>{line}</p>)}</div>}
            <h3 className="font-semibold">Contact details in the resume body</h3>
            {Object.entries(exportPreview.document.contact).map(([key, value]) => <p className="break-all" key={key}>{key}: {value}</p>)}
            {Object.entries(exportPreview.document.links).map(([key, value]) => <p className="break-all" key={key}>{key}: {value}</p>)}
            <h3 className="font-semibold">Employment, education and certifications</h3>
            {exportPreview.document.records.map(record => <div key={`${record.source_table}/${record.source_id}`} className="rounded-card border border-edge p-2">
              <p className="text-xs text-ink-soft">{record.section}</p>
              {Object.entries(record.fields).map(([key, value]) => <p className="break-all" key={key}>{key}: {value}</p>)}
            </div>)}
            <label className="flex gap-2"><input type="checkbox" checked={exportConsent} onChange={event => setExportConsent(event.target.checked)} />I approve these exact contact and structured details for export with this resume.</label>
          </div>}
          <label
            htmlFor="approval-note"
            className="mt-3 block text-xs font-medium text-ink"
          >
            Note (optional)
          </label>
          <textarea
            id="approval-note"
            rows={2}
            value={approvalNote}
            onChange={(event) => setApprovalNote(event.target.value)}
            className="mt-1 w-full rounded-card border border-edge bg-surface p-2 text-sm text-ink"
          />
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              className={PRIMARY}
              disabled={busy || (!reviewer && (!exportPreview || !exportConsent))}
              onClick={() =>
                void run("approve", () =>
                  api.approveResume(
                    review.application_id,
                    approvalNote.trim() || undefined,
                    reviewer ? undefined : exportPreview?.digest,
                  ),
                )
              }
            >
              {pending === "approve" ? "Approving…" : "Confirm Approval"}
            </button>
            <button
              type="button"
              className={SECONDARY}
              disabled={busy}
              onClick={() => setConfirming(false)}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {changesOpen && (
        <form
          data-testid="changes-form"
          className="mt-4 rounded-card border border-edge bg-inset p-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (!isValidChangeNote(note) || !review.approval_id) return;
            void run("changes", () =>
              api.requestResumeChanges(review.application_id, note.trim(), review.approval_id!),
            );
          }}
        >
          <label htmlFor="changes-note" className="block text-xs font-medium text-ink">
            What should change?
          </label>
          <textarea
            id="changes-note"
            rows={3}
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Describe what needs rewriting, and why."
            className="mt-1 w-full rounded-card border border-edge bg-surface p-2 text-sm text-ink"
          />
          <p className="mt-1 text-[11px] text-ink-soft">
            Required. The note is stored with your decision and is what the
            rewrite will be based on.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="submit"
              className={PRIMARY}
              disabled={busy || !isValidChangeNote(note)}
            >
              {pending === "changes" ? "Sending…" : "Send Request"}
            </button>
            <button
              type="button"
              className={SECONDARY}
              disabled={busy}
              onClick={() => {
                setChangesOpen(false);
                setNote("");
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {/* --- decided, or not actionable ------------------------------------ */}
      {!review.can_start_review &&
        !review.can_approve &&
        !review.can_request_changes &&
        review.blocked_reason && (
          <p className="mt-4 text-xs text-ink-soft" data-testid="blocked-reason">
            {review.blocked_reason.includes("stale") ? "A newer resume is available. Refresh before making a decision; this review is stale." : "No review action is available right now. The current application state or verification needs to change first."}
          </p>
        )}

      {review.review_status === "changes_requested" && (
        <p className="mt-3 text-xs text-ink-soft">
          Regenerating from this request is a later phase — nothing has been
          rewritten yet, and the resume above is unchanged.
        </p>
      )}
    </section>
  );
}

/**
 * The most specific message available, without trusting the body's shape.
 *
 * The API answers with RFC 7807 problem details, whose `detail` is written for
 * a human — "a newer resume version exists; this review is stale" is far more
 * use than "Request failed: 409". Anything that is not that shape falls back
 * to the error's own message.
 */
function errorMessage(cause: unknown): string {
  if (cause instanceof ApiError && isProblem(cause.detail)) {
    if (cause.detail.detail.includes("three request-changes cycles") || cause.detail.detail.includes("rework_limit_reached")) {
      return "This application has reached its three revision requests. The current resume remains available to review; no further change request can be sent.";
    }
    return cause.detail.detail;
  }
  if (cause instanceof Error) return cause.message;
  return "The review action failed";
}

/** Narrow an RFC 7807 problem body without trusting its shape. */
function isProblem(value: unknown): value is { detail: string } {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { detail?: unknown }).detail === "string"
  );
}
