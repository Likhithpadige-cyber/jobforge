"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type Readiness } from "@/lib/api";

const STATUS_STYLES: Record<string, string> = {
  ok: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200",
  degraded: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200",
  unavailable: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200",
  unknown: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
};

export function HealthBadge() {
  const [health, setHealth] = useState<Readiness | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setHealth(await api.readiness());
      setError(null);
    } catch (cause) {
      setHealth(null);
      setError(cause instanceof Error ? cause.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 15_000);
    return () => clearInterval(timer);
  }, [load]);

  const status = error ? "unavailable" : (health?.status ?? "unknown");

  return (
    <section
      aria-label="System health"
      className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          System health
        </h2>
        <span
          data-testid="overall-status"
          className={`rounded-full px-3 py-1 text-xs font-semibold ${STATUS_STYLES[status]}`}
        >
          {loading && !health ? "checking…" : status}
        </span>
      </div>

      {error && (
        <p role="alert" className="mt-4 text-sm text-red-700 dark:text-red-300">
          {error}
        </p>
      )}

      {health && (
        <dl className="mt-4 space-y-2 text-sm">
          {Object.entries(health.checks).map(([name, check]) => (
            <div key={name} className="flex items-center justify-between" data-testid={`check-${name}`}>
              <dt className="font-medium capitalize">{name}</dt>
              <dd className="tabular-nums text-slate-600 dark:text-slate-400">
                {check.status}
                {check.latency_ms !== null ? ` · ${check.latency_ms} ms` : ""}
              </dd>
            </div>
          ))}
          <div className="flex items-center justify-between border-t border-slate-100 pt-2 dark:border-slate-800">
            <dt className="font-medium">Migration</dt>
            <dd className="font-mono text-xs text-slate-600 dark:text-slate-400">
              {health.migration_revision ?? "not applied"}
            </dd>
          </div>
        </dl>
      )}

      <button
        type="button"
        onClick={() => void load()}
        className="mt-5 rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
      >
        Refresh
      </button>
    </section>
  );
}
