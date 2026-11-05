"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { reviewError } from "@/lib/review";

/**
 * Accessible confirmation for consequential actions: modal, labelled, focus
 * trapped, Escape cancels. The confirm button stays disabled until the
 * acknowledgement is ticked and while the request is in flight, so a double
 * click cannot send a second request. Errors stay in the dialog.
 */
export function ConfirmDialog({
  title,
  children,
  acknowledgement,
  confirmLabel,
  busyLabel,
  danger = false,
  onCancel,
  onConfirm,
}: {
  title: string;
  children: ReactNode;
  acknowledgement: string;
  confirmLabel: string;
  busyLabel: string;
  danger?: boolean;
  onCancel: () => void;
  onConfirm: () => Promise<void>;
}) {
  const titleId = useId();
  const bodyId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const inFlight = useRef(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLElement>("input, button")?.focus();
    return () => previous?.focus();
  }, []);

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Escape" && !busy) {
      event.stopPropagation();
      onCancel();
      return;
    }
    if (event.key !== "Tab" || !dialog.current) return;
    const focusable = Array.from(dialog.current.querySelectorAll<HTMLElement>("a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), button:not([disabled]), [tabindex]:not([tabindex='-1'])"));
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
    if (inFlight.current) return; // never a second request
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
    } catch (cause) {
      setError(reviewError(cause).message);
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center p-4 sm:items-center" onKeyDown={onKeyDown}>
      <button type="button" tabIndex={-1} aria-label="Close dialog" className="absolute inset-0 bg-ink/40" onClick={() => !busy && onCancel()} />
      <div ref={dialog} role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={bodyId} className="relative max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-[1.25rem] border border-edge bg-surface p-6 shadow-raised">
        <h2 id={titleId} className="text-lg font-semibold text-ink">{title}</h2>
        <div id={bodyId} className="mt-3 space-y-3 text-sm text-ink-soft">{children}</div>
        <label className="mt-5 flex min-h-11 items-start gap-3 text-sm text-ink">
          <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} disabled={busy} className="mt-0.5 h-5 w-5 accent-violet" />
          <span>{acknowledgement}</span>
        </label>
        {error && <p role="alert" className="mt-3 text-sm font-medium text-signal-risk">{error}</p>}
        <div className="mt-6 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
          <button type="button" onClick={onCancel} disabled={busy} className="min-h-11 rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-ink hover:border-violet/40">Cancel</button>
          <button type="button" onClick={confirm} disabled={!acknowledged || busy} className={`min-h-11 rounded-chip px-4 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50 ${danger ? "bg-signal-risk hover:opacity-90" : "bg-violet hover:bg-violet-deep"}`}>
            {busy ? busyLabel : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
