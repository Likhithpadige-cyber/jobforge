"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { StatusBadge } from "@/components/StatusBadge";
import { formatTimestamp } from "@/lib/resumes";
import {
  SEVERITY_ORDER,
  kindLabel,
  loadError,
  operations,
  severityTone,
  statusLabel,
  type AlertQuery,
  type AlertSeverity,
  type AlertStatusFilter,
  type LoadError,
  type OperatorAlert,
} from "@/lib/operations";
import { ErrorState, LoadingState } from "./OperatorStates";

const PAGE_SIZE = 50;
const STATUS_TABS: { value: AlertStatusFilter; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "resolved", label: "Resolved" },
  { value: "all", label: "All" },
];
const KINDS = [
  "fabrication",
  "numeral_invented",
  "sensitive_auto_answer_attempt",
  "prompt_injection",
  "pii_in_prompt",
  "worker_heartbeat_missing",
  "dead_letter",
  "submission_ambiguous",
  "submission_failure_spike",
  "application_stalled",
  "approval_backlog",
  "consumer_lag",
];

export function AlertsView({ initialPausingOnly = false }: { initialPausingOnly?: boolean }) {
  const [status, setStatus] = useState<AlertStatusFilter>("open");
  const [severity, setSeverity] = useState<AlertSeverity | "">("");
  const [kind, setKind] = useState("");
  const [pausingOnly, setPausingOnly] = useState(initialPausingOnly);
  const [rows, setRows] = useState<OperatorAlert[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<LoadError | null>(null);
  const [attempt, setAttempt] = useState(0);

  const query = useCallback(
    (offset: number): AlertQuery => ({
      status,
      severity: severity || undefined,
      kind: kind || undefined,
      auto_paused: pausingOnly ? true : undefined,
      limit: PAGE_SIZE,
      offset,
    }),
    [status, severity, kind, pausingOnly],
  );

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    operations
      .listAlerts(query(0))
      .then((result) => {
        if (!active) return;
        setRows(result);
        setHasMore(result.length === PAGE_SIZE);
      })
      .catch((cause) => active && setError(loadError(cause)))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [query, attempt]);

  async function loadMore() {
    setLoadingMore(true);
    try {
      const next = await operations.listAlerts(query(rows.length));
      setRows((current) => [...current, ...next]);
      setHasMore(next.length === PAGE_SIZE);
    } catch (cause) {
      setError(loadError(cause));
    } finally {
      setLoadingMore(false);
    }
  }

  const filtered = Boolean(severity || kind || pausingOnly);
  return (
    <div className="space-y-5">
      <section aria-label="Filter alerts" className="space-y-4">
        <div role="group" aria-label="Alert status" className="flex flex-wrap gap-2">
          {STATUS_TABS.map((tab) => (
            <button
              key={tab.value}
              type="button"
              aria-pressed={status === tab.value}
              onClick={() => setStatus(tab.value)}
              className={`min-h-11 rounded-full border px-4 text-sm font-medium transition-colors ${status === tab.value ? "border-violet bg-violet text-white" : "border-edge bg-surface text-ink-soft hover:border-violet/40 hover:text-violet-deep"}`}
            >
              {tab.label}
            </button>
          ))}
        </div>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <label className="text-sm font-medium text-ink">
            Severity
            <select value={severity} onChange={(event) => setSeverity(event.target.value as AlertSeverity | "")} className="mt-1.5 h-11 w-full rounded-chip border border-edge bg-surface px-3 text-sm font-normal text-ink sm:w-40">
              <option value="">Any severity</option>
              {SEVERITY_ORDER.map((value) => (
                <option key={value} value={value}>{value[0]!.toUpperCase() + value.slice(1)}</option>
              ))}
            </select>
          </label>
          <label className="text-sm font-medium text-ink">
            Kind
            <select value={kind} onChange={(event) => setKind(event.target.value)} className="mt-1.5 h-11 w-full rounded-chip border border-edge bg-surface px-3 text-sm font-normal text-ink sm:w-64">
              <option value="">Any kind</option>
              {KINDS.map((value) => (
                <option key={value} value={value}>{kindLabel(value)}</option>
              ))}
            </select>
          </label>
          <label className="flex min-h-11 items-center gap-2 text-sm font-medium text-ink">
            <input type="checkbox" checked={pausingOnly} onChange={(event) => setPausingOnly(event.target.checked)} className="h-5 w-5 accent-violet" />
            Pauses generation only
          </label>
        </div>
      </section>

      {loading && <LoadingState label="Loading alerts" />}
      {!loading && error && <ErrorState error={error} onRetry={() => setAttempt((value) => value + 1)} />}
      {!loading && !error && rows.length === 0 && (
        <EmptyState
          title={filtered ? "No alerts match these filters" : status === "resolved" ? "No resolved alerts yet" : status === "open" ? "No open alerts" : "No alerts recorded"}
          body={status === "open" && !filtered ? "Nothing currently needs your attention. New incidents appear here as soon as the monitor records them." : "Try another status or filter."}
        />
      )}
      {!loading && !error && rows.length > 0 && (
        <ul className="space-y-3" aria-label="Alerts">
          {rows.map((alert) => (
            <li key={alert.id}>
              <Link
                href={`/alerts/${alert.id}`}
                className="block rounded-card border border-edge bg-surface p-4 shadow-raised transition-colors hover:border-violet/40 focus-visible:border-violet sm:p-5"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-xs font-semibold uppercase tracking-[.12em] text-violet-deep">{kindLabel(alert.kind)}</p>
                    <p className="mt-1 font-semibold text-ink">{alert.title}</p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {alert.auto_paused && <StatusBadge label="Pauses generation" tone="risk" />}
                    <StatusBadge label={alert.severity[0]!.toUpperCase() + alert.severity.slice(1)} tone={severityTone(alert.severity)} />
                    <StatusBadge label={statusLabel(alert)} tone={alert.status === "resolved" ? "strong" : "neutral"} />
                  </div>
                </div>
                <p className="mt-3 text-xs text-ink-soft">
                  Raised {formatTimestamp(alert.created_at)}
                  {" · "}
                  {alert.requires_human ? "Needs owner resolution" : "Clears itself on verified recovery"}
                </p>
              </Link>
            </li>
          ))}
        </ul>
      )}
      {!loading && !error && hasMore && (
        <button type="button" onClick={loadMore} disabled={loadingMore} className="min-h-11 w-full rounded-chip border border-edge bg-surface text-sm font-semibold text-violet-deep hover:border-violet/40 disabled:opacity-50">
          {loadingMore ? "Loading…" : "Load more alerts"}
        </button>
      )}
    </div>
  );
}
