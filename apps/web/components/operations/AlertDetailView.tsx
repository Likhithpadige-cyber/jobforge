"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { StatusBadge } from "@/components/StatusBadge";
import { formatTimestamp } from "@/lib/resumes";
import {
  kindLabel,
  loadError,
  operations,
  severityTone,
  statusLabel,
  type LoadError,
  type OperatorAlert,
} from "@/lib/operations";
import { ErrorState, LoadingState } from "./OperatorStates";
import { useOperations } from "./OperationsProvider";
import { ResolveAlertDialog } from "./ResolveAlertDialog";

export function AlertDetailView({ id }: { id: string }) {
  const { pause, pauseStale, refresh } = useOperations();
  const [alert, setAlert] = useState<OperatorAlert | null>(null);
  const [error, setError] = useState<LoadError | null>(null);
  const [loading, setLoading] = useState(true);
  const [confirming, setConfirming] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setAlert(await operations.getAlert(id));
    } catch (cause) {
      setError(loadError(cause));
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function onResolved() {
    setConfirming(false);
    // Re-read both from the server; the banner follows the server, not us.
    await Promise.all([load(), refresh()]);
    setNotice("Incident resolved. Generation status was re-checked with the server.");
  }

  if (loading && !alert) return <LoadingState label="Loading alert" />;
  if (error && !alert) return <ErrorState error={error} onRetry={load} />;
  if (!alert) return null;

  const resolved = alert.status === "resolved";
  return (
    <div className="space-y-5">
      <Link href="/alerts" className="inline-flex min-h-11 items-center text-sm font-semibold text-accent hover:underline">← All alerts</Link>
      {notice && <p role="status" className="rounded-card border border-success bg-inset px-4 py-3 text-sm text-ink">{notice}</p>}
      <article className="rounded-[1.25rem] border border-edge bg-surface p-5 shadow-raised sm:p-7">
        <p className="text-xs font-semibold uppercase tracking-[.12em] text-accent">{kindLabel(alert.kind)}</p>
        <h2 className="mt-1 text-2xl font-semibold tracking-tight text-ink">{alert.title}</h2>
        <div className="mt-3 flex flex-wrap gap-2">
          {alert.auto_paused && <StatusBadge label="Pauses generation" tone="risk" />}
          <StatusBadge label={alert.severity[0]!.toUpperCase() + alert.severity.slice(1)} tone={severityTone(alert.severity)} />
          <StatusBadge label={statusLabel(alert)} tone={resolved ? "strong" : "neutral"} />
        </div>

        <dl className="mt-6 grid gap-4 border-t border-edge pt-5 text-sm sm:grid-cols-2">
          <div><dt className="text-xs text-ink-soft">Raised</dt><dd className="mt-1 font-medium text-ink">{formatTimestamp(alert.created_at)}</dd></div>
          <div><dt className="text-xs text-ink-soft">Resolution</dt><dd className="mt-1 font-medium text-ink">{alert.requires_human ? "Requires owner resolution" : "Clears itself when recovery is verified; an owner may also resolve it"}</dd></div>
          {alert.acknowledged_at && <div><dt className="text-xs text-ink-soft">Acknowledged</dt><dd className="mt-1 font-medium text-ink">{formatTimestamp(alert.acknowledged_at)}</dd></div>}
          {resolved && (
            <div>
              <dt className="text-xs text-ink-soft">Resolved</dt>
              <dd className="mt-1 font-medium text-ink">
                {alert.resolved_at ? formatTimestamp(alert.resolved_at) : "Time not recorded"}
                {" · "}
                {alert.resolution === "monitor" ? "recovery verified by the monitor" : "by the workspace owner"}
              </dd>
            </div>
          )}
        </dl>

        {(alert.application_id || alert.job_id) && (
          <div className="mt-5 flex flex-wrap gap-3 border-t border-edge pt-5">
            {alert.application_id && <Link href={`/applications/${alert.application_id}`} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-accent hover:border-control">Open application</Link>}
            {alert.job_id && <Link href={`/job-feed/${alert.job_id}`} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-accent hover:border-control">Open job</Link>}
          </div>
        )}

        {!resolved && (
          <div className="mt-6 border-t border-edge pt-5">
            {alert.auto_paused && pauseStale && (
              <p className="mb-3 text-sm text-attention">The generation status could not be refreshed; the impact below uses the last confirmed value.</p>
            )}
            <button type="button" onClick={() => setConfirming(true)} className="inline-flex min-h-11 items-center rounded-chip bg-action px-4 text-sm font-semibold text-action-ink hover:bg-action-hover">
              Resolve incident…
            </button>
          </div>
        )}
      </article>
      {confirming && <ResolveAlertDialog alert={alert} pause={pause} onClose={() => setConfirming(false)} onResolved={onResolved} />}
    </div>
  );
}
