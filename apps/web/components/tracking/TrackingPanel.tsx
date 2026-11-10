"use client";

/**
 * M19: the owner's record of what happened after applying.
 *
 * Outcome, applied date, follow-up date and notes. This is a human record
 * only: saving it never moves the pipeline status, never marks anything as
 * submitted and never contacts an employer. Reviewers see it read-only.
 * The server validates every rule (an outcome needs an applied date, dates
 * in range, notes length) and its message is shown as-is.
 */

import { useEffect, useState } from "react";
import { useAuth } from "@/components/AuthGuard";
import { apiErrorText } from "@/lib/api";
import { OUTCOMES, OUTCOME_LABEL, localToday, trackingLabel, tracking as trackingApi, type Outcome, type Tracking, type TrackingChange } from "@/lib/tracking";

const FIELD = "mt-1 block w-full rounded-chip border border-edge bg-surface px-3 py-2 text-sm text-ink";

interface Draft {
  outcome: Outcome;
  applied_on: string;
  follow_up_on: string;
  notes: string;
}

const toDraft = (t: Tracking): Draft => ({
  outcome: t.outcome,
  applied_on: t.applied_on ?? "",
  follow_up_on: t.follow_up_on ?? "",
  notes: t.notes ?? "",
});

/** Only the fields that changed; empty inputs clear (null). */
export function changesFrom(saved: Tracking, draft: Draft): TrackingChange {
  const change: TrackingChange = {};
  if (draft.outcome !== saved.outcome) change.outcome = draft.outcome;
  if ((draft.applied_on || null) !== saved.applied_on) change.applied_on = draft.applied_on || null;
  if ((draft.follow_up_on || null) !== saved.follow_up_on) change.follow_up_on = draft.follow_up_on || null;
  if ((draft.notes.trim() || null) !== saved.notes) change.notes = draft.notes.trim() || null;
  return change;
}

export function TrackingPanel({ applicationId }: { applicationId: string }) {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";
  const [saved, setSaved] = useState<Tracking | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    trackingApi
      .get(applicationId)
      .then((t) => {
        if (cancelled) return;
        setSaved(t);
        setDraft(toDraft(t));
      })
      .catch((cause) => !cancelled && setLoadError(apiErrorText(cause, "Tracking could not be loaded.")));
    return () => {
      cancelled = true;
    };
  }, [applicationId]);

  if (loadError) return <p role="alert" className="text-sm text-danger">{loadError}</p>;
  if (!saved || !draft) return <p className="text-sm text-ink-soft">Loading tracking…</p>;

  if (!isOwner) {
    return (
      <dl className="grid gap-2 text-sm sm:grid-cols-2">
        <div><dt className="text-ink-soft">Where it stands</dt><dd className="text-ink">{trackingLabel(saved)}</dd></div>
        <div><dt className="text-ink-soft">Applied</dt><dd className="text-ink">{saved.applied_on ?? "—"}</dd></div>
        <div><dt className="text-ink-soft">Follow up on</dt><dd className="text-ink">{saved.follow_up_on ?? "—"}</dd></div>
        <div className="sm:col-span-2"><dt className="text-ink-soft">Notes</dt><dd className="whitespace-pre-wrap text-ink">{saved.notes ?? "—"}</dd></div>
      </dl>
    );
  }

  const set = (patch: Partial<Draft>) => setDraft((d) => (d ? { ...d, ...patch } : d));

  async function save() {
    if (!saved || !draft) return;
    const change = changesFrom(saved, draft);
    if (Object.keys(change).length === 0) return setMessage({ tone: "ok", text: "Nothing changed." });
    setBusy(true);
    setMessage(null);
    try {
      const next = await trackingApi.update(applicationId, change);
      setSaved(next);
      setDraft(toDraft(next));
      setMessage({ tone: "ok", text: "Tracking saved. This is your record only; nothing was sent." });
    } catch (cause) {
      setMessage({ tone: "error", text: apiErrorText(cause, "Tracking could not be saved.") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      aria-label="Application tracking"
      className="space-y-3"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="block text-sm text-ink">
          Where it stands
          <select className={FIELD} value={draft.outcome} onChange={(e) => set({ outcome: e.target.value as Outcome })}>
            {OUTCOMES.map((o) => (
              <option key={o} value={o}>{o === "pending" ? "Applied / waiting" : OUTCOME_LABEL[o]}</option>
            ))}
          </select>
        </label>
        <label className="block text-sm text-ink">
          Date applied
          <input type="date" className={FIELD} value={draft.applied_on} max={localToday()} onChange={(e) => set({ applied_on: e.target.value })} />
        </label>
        <label className="block text-sm text-ink">
          Follow up on
          <input type="date" className={FIELD} value={draft.follow_up_on} onChange={(e) => set({ follow_up_on: e.target.value })} />
        </label>
      </div>
      {!draft.applied_on && saved.submitted_at && (
        <button type="button" className="text-xs font-semibold text-accent underline" onClick={() => set({ applied_on: saved.submitted_at!.slice(0, 10) })}>
          Use the recorded submission date ({saved.submitted_at.slice(0, 10)})
        </button>
      )}
      <label className="block text-sm text-ink">
        Notes
        <textarea className={`${FIELD} min-h-24`} maxLength={4000} value={draft.notes} onChange={(e) => set({ notes: e.target.value })} />
      </label>
      <p className="text-xs text-ink-soft">
        An outcome other than “Applied / waiting” needs the date you applied. Saving never changes the application&apos;s pipeline status or contacts anyone.
      </p>
      <button type="submit" disabled={busy} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-inset px-4 py-2 text-sm font-semibold text-attention disabled:opacity-60">
        {busy ? "Saving…" : "Save tracking"}
      </button>
      {message && (
        <p role={message.tone === "error" ? "alert" : "status"} className={`text-sm ${message.tone === "error" ? "text-danger" : "text-success"}`}>
          {message.text}
        </p>
      )}
    </form>
  );
}
