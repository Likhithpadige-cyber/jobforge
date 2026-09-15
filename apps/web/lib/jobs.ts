/**
 * Job helpers.
 *
 * Pure functions only — no fetching, no React. Everything the Job Feed does
 * to a list (search, filter, sort) lives here so it can be tested without a
 * browser or a backend.
 *
 * The API supports `status`, `limit` and `offset` and nothing else, so all
 * other filtering happens client-side over the loaded page. The UI states
 * that plainly rather than implying server-side search it does not have.
 */

import type { ApiJob, ApiRequirement, JobStatus } from "./api";

/**
 * Backend contracts this screen would need but which do not exist yet.
 *
 * Listed here rather than scattered through components so the gap set is
 * countable, and so the "not wired yet" notices in the UI can be driven from
 * one place. Nothing below is a bug — these are simply features that have
 * not been built.
 */
export const BACKEND_GAPS = [
  {
    id: "evaluation",
    needs: "GET /api/v1/jobs/{id}/evaluation",
    blocks: "Fit score, match matrix, gaps, recommendation",
    note: "job_evaluations rows are written by A5 but no endpoint reads them.",
  },
  {
    id: "evidence",
    needs: "Requirement-to-fact mapping in the evaluation response",
    blocks: "Which verified facts support each matched requirement",
    note: "A2 (match matrix) is not implemented, so this data does not exist yet.",
  },
  {
    id: "company",
    needs: "company name on JobRead, or GET /api/v1/companies",
    blocks: "Company column and detail header",
    note: "Jobs carry company_id only, and there is no companies endpoint.",
  },
  {
    id: "security",
    needs: "Injection-scan findings on the job response",
    blocks: "Security flag in the feed and the detail panel",
    note: "G1 runs but its findings are not persisted against the job.",
  },
  {
    id: "query",
    needs: "search / sort / location / source query parameters on GET /jobs",
    blocks: "Server-side search, sort and filtering",
    note: "Only status, limit and offset are supported today.",
  },
] as const;

export const WORK_MODES = ["remote", "hybrid", "onsite"] as const;
export type WorkMode = (typeof WORK_MODES)[number];

export function workMode(job: ApiJob): WorkMode {
  if (job.is_hybrid) return "hybrid";
  if (job.is_remote) return "remote";
  return "onsite";
}

export const WORK_MODE_LABEL: Record<WorkMode, string> = {
  remote: "Remote",
  hybrid: "Hybrid",
  onsite: "On-site",
};

/** Human location, or "Remote" when there is nothing else to say. */
export function formatLocation(job: ApiJob): string {
  const parts = [job.location_city, job.location_state].filter(Boolean);
  if (parts.length > 0) return parts.join(", ");
  if (job.location_country) return job.location_country;
  return job.is_remote ? "Remote" : "Location not given";
}

export function formatSalary(job: ApiJob): string | null {
  if (job.salary_min === null && job.salary_max === null) return null;
  const symbol = job.salary_currency === "GBP" ? "£" : job.salary_currency === "EUR" ? "€" : "$";
  const round = (n: number) => (n >= 10_000 ? `${Math.round(n / 1000)}k` : String(n));
  const period = job.salary_period === "hourly" ? "/hr" : job.salary_period === "monthly" ? "/mo" : "";
  if (job.salary_min !== null && job.salary_max !== null) {
    return `${symbol}${round(job.salary_min)}–${symbol}${round(job.salary_max)}${period}`;
  }
  const single = job.salary_max ?? job.salary_min;
  return single === null ? null : `${symbol}${round(single)}${period}`;
}

