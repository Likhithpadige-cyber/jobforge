/**
 * Agent activity client (M13 Phase 3).
 *
 * Mirrors apps/api/routers/agent_runs.py. The server returns only run
 * metadata: what ran, its outcome, step names, tokens, cost and timing.
 * Prompts, model responses, step outputs and error messages are never part
 * of these responses, so nothing here can display them.
 */

import { apiRequest } from "./api";
import { env } from "./env";

export type AgentName = "resume_optimizer" | "job_application" | "monitoring";
export type RunStatus = "running" | "succeeded" | "failed" | "aborted";
export type Pipeline = "evaluation" | "tailoring" | "packet" | "submission" | "digest";

export interface AgentRun {
  id: string;
  agent: AgentName;
  pipeline: Pipeline;
  status: RunStatus;
  steps_total: number;
  steps_completed: number;
  input_tokens: number;
  output_tokens: number;
  /** Decimal string, e.g. "0.0123". */
  cost_usd: string;
  duration_ms: number | null;
  /** The error's class name only; never its message. */
  error_type: string | null;
  started_at: string;
  finished_at: string | null;
  application_id: string | null;
}

export interface AgentRunStep {
  step_index: number;
  step_name: string;
  status: RunStatus;
  duration_ms: number | null;
  started_at: string;
  finished_at: string | null;
}

export interface AgentRunDetail extends AgentRun {
  steps: AgentRunStep[];
}

export interface AgentRunSummary {
  since: string;
  runs: number;
  by_status: Partial<Record<RunStatus, number>>;
  input_tokens: number;
  output_tokens: number;
  cost_usd: string;
}

export interface AgentRunQuery {
  agent?: AgentName;
  status?: RunStatus;
  application_id?: string;
  limit?: number;
  offset?: number;
}

const base = () => `${env.apiPrefix}/agent-runs`;

export const activity = {
  runs: (query: AgentRunQuery = {}) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== "") params.set(key, String(value));
    }
    const qs = params.toString();
    return apiRequest<AgentRun[]>(`${base()}${qs ? `?${qs}` : ""}`);
  },
  run: (id: string) => apiRequest<AgentRunDetail>(`${base()}/${encodeURIComponent(id)}`),
  summary: (days = 7) => apiRequest<AgentRunSummary>(`${base()}/summary?days=${days}`),
};

export const AGENT_LABEL: Record<AgentName, string> = {
  resume_optimizer: "Resume optimizer",
  job_application: "Application agent",
  monitoring: "Monitoring",
};

export const PIPELINE_LABEL: Record<Pipeline, string> = {
  evaluation: "Job evaluation",
  tailoring: "Resume tailoring",
  packet: "Application packet",
  submission: "Submission",
  digest: "Digest",
};

export const RUN_STATUS: Record<RunStatus, { label: string; tone: "strong" | "risk" | "info" | "neutral" }> = {
  succeeded: { label: "Succeeded", tone: "strong" },
  failed: { label: "Failed", tone: "risk" },
  running: { label: "Running", tone: "info" },
  aborted: { label: "Stopped", tone: "neutral" },
};

/** "$0.0123" with up to four decimals, never scientific notation. */
export function formatCost(value: string | number): string {
  const amount = typeof value === "number" ? value : Number.parseFloat(value);
  if (!Number.isFinite(amount)) return "—";
  if (amount === 0) return "$0.00";
  return `$${amount < 0.01 ? amount.toFixed(4) : amount.toFixed(2)}`;
}

export function formatDuration(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds % 60)} s`;
}
