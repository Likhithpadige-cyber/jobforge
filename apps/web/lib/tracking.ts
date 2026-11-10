/**
 * M19 application tracking client: apps/api/routers/tracking.py.
 *
 * Tracking is the owner's own record (outcome, applied date, follow-up date,
 * notes). It never moves the pipeline status and never sends anything.
 */

import { apiRequest } from "./api";
import { env } from "./env";

export type Outcome =
  | "pending"
  | "acknowledged"
  | "screening"
  | "interviewing"
  | "offer"
  | "accepted"
  | "rejected"
  | "no_response"
  | "withdrawn";

/** "pending" with an applied date reads as "Applied". */
export const OUTCOME_LABEL: Record<Outcome, string> = {
  pending: "Applied",
  acknowledged: "Acknowledged",
  screening: "Screening",
  interviewing: "Interview",
  offer: "Offer",
  accepted: "Offer accepted",
  rejected: "Rejected",
  no_response: "No response",
  withdrawn: "Withdrawn",
};
export const OUTCOMES = Object.keys(OUTCOME_LABEL) as Outcome[];

/** "jobforge": the approved pipeline submitted it. "manual": the owner recorded sending it. */
export type AppliedVia = "jobforge" | "manual";
export const APPLIED_VIA_LABEL: Record<AppliedVia, string> = {
  jobforge: "sent by JobForge",
  manual: "sent by you",
};

export interface Tracking {
  application_id: string;
  status: string;
  outcome: Outcome;
  applied_on: string | null;
  follow_up_on: string | null;
  notes: string | null;
  submitted_at: string | null;
  outcome_updated_at: string | null;
  applied_via: AppliedVia | null;
}

export type TrackingChange = Partial<Pick<Tracking, "outcome" | "applied_on" | "follow_up_on" | "notes">>;

export interface FollowUp {
  application_id: string;
  job_title: string;
  company_name: string | null;
  outcome: Outcome;
  applied_on: string | null;
  applied_via: AppliedVia | null;
  follow_up_on: string;
  days_overdue: number;
}

/** The browser's local date as YYYY-MM-DD (not UTC). */
export function localToday(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

export function trackingLabel(t: { outcome: Outcome; applied_via: AppliedVia | null }): string {
  return t.outcome === "pending" && !t.applied_via ? "Not applied yet" : OUTCOME_LABEL[t.outcome];
}

/** YYYY-MM-DD plus n calendar days (local, DST-safe). */
export function addDays(day: string, n: number): string {
  const [y, m, d] = day.split("-").map(Number);
  return localToday(new Date(y!, m! - 1, d! + n));
}

/** List filters: each maps to the server's outcome/applied query. */
export const TRACKING_FILTERS = {
  any: { label: "Any outcome", query: {} },
  not_applied: { label: "Not applied", query: { applied: false } },
  waiting: { label: "Applied, waiting", query: { applied: true, outcome: "pending" } },
  interviewing: { label: "Interview", query: { outcome: "interviewing" } },
  offer: { label: "Offer", query: { outcome: "offer" } },
  rejected: { label: "Rejected", query: { outcome: "rejected" } },
  no_response: { label: "No response", query: { outcome: "no_response" } },
} as const satisfies Record<string, { label: string; query: { applied?: boolean; outcome?: Outcome } }>;
export type TrackingFilter = keyof typeof TRACKING_FILTERS;

export function dueText(days: number): string {
  if (days === 0) return "due today";
  if (days > 0) return `${days} day${days === 1 ? "" : "s"} overdue`;
  return `in ${-days} day${days === -1 ? "" : "s"}`;
}

const base = (id: string) => `${env.apiPrefix}/applications/${encodeURIComponent(id)}/tracking`;

export const tracking = {
  get: (applicationId: string) => apiRequest<Tracking>(base(applicationId)),
  update: (applicationId: string, change: TrackingChange) =>
    apiRequest<Tracking>(base(applicationId), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(change),
    }),
  followUps: (on: string = localToday(), withinDays = 0) =>
    apiRequest<FollowUp[]>(`${env.apiPrefix}/follow-ups?on=${encodeURIComponent(on)}&within_days=${withinDays}`),
};
