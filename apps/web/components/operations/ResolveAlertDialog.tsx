"use client";

import { useEffect, useId, useRef, useState } from "react";
import { loadError, operations, resolutionImpact, type OperatorAlert, type PauseStatus } from "@/lib/operations";

/**
 * Explicit human resolution. The confirm button stays disabled until the
 * owner states they reviewed the incident, and the consequence for
 * generation is shown before the click. Nothing is updated optimistically:
 * on success the caller re-reads the alert and the pause status from the API.
 */
export function ResolveAlertDialog({
  alert,
  pause,
  onClose,
  onResolved,
}: {
  alert: OperatorAlert;
  pause: PauseStatus | null;
  onClose: () => void;
  onResolved: () => Promise<void> | void;
}) {
  const titleId = useId();
  const descriptionId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const [reviewed, setReviewed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLElement>("input, button")?.focus();
    return () => previous?.focus();
  }, []);

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Escape" && !submitting) {
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== "Tab" || !dialog.current) return;
    const focusable = Array.from(
      dialog.current.querySelectorAll<HTMLElement>("input:not([disabled]), button:not([disabled])"),
    );
    if (focusable.length === 0) return;
    const first = focusable[0]!;
    const last = focusable[focusable.length - 1]!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  async function confirm() {
    setSubmitting(true);
    setError(null);
    try {
      await operations.resolveAlert(alert.id);
      await onResolved();
    } catch (cause) {
      const problem = loadError(cause);
      setError(problem.kind === "unauthorized" ? "Only the workspace owner can resolve incidents." : `Could not resolve: ${problem.message}`);
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center p-4 sm:items-center" onKeyDown={onKeyDown}>
      <button type="button" tabIndex={-1} aria-label="Close dialog" className="absolute inset-0 bg-canvas" onClick={() => !submitting && onClose()} />
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        className="relative w-full max-w-lg rounded-[1.25rem] border border-edge bg-surface p-6 shadow-raised"
      >
        <h2 id={titleId} className="text-lg font-semibold text-ink">Resolve this incident?</h2>
        <div id={descriptionId} className="mt-3 space-y-3 text-sm text-ink-soft">
          <p><span className="font-medium text-ink">{alert.title}</span>. Resolving records that you reviewed it. The alert is kept as history and cannot be reopened by the same evidence.</p>
          <p className={alert.auto_paused ? "font-medium text-danger" : ""} data-testid="resolution-impact">{resolutionImpact(alert, pause)}</p>
        </div>
        <label className="mt-5 flex min-h-11 items-start gap-3 text-sm text-ink">
          <input type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} disabled={submitting} className="mt-0.5 h-5 w-5 accent-action" />
          <span>I have reviewed this incident and its cause.</span>
        </label>
        {error && <p role="alert" className="mt-3 text-sm font-medium text-danger">{error}</p>}
        <div className="mt-6 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
          <button type="button" onClick={onClose} disabled={submitting} className="min-h-11 rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-ink hover:border-control">Cancel</button>
          <button type="button" onClick={confirm} disabled={!reviewed || submitting} className="min-h-11 rounded-chip bg-action px-4 text-sm font-semibold text-action-ink hover:bg-action-hover disabled:cursor-not-allowed">
            {submitting ? "Resolving…" : "Resolve incident"}
          </button>
        </div>
      </div>
    </div>
  );
}