/** "4h ago" / "3d ago". Deliberately coarse: precision here is noise. */
export function relativeTime(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const minutes = Math.max(0, Math.round((now.getTime() - then) / 60000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return `${Math.round(days / 30)}mo ago`;
}

export const STATUS_LABEL: Record<string, string> = {
  discovered: "Discovered",
  normalized: "Ready to score",
  evaluated: "Needs your call",
  shortlisted: "Shortlisted",
  queued: "Queued",
  claimed: "In progress",
  applied: "Applied",
  discarded: "Set aside",
  duplicate: "Duplicate",
  expired: "Expired",
};

/** Statuses where the documented lifecycle is waiting on a person. */
export const HUMAN_DECISION_STATUSES: JobStatus[] = ["evaluated"];

export function needsHumanDecision(job: ApiJob): boolean {
  return HUMAN_DECISION_STATUSES.includes(job.status);
}

// --------------------------------------------------------------- filtering
export type SortKey = "best-fit" | "newest" | "company" | "status";

export interface FeedFilters {
  search: string;
  status: JobStatus | "all";
  mode: WorkMode | "all";
  sourceId: string | "all";
  sort: SortKey;
}

export const DEFAULT_FILTERS: FeedFilters = {
  search: "",
  status: "all",
  mode: "all",
  sourceId: "all",
  sort: "best-fit",
};

export function filtersAreDefault(filters: FeedFilters): boolean {
  return (
    filters.search === DEFAULT_FILTERS.search &&
    filters.status === DEFAULT_FILTERS.status &&
    filters.mode === DEFAULT_FILTERS.mode &&
    filters.sourceId === DEFAULT_FILTERS.sourceId &&
    filters.sort === DEFAULT_FILTERS.sort
  );
}

export function matchesSearch(job: ApiJob, term: string): boolean {
  const needle = term.trim().toLowerCase();
  if (!needle) return true;
  const haystack = [
    job.title,
    job.normalized_title ?? "",
    formatLocation(job),
    job.seniority ?? "",
    job.employment_type ?? "",
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(needle);
}

/**
 * Ordering.
 *
 * "Best fit" cannot yet order by fit, because no fit score is exposed. It
 * falls back to the pipeline's own ranking - priority, then how recently the
 * job was seen - and the UI labels it "Best available" so the name does not
 * promise something the data cannot deliver.
 */
export function sortJobs(jobs: ApiJob[], sort: SortKey): ApiJob[] {
  const copy = [...jobs];
  switch (sort) {
    case "newest":
      return copy.sort(
        (a, b) =>
          new Date(b.posted_at ?? b.first_seen_at).getTime() -
          new Date(a.posted_at ?? a.first_seen_at).getTime(),
      );
    case "company":
      return copy.sort((a, b) => a.title.localeCompare(b.title));
    case "status":
      return copy.sort((a, b) => a.status.localeCompare(b.status));
    case "best-fit":
    default:
      return copy.sort((a, b) => {
        if (b.priority !== a.priority) return b.priority - a.priority;
        return (
          new Date(b.first_seen_at).getTime() - new Date(a.first_seen_at).getTime()
        );
      });
  }
}

export function applyFilters(jobs: ApiJob[], filters: FeedFilters): ApiJob[] {
  const filtered = jobs.filter((job) => {
    if (!matchesSearch(job, filters.search)) return false;
    if (filters.status !== "all" && job.status !== filters.status) return false;
    if (filters.mode !== "all" && workMode(job) !== filters.mode) return false;
    if (filters.sourceId !== "all" && job.source_id !== filters.sourceId) return false;
    return true;
  });
  return sortJobs(filtered, filters.sort);
}

// ------------------------------------------------------------ requirements
export function splitRequirements(requirements: ApiRequirement[] | undefined) {
  const list = requirements ?? [];
  return {
    required: list.filter((r) => r.necessity === "required"),
    preferred: list.filter((r) => r.necessity === "preferred"),
  };
}

export function formatYears(req: ApiRequirement): string | null {
  if (req.years_min === null) return null;
  if (req.years_max !== null && req.years_max !== req.years_min) {
    return `${req.years_min}–${req.years_max} years`;
  }
  return `${req.years_min}+ years`;
}

export const REQUIREMENT_KIND_LABEL: Record<string, string> = {
  skill: "Skill",
  certification: "Certification",
  education: "Education",
  experience: "Experience",
  responsibility: "Responsibility",
};
