"use client";

import { useId, useState } from "react";
import { asSentence } from "@/lib/review";
import { ConfirmDialog } from "./ConfirmDialog";

const NOTE_LIMIT = 4000;

/**
 * The human decision on one reviewable item. Both decisions are explicit:
 * approval asks for confirmation, and requesting changes needs a note
 * (required by the API) plus confirmation of what happens next. The parent
 * re-reads server state after either; nothing is updated optimistically.
 */
export function DecisionForm({
  subject,
  canApprove,
  canRequestChanges,
  blockedReason,
  approveAcknowledgement,
  changesEffect,
  onApprove,
  onRequestChanges,
}: {
  subject: string;
  canApprove: boolean;
  canRequestChanges: boolean;
  blockedReason: string | null;
  approveAcknowledgement: string;
  changesEffect: string;
  onApprove: (note: string) => Promise<void>;
  onRequestChanges: (note: string) => Promise<void>;
}) {
  const noteId = useId();
  const changesId = useId();
  const [note, setNote] = useState("");
  const [changes, setChanges] = useState("");
  const [confirming, setConfirming] = useState<"approve" | "changes" | null>(null);

  if (!canApprove && !canRequestChanges) {
    return (
      <p data-testid="decision-blocked" className="rounded-card border border-edge bg-muted px-4 py-3 text-sm text-ink-soft">
        {blockedReason ? asSentence(blockedReason) : `No decision is available for this ${subject} right now.`}
      </p>
    );
  }

  const trimmed = changes.trim();
  return (
    <div className="space-y-5">
      {canApprove && (
        <div className="space-y-2">
          <label htmlFor={noteId} className="block text-sm font-medium text-ink">Approval note (optional)</label>
          <textarea id={noteId} value={note} maxLength={NOTE_LIMIT} onChange={(event) => setNote(event.target.value)} rows={2} className="w-full rounded-chip border border-edge bg-surface px-3 py-2 text-sm text-ink" />
          <button type="button" onClick={() => setConfirming("approve")} className="inline-flex min-h-11 items-center rounded-chip bg-violet px-4 text-sm font-semibold text-white hover:bg-violet-deep">
            Approve {subject}…
          </button>
        </div>
      )}
      {canRequestChanges && (
        <div className="space-y-2 border-t border-edge pt-5">
          <label htmlFor={changesId} className="block text-sm font-medium text-ink">What should change? (required)</label>
          <textarea id={changesId} value={changes} maxLength={NOTE_LIMIT} onChange={(event) => setChanges(event.target.value)} rows={3} aria-required="true" aria-describedby={`${changesId}-help`} className="w-full rounded-chip border border-edge bg-surface px-3 py-2 text-sm text-ink" />
          <p id={`${changesId}-help`} className="text-xs text-ink-soft">{changesEffect}</p>
          {!trimmed && <p id={`${changesId}-why`} className="text-xs text-ink-soft">Write what should change to enable this.</p>}
          <button type="button" onClick={() => setConfirming("changes")} disabled={!trimmed} aria-describedby={trimmed ? undefined : `${changesId}-why`} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-ink hover:border-violet/40 disabled:cursor-not-allowed disabled:opacity-50">
            Request changes…
          </button>
        </div>
      )}
      {confirming === "approve" && (
        <ConfirmDialog
          title={`Approve this ${subject}?`}
          acknowledgement={approveAcknowledgement}
          confirmLabel="Approve"
          busyLabel="Approving…"
          onCancel={() => setConfirming(null)}
          onConfirm={async () => {
            await onApprove(note.trim());
            setConfirming(null);
            setNote("");
          }}
        >
          <p>Approval is recorded under your account. It is one of the approvals an application needs before it can become ready to submit; it does not submit anything.</p>
        </ConfirmDialog>
      )}
      {confirming === "changes" && (
        <ConfirmDialog
          title={`Request changes to this ${subject}?`}
          acknowledgement="Send it back with my note."
          confirmLabel="Request changes"
          busyLabel="Sending…"
          onCancel={() => setConfirming(null)}
          onConfirm={async () => {
            await onRequestChanges(trimmed);
            setConfirming(null);
            setChanges("");
          }}
        >
          <p>{changesEffect}</p>
          <blockquote className="whitespace-pre-wrap rounded-chip border border-edge bg-muted px-3 py-2 text-ink">{trimmed}</blockquote>
        </ConfirmDialog>
      )}
    </div>
  );
}
