/**
 * ============================================================================
 * DEMO DATA — NOT REAL, NOT FROM THE API.
 * ============================================================================
 *
 * Every export here is placeholder content for the dashboard shell. The
 * backend does not yet expose job, evaluation or activity endpoints, and this
 * increment deliberately does not invent any: adding a `/api/v1/jobs/feed`
 * client call for an endpoint that does not exist would create a contract the
 * backend never agreed to.
 *
 * Replacing this file is the whole migration path. Every consumer imports
 * from here and nowhere else, and each shape below mirrors the fields the
 * real endpoints already persist (jobs, job_evaluations, events), so the
 * swap is a data-source change rather than a component rewrite.
 *
 * Companies are fictional and use example.invalid, matching the convention
 * in the backend test fixtures.
 */

export const IS_DEMO_DATA = true;

export type DemoJob = {
  id: string;
  role: string;
  company: string;
  location: string;
  remote: boolean;
  fit: number;
  status: string;
  foundAgo: string;
  matched: number;
  missing: number;
  topGap?: string;
};

export const DEMO_JOBS: DemoJob[] = [
  {
    id: "j-1", role: "Senior Cloud Engineer", company: "Northgate Systems",
    location: "Remote — US", remote: true, fit: 87, status: "Shortlisted",
    foundAgo: "2h ago", matched: 7, missing: 1, topGap: "Kubernetes at scale",
  },
  {
    id: "j-2", role: "AWS Infrastructure Engineer", company: "Halden Logistics",
    location: "Fort Worth, TX", remote: false, fit: 81, status: "Resume ready",
    foundAgo: "4h ago", matched: 6, missing: 2, topGap: "Terraform modules",
  },
  {
    id: "j-3", role: "DevOps Engineer", company: "Corveth Health",
    location: "Remote — US", remote: true, fit: 76, status: "Shortlisted",
    foundAgo: "6h ago", matched: 6, missing: 2, topGap: "CI/CD ownership",
  },
  {
    id: "j-4", role: "Platform Engineer", company: "Ambleside Data",
    location: "Dallas, TX", remote: false, fit: 68, status: "Awaiting approval",
    foundAgo: "9h ago", matched: 5, missing: 3, topGap: "Service mesh",
  },
  {
    id: "j-5", role: "Cloud Operations Engineer", company: "Ridgeline Freight",
    location: "Remote — US", remote: true, fit: 62, status: "Evaluated",
    foundAgo: "1d ago", matched: 4, missing: 3, topGap: "On-call rotation",
  },
];

export type DemoStage = {
  key: string;
  label: string;
  count: number;
  note?: string;
};

/** Mirrors the documented job/application lifecycle. */
export const DEMO_PIPELINE: DemoStage[] = [
  { key: "discovered", label: "Discovered", count: 214 },
  { key: "evaluated", label: "Evaluated", count: 96 },
  { key: "shortlisted", label: "Shortlisted", count: 31 },
  { key: "resume", label: "Resume ready", count: 12 },
  { key: "approval", label: "Approval", count: 4, note: "waiting on you" },
  { key: "applied", label: "Applied", count: 47 },
  { key: "interview", label: "Interview", count: 3 },
];

export type DemoActivity = {
  id: string;
  action: string;
  detail: string;
  provenance: "deterministic" | "ai-assisted" | "human";
  at: string;
};

export const DEMO_ACTIVITY: DemoActivity[] = [
  {
    id: "a-1", action: "Job requirements read",
    detail: "Read 8 things the Northgate Systems ad asks for — 4 essential, 4 preferred",
    provenance: "deterministic", at: "12 min ago",
  },
  {
    id: "a-2", action: "Fit score worked out",
    detail: "Senior Cloud Engineer scored 87 — you cover 7 of the 8 things asked for",
    provenance: "deterministic", at: "12 min ago",
  },
  {
    id: "a-3", action: "Security scan completed",
    detail: "Checked 18 job ads for hidden instructions — 1 needs your eyes",
    provenance: "deterministic", at: "38 min ago",
  },
  {
    id: "a-4", action: "Match saved",
    detail: "Halden Logistics moved to your shortlist",
    provenance: "deterministic", at: "1h ago",
  },
  {
    id: "a-5", action: "Resume approved",
    detail: "You approved the resume for Corveth Health",
    provenance: "human", at: "3h ago",
  },
];

export type DemoAttention = {
  id: string;
  kind: "review" | "risk" | "info";
  title: string;
  body: string;
  cta: string;
};

export const DEMO_ATTENTION: DemoAttention[] = [
  {
    id: "r-1", kind: "review",
    title: "4 applications waiting for your approval",
    body: "Nothing goes out until you approve it. The oldest has been waiting two days.",
    cta: "Review them now",
  },
  {
    id: "r-2", kind: "risk",
    title: "A posting tried to change how it was read",
    body: "The Ridgeline Freight ad contains hidden text aimed at changing how it is read. JobForge ignored it and flagged the job for you.",
    cta: "See what was flagged",
  },
  {
    id: "r-3", kind: "info",
    title: "3 facts about you are not confirmed yet",
    body: "Only facts you have confirmed can appear on a resume. Confirming these gives JobForge more to work with.",
    cta: "Confirm these facts",
  },
];

export const DEMO_FIT_DISTRIBUTION = [
  { band: "Strong", range: "75–100", count: 31, tone: "strong" as const },
  { band: "Moderate", range: "60–74", count: 42, tone: "moderate" as const },
  { band: "Low", range: "Below 60", count: 23, tone: "low" as const },
];

export const DEMO_METRICS = {
  discovered: 214,
  strongMatches: 31,
  awaitingApproval: 4,
  applicationsSent: 47,
  dailyQuota: 100,
};
