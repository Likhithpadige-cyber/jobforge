"use client";

import Link from "next/link";
import { useCallback, useEffect, useId, useState } from "react";
import { EmptyState } from "@/components/EmptyState";
import { StatusBadge } from "@/components/StatusBadge";
import { ReviewErrorState, ReviewLoading, SectionCard } from "@/components/review/ReviewStates";
import {
  AGENT_LABEL,
  PIPELINE_LABEL,
  RUN_STATUS,
  activity,
  formatCost,
  formatDuration,
  type AgentName,
  type AgentRun,
  type AgentRunDetail,
  type AgentRunSummary,
  type RunStatus,
} from "@/lib/activity";
import { formatTimestamp } from "@/lib/resumes";
import { reviewError, type ReviewError } from "@/lib/review";

const PAGE = 25;

/**
 * What the agents actually did: every run, its outcome, tokens, cost and the
 * names of its steps. Read-only; no prompts or model output are available.
 */
export function AgentActivityView() {
  const id = useId();
  const [agent, setAgent] = useState<AgentName | "">("");
  const [status, setStatus] = useState<RunStatus | "">("");
  const [runs, setRuns] = useState<AgentRun[] | null>(null);
  const [summary, setSummary] = useState<AgentRunSummary | null>(null);
  const [error, setError] = useState<ReviewError | null>(null);
  const [loading, setLoading] = useState(true);
  const [more, setMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [page, nextSummary] = await Promise.all([
        activity.runs({ agent: agent || undefined, status: status || undefined, limit: PAGE, offset: 0 }),
        activity.summary(7),
      ]);
      setRuns(page);
      setMore(page.length === PAGE);
      setSummary(nextSummary);
      setError(null);
    } catch (cause) {
      setError(reviewError(cause));
    } finally {
      setLoading(false);
    }
  }, [agent, status]);

  useEffect(() => {
    void load();
  }, [load]);

  async function loadMore() {
    if (!runs || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await activity.runs({ agent: agent || undefined, status: status || undefined, limit: PAGE, offset: runs.length });
      setRuns([...runs, ...page]);
      setMore(page.length === PAGE);
    } catch (cause) {
      setError(reviewError(cause));
    } finally {
      setLoadingMore(false);
    }
  }

  if (loading && !runs) return <ReviewLoading label="Loading agent activity" />;
  if (error && !runs) return <ReviewErrorState error={error} onRetry={load} backHref="/" />;

  return (
    <div className="space-y-6">
      {summary && (
        <section aria-label="Last 7 days" className="grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="activity-summary">
          <Metric label="Runs (7 days)" value={String(summary.runs)} />
          <Metric label="Succeeded" value={String(summary.by_status.succeeded ?? 0)} />
          <Metric label="Failed or stopped" value={String((summary.by_status.failed ?? 0) + (summary.by_status.aborted ?? 0))} />
          <Metric label="Model cost (7 days)" value={formatCost(summary.cost_usd)} hint={`${(summary.input_tokens + summary.output_tokens).toLocaleString()} tokens`} />
        </section>
      )}

      <SectionCard title="Runs">
        <div className="mb-4 flex flex-wrap gap-3">
          <div>
            <label htmlFor={`${id}-agent`} className="block text-xs font-semibold text-ink-soft">Agent</label>
            <select id={`${id}-agent`} value={agent} onChange={(event) => setAgent(event.target.value as AgentName | "")} className="mt-1 min-h-11 rounded-chip border border-edge bg-surface px-3 text-sm text-ink">
              <option value="">All agents</option>
              {(Object.keys(AGENT_LABEL) as AgentName[]).map((name) => <option key={name} value={name}>{AGENT_LABEL[name]}</option>)}
            </select>
          </div>
          <div>
            <label htmlFor={`${id}-status`} className="block text-xs font-semibold text-ink-soft">Outcome</label>
            <select id={`${id}-status`} value={status} onChange={(event) => setStatus(event.target.value as RunStatus | "")} className="mt-1 min-h-11 rounded-chip border border-edge bg-surface px-3 text-sm text-ink">
              <option value="">All outcomes</option>
              {(Object.keys(RUN_STATUS) as RunStatus[]).map((name) => <option key={name} value={name}>{RUN_STATUS[name].label}</option>)}
            </select>
          </div>
        </div>
        {error && <p role="alert" className="mb-3 rounded-card border border-signal-review/25 bg-signal-review/10 px-4 py-3 text-sm text-ink">The latest refresh failed: {error.message}</p>}
        {runs && runs.length === 0 ? (
          <EmptyState title="No runs yet" body={agent || status ? "No runs match these filters." : "Agent runs appear here once JobForge evaluates jobs or prepares an application."} />
        ) : (
          <ul className="space-y-3" aria-busy={loading}>
            {runs?.map((run) => <RunItem key={run.id} run={run} />)}
          </ul>
        )}
        {more && (
          <button type="button" onClick={() => void loadMore()} disabled={loadingMore} className="mt-4 inline-flex min-h-11 items-center rounded-chip border border-edge bg-surface px-4 text-sm font-semibold text-violet-deep hover:border-violet/40 disabled:opacity-50">
            {loadingMore ? "Loading…" : "Load more"}
          </button>
        )}
      </SectionCard>
    </div>
  );
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-card border border-edge bg-surface p-4 shadow-raised">
      <p className="text-xs text-ink-soft">{label}</p>
      <p className="mt-1 text-2xl font-semibold tracking-tight text-ink">{value}</p>
      {hint && <p className="text-xs text-ink-soft">{hint}</p>}
    </div>
  );
}

