/**
 * Operator data layer (M12 Phase 3).
 *
 * Mirrors the owner-only alerts and monitoring APIs exactly
 * (apps/api/routers/alerts.py, apps/api/routers/monitoring.py). Nothing here
 * invents a value: a metric the server did not report is `null` and renders
 * as "Not available", never as zero.
 */

import { ApiError, apiRequest } from "./api";
import { env } from "./env";
import type { StatusTone } from "@/components/StatusBadge";

// ------------------------------------------------------------------- types
export type AlertSeverity = "critical" | "high" | "medium" | "low" | "info";
export type AlertStatus = "open" | "acknowledged" | "resolved";
export type AlertStatusFilter = "open" | "resolved" | "all";

export interface PauseStatus {
  generation_paused: boolean;
  unresolved_auto_pause_count: number;
}

export interface OperatorAlert {
  id: string;
  kind: string;
  title: string;
  severity: AlertSeverity;
  status: AlertStatus;
  auto_paused: boolean;
  requires_human: boolean;
  resolution: "human" | "monitor" | null;
  application_id: string | null;
  job_id: string | null;
  created_at: string;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
  resolved_at: string | null;
}

export interface AlertQuery {
  status?: AlertStatusFilter;
  kind?: string;
  severity?: AlertSeverity;
  auto_paused?: boolean;
  limit?: number;
  offset?: number;
}

export interface WorkerCounts {
  live: number;
  missing: number;
}

/** Every field is optional: an older snapshot may predate a metric. */
export interface Operations {
  evaluated_at?: string | null;
  redis_available?: boolean | null;
  workers?: Record<string, WorkerCounts>;
  consumer_lag?: Record<string, number | null>;
  dead_letters?: number;
  submission_window?: { attempts: number; failures: number };
  stalled_applications?: Record<string, number>;
  pending_approvals?: number;
  open_alerts?: Record<string, number>;
}

export interface Snapshot {
  bucket_start: string;
  grain: string;
  job_counts: Record<string, number>;
  operations: Operations | null;
}

export interface MonitoringSummary {
  generation_paused: boolean;
  open_alerts: {
    total: number;
    by_severity: Record<string, number>;
    by_kind: Record<string, number>;
  };
  latest_snapshot: Snapshot | null;
}

// --------------------------------------------------------------------- api
const prefix = () => env.apiPrefix;

export const operations = {
  pauseStatus: () => apiRequest<PauseStatus>(`${prefix()}/alerts/generation-pause`),

  listAlerts: (query: AlertQuery = {}) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== "") params.set(key, String(value));
    }
    const suffix = params.toString() ? `?${params}` : "";
    return apiRequest<OperatorAlert[]>(`${prefix()}/alerts${suffix}`);
  },

  getAlert: (id: string) =>
    apiRequest<OperatorAlert>(`${prefix()}/alerts/${encodeURIComponent(id)}`),

  /** Explicit human resolution. CSRF is attached by the shared transport. */
  resolveAlert: (id: string) =>
    apiRequest<OperatorAlert>(`${prefix()}/alerts/${encodeURIComponent(id)}/resolve`, {
      method: "POST",
    }),

  summary: () => apiRequest<MonitoringSummary>(`${prefix()}/monitoring/summary`),

  snapshots: (limit = 12) =>
    apiRequest<Snapshot[]>(`${prefix()}/monitoring/snapshots?limit=${limit}`),
};

// ----------------------------------------------------------------- display
export const NOT_AVAILABLE = "Not available";

/** A missing number is shown as missing. Zero is only shown when reported. */
export function formatMetric(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("en-US")
    : NOT_AVAILABLE;
}

