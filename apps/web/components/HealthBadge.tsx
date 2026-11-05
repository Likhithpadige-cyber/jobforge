"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type Readiness } from "@/lib/api";

const STATUS_STYLES: Record<string, string> = {
  ok: "bg-inset text-ink",
  degraded: "bg-inset text-ink",
  unavailable: "bg-inset text-danger",
  unknown: "bg-inset text-ink-soft",
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
      className="rounded-xl border border-edge bg-surface p-6"
    >
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-ink-soft">
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
        <p role="alert" className="mt-4 text-sm text-danger">
          {error}
        </p>
      )}

      {health && (
        <dl className="mt-4 space-y-2 text-sm">
          {Object.entries(health.checks).map(([name, check]) => (
            <div key={name} className="flex items-center justify-between" data-testid={`check-${name}`}>
              <dt className="font-medium capitalize">{name}</dt>
              <dd className="tabular-nums text-ink-soft">
                {check.status}
                {check.latency_ms !== null ? ` · ${check.latency_ms} ms` : ""}
              </dd>
            </div>
          ))}
          <div className="flex items-center justify-between border-t border-edge pt-2">
            <dt className="font-medium">Migration</dt>
            <dd className="font-mono text-xs text-ink-soft">
              {health.migration_revision ?? "not applied"}
            </dd>
          </div>
        </dl>
      )}

      <button
        type="button"
        onClick={() => void load()}
        className="mt-5 rounded-lg border border-edge px-3 py-1.5 text-sm font-medium hover:bg-inset"
      >
        Refresh
      </button>
    </section>
  );
}