function RunItem({ run }: { run: AgentRun }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<AgentRunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const status = RUN_STATUS[run.status];

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next && !detail) {
      try {
        setDetail(await activity.run(run.id));
        setError(null);
      } catch (cause) {
        setError(reviewError(cause).message);
      }
    }
  }

  return (
    <li className="rounded-card border border-edge p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="font-medium text-ink">{PIPELINE_LABEL[run.pipeline]} <span className="text-ink-soft">· {AGENT_LABEL[run.agent]}</span></p>
          <p className="text-xs text-ink-soft">Started {formatTimestamp(run.started_at)} · {formatDuration(run.duration_ms)} · {run.steps_completed}/{run.steps_total} steps · {formatCost(run.cost_usd)}</p>
        </div>
        <StatusBadge label={status.label} tone={status.tone} />
      </div>
      {run.error_type && <p className="mt-2 text-sm text-signal-risk">Stopped with {run.error_type}</p>}
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <button type="button" onClick={() => void toggle()} aria-expanded={open} className="inline-flex min-h-11 items-center text-sm font-semibold text-violet-deep hover:underline">{open ? "Hide steps" : "Show steps"}</button>
        {run.application_id && <Link href={`/applications/${run.application_id}`} className="inline-flex min-h-11 items-center text-sm font-semibold text-violet-deep hover:underline">Open application →</Link>}
      </div>
      {open && (
        error ? <p role="alert" className="text-sm text-signal-risk">{error}</p>
        : !detail ? <p role="status" className="text-sm text-ink-soft">Loading steps…</p>
        : detail.steps.length === 0 ? <p className="text-sm text-ink-soft">No steps were recorded.</p>
        : (
          <ol className="mt-1 space-y-1" aria-label="Steps">
            {detail.steps.map((step) => (
              <li key={step.step_index} className="flex flex-wrap items-center justify-between gap-2 rounded-chip bg-muted px-3 py-2 text-sm">
                <span className="text-ink">{step.step_index + 1}. {step.step_name.replaceAll("_", " ")}</span>
                <span className="text-xs text-ink-soft">{RUN_STATUS[step.status].label} · {formatDuration(step.duration_ms)}</span>
              </li>
            ))}
          </ol>
        )
      )}
    </li>
  );
}
