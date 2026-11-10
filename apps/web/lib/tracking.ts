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

export interface Tracking {
  application_id: string;
  status: string;
  outcome: Outcome;
  applied_on: string | null;
  follow_up_on: string | null;
  notes: string | null;
  submitted_at: string | null;
  outcome_updated_at: string | null;
}

export type TrackingChange = Partial<Pick<Tracking, "outcome" | "applied_on" | "follow_up_on" | "notes">>;

export interface FollowUp {
  application_id: string;
  job_title: string;
  company_name: string | null;
  outcome: Outcome;
  applied_on: string | null;
  follow_up_on: string;
  days_overdue: number;
}

/** The browser's local date as YYYY-MM-DD (not UTC). */
export function localToday(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

export function trackingLabel(t: Pick<Tracking, "outcome" | "applied_on">): string {
  return t.outcome === "pending" && !t.applied_on ? "Not applied yet" : OUTCOME_LABEL[t.outcome];
}

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
