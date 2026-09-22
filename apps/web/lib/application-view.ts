import type { ApiApplication, ApplicationStatus } from "./api";
import type { StatusTone } from "@/components/StatusBadge";

export type ApplicationGroup = "All" | "Needs attention" | "Preparing" | "Ready" | "Submitted" | "Closed";

export const APPLICATION_GROUPS: ApplicationGroup[] = ["All", "Needs attention", "Preparing", "Ready", "Submitted", "Closed"];

const ATTENTION = new Set<ApplicationStatus>(["awaiting_approval", "changes_requested", "verification_failed", "submission_failed", "manual_handoff", "policy_blocked", "error"]);
const PREPARING = new Set<ApplicationStatus>(["created", "resume_generating", "questions_pending", "submitting"]);
const READY = new Set<ApplicationStatus>(["resume_ready", "approved", "submission_ready"]);
const CLOSED = new Set<ApplicationStatus>(["abandoned"]);

export function applicationGroup(status: ApplicationStatus): ApplicationGroup {
  if (ATTENTION.has(status)) return "Needs attention";
  if (PREPARING.has(status)) return "Preparing";
  if (READY.has(status)) return "Ready";
  if (CLOSED.has(status)) return "Closed";
  return "Submitted";
}

export function applicationStatus(status: ApplicationStatus): { label: string; tone: StatusTone } {
  const labels: Partial<Record<ApplicationStatus, string>> = {
    created: "Saved", resume_generating: "Preparing resume", resume_ready: "Resume ready",
    verification_failed: "Verification needs attention", questions_pending: "Questions pending",
    awaiting_approval: "Needs your review", changes_requested: "Changes requested",
    approved: "Approved", submission_ready: "Ready to submit", submitting: "Submitting",
    submitted: "Applied", submission_failed: "Submission failed", manual_handoff: "Manual step needed",
    policy_blocked: "Action blocked", abandoned: "Closed", error: "Needs attention",
  };
  const group = applicationGroup(status);
  const tone: StatusTone = group === "Needs attention" ? "review" : group === "Submitted" ? "strong" : group === "Ready" ? "brass" : "neutral";
  return { label: labels[status] ?? status, tone };
}

export function filterApplications(items: ApiApplication[], query: string, group: ApplicationGroup, sort: "recent" | "oldest" | "company"): ApiApplication[] {
  const needle = query.trim().toLocaleLowerCase();
  return items.filter(item => {
    if (group !== "All" && applicationGroup(item.status) !== group) return false;
    return !needle || [item.job_title, item.job_company, item.status].some(value => value?.toLocaleLowerCase().includes(needle));
  }).sort((a, b) => sort === "company"
    ? (a.job_company ?? "").localeCompare(b.job_company ?? "")
    : sort === "oldest" ? Date.parse(a.updated_at) - Date.parse(b.updated_at) : Date.parse(b.updated_at) - Date.parse(a.updated_at));
}

export function nextAction(status: ApplicationStatus, hasResume: boolean): { label: string; href: "resume" | "detail" } {
  if (status === "awaiting_approval" || status === "resume_ready" || status === "changes_requested" || status === "verification_failed") {
    return { label: "Review resume", href: "resume" };
  }
  return hasResume ? { label: "View details", href: "detail" } : { label: "View application", href: "detail" };
}
