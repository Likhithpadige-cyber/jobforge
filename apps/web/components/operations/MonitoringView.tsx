"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { StatusBadge } from "@/components/StatusBadge";
import {
  NOT_AVAILABLE,
  formatAge,
  formatMetric,
  isStale,
  kindLabel,
  loadError,
  operations,
  series,
  snapshotAgeMs,
  type LoadError,
  type MonitoringSummary,
  type Operations,
  type Snapshot,
} from "@/lib/operations";
import { ErrorState, LoadingState } from "./OperatorStates";

const TREND_SNAPSHOTS = 24; // two hours of 5-minute buckets
const REFRESH_MS = 60_000;

/**
 * Operational health from the monitor's persisted snapshots only.
 *
 * Every figure is one the API returned. A metric that is absent stays
 * "Not available"; nothing is defaulted to zero or taken from demo data.
 */
export function MonitoringView() {
  const [summary, setSummary] = useState<MonitoringSummary | null>(null);
  const [history, setHistory] = useState<Snapshot[]>([]);
  const [error, setError] = useState<LoadError | null>(null);
  const [loading, setLoading] = useState(true);
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(async () => {
    try {
      const [nextSummary, nextHistory] = await Promise.all([
        operations.summary(),
        operations.snapshots(TREND_SNAPSHOTS),
      ]);
      setSummary(nextSummary);
      setHistory(nextHistory);
      setError(null);
    } catch (cause) {
      setError(loadError(cause));
    } finally {
      setNow(Date.now());
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  if (loading) return <LoadingState label="Loading monitoring data" />;
  if (error && !summary) return <ErrorState error={error} onRetry={() => void load()} />;
  if (!summary) return null;

  const snapshot = summary.latest_snapshot;
  const ops = snapshot?.operations ?? null;
  const age = snapshotAgeMs(ops, now);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-ink-soft">
          Last evaluated: <span className="font-medium text-ink">{formatAge(age)}</span>
          {summary.generation_paused && <span className="ml-2"><StatusBadge label="Generation paused" tone="risk" /></span>}
        </p>
        <button type="button" onClick={() => void load()} className="inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-accent hover:border-control">
          Refresh
        </button>
      </div>
      {error && (
        <p role="alert" className="rounded-card border border-attention bg-inset px-4 py-3 text-sm text-ink">
          The latest refresh failed: {error.message} Showing the previous data.
        </p>
      )}

      {!snapshot && (
        <EmptyState title="Monitoring has not run yet" body="The monitor records a snapshot every five minutes once the scheduler and monitor workers are running." />
      )}
      {snapshot && !ops && (
        <EmptyState title="No operational data in the latest snapshot" body="The latest snapshot predates operational monitoring or was recorded without it. The next monitor run will fill this in." />
      )}
      {ops && (
        <>
          {isStale(age) && (
            <p role="alert" data-testid="stale-snapshot" className="rounded-card border border-attention bg-inset px-4 py-3 text-sm text-ink">
              <strong className="font-semibold">Monitoring data is stale.</strong> The last evaluation was {formatAge(age)}. The monitor or scheduler worker may be down, and it cannot report its own absence.
            </p>
          )}
          {ops.redis_available === false && (
            <p role="alert" className="rounded-card border border-attention bg-inset px-4 py-3 text-sm text-ink">
              Redis was unavailable at the last evaluation, so worker health and consumer lag were not measured.
            </p>
          )}
          <Headline ops={ops} openAlerts={summary.open_alerts.total} />
          <div className="grid gap-5 xl:grid-cols-2">
            <WorkerHealth ops={ops} />
            <ConsumerLag ops={ops} />
            <StalledApplications ops={ops} />
            <OpenAlerts byKind={summary.open_alerts.by_kind} />
          </div>
          <Trends history={history} />
        </>
      )}
    </div>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="rounded-[1.25rem] border border-edge bg-surface p-5 shadow-raised">
      <h3 className="text-sm font-semibold text-ink">{title}</h3>
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Stat({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "risk" }) {
  return (
    <div className="rounded-card border border-edge bg-surface p-4 shadow-raised">
      <p className="text-xs font-medium text-ink-soft">{label}</p>
      <p className={`mt-1 text-2xl font-semibold tracking-tight ${tone === "risk" ? "text-danger" : "text-ink"}`}>{value}</p>
      {hint && <p className="mt-1 text-xs text-ink-soft">{hint}</p>}
    </div>
  );
}

function sumValues(values: Record<string, number> | undefined): number | null {
  if (!values) return null;
  return Object.values(values).reduce((total, value) => total + value, 0);
}

function Headline({ ops, openAlerts }: { ops: Operations; openAlerts: number }) {
  const sendWindow = ops.submission_window;
  const stalled = sumValues(ops.stalled_applications);
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
      <Stat label="Unresolved alerts" value={formatMetric(openAlerts)} tone={openAlerts > 0 ? "risk" : undefined} />
      <Stat label="Dead-lettered deliveries" value={formatMetric(ops.dead_letters)} hint="Each needs an owner to review it" tone={(ops.dead_letters ?? 0) > 0 ? "risk" : undefined} />
      <Stat
        label="Submission failures"
        value={sendWindow ? `${formatMetric(sendWindow.failures)} of ${formatMetric(sendWindow.attempts)}` : NOT_AVAILABLE}
        hint="Most recent real send attempts"
      />
      <Stat label="Stalled applications" value={formatMetric(stalled)} />
      <Stat label="Pending approvals" value={formatMetric(ops.pending_approvals)} />
    </div>
  );
}

function WorkerHealth({ ops }: { ops: Operations }) {
  if (!ops.workers) return <Card title="Worker health"><p className="text-sm text-ink-soft">{NOT_AVAILABLE}: heartbeats were not read in the last evaluation.</p></Card>;
  const kinds = Object.entries(ops.workers).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Card title="Worker health">
      {kinds.length === 0 ? (
        <p className="text-sm text-ink-soft">No worker has registered a heartbeat yet.</p>
      ) : (
        <table className="w-full text-left text-sm">
          <thead className="text-xs text-ink-soft"><tr><th scope="col" className="py-1.5 font-medium">Worker</th><th scope="col" className="py-1.5 font-medium">Live</th><th scope="col" className="py-1.5 font-medium">Missing</th></tr></thead>
          <tbody>
            {kinds.map(([kind, counts]) => (
              <tr key={kind} className="border-t border-edge">
                <th scope="row" className="py-2 font-medium text-ink">{kind}</th>
                <td className="py-2 text-ink">{formatMetric(counts.live)}</td>
                <td className="py-2">{counts.missing > 0 ? <StatusBadge label={`${counts.missing} missing`} tone="risk" /> : <span className="text-ink">0</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

function ConsumerLag({ ops }: { ops: Operations }) {
  if (!ops.consumer_lag) return <Card title="Consumer lag"><p className="text-sm text-ink-soft">{NOT_AVAILABLE}: stream lag was not read in the last evaluation.</p></Card>;
  const entries = Object.entries(ops.consumer_lag).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Card title="Consumer lag">
      {entries.length === 0 ? (
        <p className="text-sm text-ink-soft">No consumer groups exist yet.</p>
      ) : (
        <table className="w-full text-left text-sm">
          <thead className="text-xs text-ink-soft"><tr><th scope="col" className="py-1.5 font-medium">Stream / group</th><th scope="col" className="py-1.5 text-right font-medium">Pending entries</th></tr></thead>
          <tbody>
            {entries.map(([key, lag]) => (
              <tr key={key} className="border-t border-edge">
                <th scope="row" className="break-all py-2 pr-3 font-mono text-xs font-normal text-ink">{key}</th>
                <td className="py-2 text-right text-ink">{lag === null ? "Unknown" : formatMetric(lag)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

function StalledApplications({ ops }: { ops: Operations }) {
  if (!ops.stalled_applications) return <Card title="Stalled applications"><p className="text-sm text-ink-soft">{NOT_AVAILABLE}</p></Card>;
  const entries = Object.entries(ops.stalled_applications);
  return (
    <Card title="Stalled applications">
      {entries.length === 0 ? (
        <p className="text-sm text-ink-soft">No application is stalled in a system-owned step.</p>
      ) : (
        <ul className="space-y-2 text-sm">
          {entries.map(([status, count]) => (
            <li key={status} className="flex justify-between gap-3"><span className="text-ink">{status.replace(/_/g, " ")}</span><span className="font-medium text-attention">{formatMetric(count)}</span></li>
          ))}
        </ul>
      )}
      <Link href="/alerts" className="mt-3 inline-flex min-h-11 items-center text-sm font-semibold text-accent hover:underline">See stalled-application alerts</Link>
    </Card>
  );
}

function OpenAlerts({ byKind }: { byKind: Record<string, number> }) {
  const entries = Object.entries(byKind).sort(([, a], [, b]) => b - a);
  return (
    <Card title="Unresolved alerts by kind">
      {entries.length === 0 ? (
        <p className="text-sm text-ink-soft">No unresolved alerts.</p>
      ) : (
        <ul className="space-y-2 text-sm">
          {entries.map(([kind, count]) => (
            <li key={kind} className="flex justify-between gap-3"><span className="text-ink">{kindLabel(kind)}</span><span className="font-medium text-ink">{formatMetric(count)}</span></li>
          ))}
        </ul>
      )}
      <Link href="/alerts" className="mt-3 inline-flex min-h-11 items-center text-sm font-semibold text-accent hover:underline">Review alerts</Link>
    </Card>
  );
}

function Trends({ history }: { history: Snapshot[] }) {
  const charts = [
    { label: "Pending approvals", values: series(history, (ops) => ops.pending_approvals) },
    { label: "Dead-lettered deliveries", values: series(history, (ops) => ops.dead_letters) },
    { label: "Submission failures in window", values: series(history, (ops) => ops.submission_window?.failures) },
  ];
  return (
    <Card title="Last two hours">
      <div className="grid gap-5 md:grid-cols-3">
        {charts.map((chart) => <Sparkline key={chart.label} label={chart.label} values={chart.values} />)}
      </div>
    </Card>
  );
}

export function Sparkline({ label, values }: { label: string; values: (number | null)[] }) {
  const known = values.filter((value): value is number => value !== null);
  if (known.length < 2) {
    return <div><p className="text-xs font-medium text-ink-soft">{label}</p><p className="mt-2 text-sm text-ink-soft">Not enough data yet</p></div>;
  }
  const max = Math.max(...known, 1);
  const width = 160;
  const height = 40;
  const step = values.length > 1 ? width / (values.length - 1) : width;
  // Gaps break the line rather than being drawn as zero.
  const segments: string[] = [];
  let current = "";
  values.forEach((value, index) => {
    if (value === null) {
      if (current) segments.push(current);
      current = "";
      return;
    }
    const point = `${(index * step).toFixed(1)},${(height - (value / max) * height).toFixed(1)}`;
    current = current ? `${current} L${point}` : `M${point}`;
  });
  if (current) segments.push(current);
  const latest = known[known.length - 1]!;
  return (
    <figure>
      <figcaption className="flex justify-between text-xs font-medium text-ink-soft"><span>{label}</span><span className="text-ink">{formatMetric(latest)}</span></figcaption>
      <svg viewBox={`0 0 ${width} ${height}`} className="mt-2 h-10 w-full" role="img" aria-label={`${label}: latest ${latest}, highest ${Math.max(...known)} over ${known.length} snapshots`} preserveAspectRatio="none">
        {segments.map((d) => <path key={d} d={d} fill="none" stroke="currentColor" strokeWidth="2" className="text-accent" vectorEffect="non-scaling-stroke" />)}
      </svg>
    </figure>
  );
}