const KIND_LABELS: Record<string, string> = {
  worker_heartbeat_missing: "Worker heartbeat missing",
  dead_letter: "Dead-lettered delivery",
  submission_ambiguous: "Ambiguous submission",
  submission_failure_spike: "Submission failure spike",
  application_stalled: "Stalled application",
  approval_backlog: "Approval backlog",
  consumer_lag: "Consumer lag",
  fabrication: "Fabrication",
  numeral_invented: "Invented numeral",
  sensitive_auto_answer_attempt: "Sensitive auto-answer attempt",
  prompt_injection: "Prompt injection",
  overreach: "Overreach",
  schema_violation: "Schema violation",
  pii_in_prompt: "Private data in prompt",
  cost_ceiling: "Cost ceiling",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

export const SEVERITY_ORDER: AlertSeverity[] = ["critical", "high", "medium", "low", "info"];

export function severityTone(severity: AlertSeverity): StatusTone {
  if (severity === "critical" || severity === "high") return "risk";
  if (severity === "medium") return "review";
  return "neutral";
}

export function statusLabel(alert: OperatorAlert): string {
  if (alert.status !== "resolved") return alert.status === "acknowledged" ? "Acknowledged" : "Open";
  return alert.resolution === "monitor" ? "Recovered (monitor)" : "Resolved by owner";
}

/** What resolving this alert will do to generation, stated before the click. */
export function resolutionImpact(alert: OperatorAlert, pause: PauseStatus | null): string {
  if (!alert.auto_paused) return "This alert does not pause generation. Resolving it records that you reviewed it.";
  if (pause === null) {
    return "Generation is paused by incidents like this one. Its state will be re-read from the server after you resolve.";
  }
  const others = Math.max(pause.unresolved_auto_pause_count - 1, 0);
  return others > 0
    ? `Generation stays paused: ${others} other pausing incident${others === 1 ? "" : "s"} will remain unresolved.`
    : "This is the last unresolved pausing incident. Generation resumes only once the server confirms it.";
}

/** The monitor ticks every 5 minutes; three missed ticks means it may be down. */
export const STALE_AFTER_MS = 15 * 60 * 1000;

export function snapshotAgeMs(operations: Operations | null | undefined, now: number): number | null {
  const at = operations?.evaluated_at;
  if (!at) return null;
  const time = Date.parse(at);
  return Number.isNaN(time) ? null : Math.max(now - time, 0);
}

export function isStale(ageMs: number | null): boolean {
  return ageMs === null || ageMs > STALE_AFTER_MS;
}

export function formatAge(ageMs: number | null): string {
  if (ageMs === null) return NOT_AVAILABLE;
  const minutes = Math.floor(ageMs / 60_000);
  if (minutes < 1) return "less than a minute ago";
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.floor(minutes / 60);
  return `${hours} hour${hours === 1 ? "" : "s"} ago`;
}

/** A series for a sparkline, oldest first, with gaps kept as null. */
export function series(
  snapshots: Snapshot[],
  pick: (operations: Operations) => number | null | undefined,
): (number | null)[] {
  return [...snapshots]
    .sort((a, b) => Date.parse(a.bucket_start) - Date.parse(b.bucket_start))
    .map((snapshot) => {
      const value = snapshot.operations ? pick(snapshot.operations) : undefined;
      return typeof value === "number" && Number.isFinite(value) ? value : null;
    });
}

export type LoadError = { kind: "unauthorized" | "not_found" | "failed"; message: string };

export function loadError(cause: unknown): LoadError {
  if (cause instanceof ApiError && cause.status === 403) {
    return { kind: "unauthorized", message: "Owner access is required for operations views." };
  }
  if (cause instanceof ApiError && cause.status === 404) {
    return { kind: "not_found", message: "This record does not exist." };
  }
  if (cause instanceof ApiError && cause.status === 0) {
    return { kind: "failed", message: "Cannot reach the JobForge API." };
  }
  if (cause instanceof ApiError) {
    return { kind: "failed", message: `The API returned an error (${cause.status}).` };
  }
  return { kind: "failed", message: "Something went wrong while loading this view." };
}
