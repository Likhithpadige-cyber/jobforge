import type { IconName } from "@/components/Icon";
import type { ApiApplication } from "./api";
import { applicationGroup, type ApplicationGroup } from "./application-view";

/** Counts the dashboard reads from the API. `null` means "could not be read". */
export type DashboardSignals = {
  isOwner: boolean;
  pendingApprovals: number | null;
  /** Applications whose last submission did not complete. Owner attention first. */
  submissionProblems: number;
  readyToSubmit: number;
  draftFacts: number | null;
  jobsFound: number | null;
  applications: number;
};

export type DashboardPriority = {
  eyebrow: string;
  title: string;
  body: string;
  href: string;
  action: string;
  icon: IconName;
};

const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;

/**
 * The single most useful next action, from real counts only.
 *
 * Unknown counts never produce a claim: if the approvals inbox could not be
 * read, the dashboard says so instead of implying nothing is waiting.
 */
export function getDashboardPriority(signals: DashboardSignals): DashboardPriority {
  if (signals.submissionProblems > 0) return {
    eyebrow: "Needs your attention", title: `${plural(signals.submissionProblems, "submission", "submissions")} did not complete`,
    body: "Check what happened before doing anything else. Ambiguous outcomes are never retried automatically.",
    href: "/applications", action: "See applications", icon: "applications",
  };
  if (signals.pendingApprovals === null) return {
    eyebrow: "Could not check", title: "Your approvals could not be loaded",
    body: "Open the approvals inbox to see what is waiting for your decision.",
    href: "/approvals", action: "Open approvals", icon: "approvals",
  };
  if (signals.pendingApprovals > 0) return {
    eyebrow: "Needs your attention", title: `${plural(signals.pendingApprovals, "item is", "items are")} waiting for your review`,
    body: "Nothing leaves JobForge until a person approves it.",
    href: "/approvals", action: "Review now", icon: "approvals",
  };
  if (signals.readyToSubmit > 0) return signals.isOwner ? {
    eyebrow: "Ready", title: `${plural(signals.readyToSubmit, "application is", "applications are")} ready to submit`,
    body: "Every material is approved. Submitting still needs your explicit confirmation.",
    href: "/applications", action: "Open applications", icon: "applications",
  } : {
    eyebrow: "Waiting for the owner", title: `${plural(signals.readyToSubmit, "application is", "applications are")} ready to submit`,
    body: "All materials are approved. Only the workspace owner can submit.",
    href: "/applications", action: "Open applications", icon: "applications",
  };
  if (signals.draftFacts !== null && signals.draftFacts > 0) return {
    eyebrow: "Strengthen your profile", title: `${plural(signals.draftFacts, "fact is", "facts are")} waiting for verification`,
    body: "Only verified facts can appear in anything JobForge writes about you.",
    href: "/profile", action: "Verify facts", icon: "profile",
  };
  if (signals.jobsFound === 0) return {
    eyebrow: "Get started", title: "No jobs have been found yet",
    body: "Import a job or add a source so JobForge has roles to evaluate.",
    href: "/job-feed", action: "Open job feed", icon: "feed",
  };
  if (signals.applications === 0) return {
    eyebrow: "Build momentum", title: "Start your first application",
    body: "Pick a role from the job feed that fits your verified experience.",
    href: "/job-feed", action: "Explore jobs", icon: "feed",
  };
  return {
    eyebrow: "All caught up", title: "Nothing needs your decision right now",
    body: "JobForge will bring anything that needs you to this page.",
    href: "/applications", action: "View applications", icon: "applications",
  };
}

export type PipelineStage = { key: string; label: string; count: number; note?: string };

const STAGES: { group: ApplicationGroup; label: string }[] = [
  { group: "Preparing", label: "Preparing" },
  { group: "Needs attention", label: "Needs attention" },
  { group: "Ready", label: "Ready" },
  { group: "Submitted", label: "Applied" },
  { group: "Closed", label: "Closed" },
];

/** Real application counts per lifecycle group, led by jobs found when known. */
export function pipelineStages(applications: ApiApplication[], jobsFound: number | null): PipelineStage[] {
  const counts = new Map<ApplicationGroup, number>();
  for (const item of applications) {
    const group = applicationGroup(item.status);
    counts.set(group, (counts.get(group) ?? 0) + 1);
  }
  const stages: PipelineStage[] = jobsFound === null ? [] : [{ key: "jobs", label: "Jobs found", count: jobsFound }];
  for (const stage of STAGES) {
    const count = counts.get(stage.group) ?? 0;
    stages.push({ key: stage.group, label: stage.label, count, note: stage.group === "Needs attention" && count > 0 ? "waiting on you" : undefined });
  }
  return stages;
}

const PROBLEMS = new Set(["submission_failed", "manual_handoff", "policy_blocked", "error"]);

export function submissionProblems(applications: ApiApplication[]): number {
  return applications.filter((item) => PROBLEMS.has(item.status)).length;
}
