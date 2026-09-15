/**
 * Resume Studio helpers.
 *
 * Pure functions only — no fetching, no React. Everything the studio does to
 * a resume payload (grouping, ordering, labelling) lives here so it can be
 * tested without a browser or a backend.
 */

import type {
  ApiClaim,
  ApiResume,
  ApiResumeReview,
  ApplicationStatus,
  ClaimVerdict,
  ReviewStatus,
  VerificationStatus,
} from "./api";
import type { StatusTone } from "@/components/StatusBadge";

/**
 * Backend contracts Resume Studio would need but which do not exist yet.
 *
 * Kept here, countable, in the same spirit as lib/jobs.ts BACKEND_GAPS.
 * Nothing below is a bug — these are features that have not been built.
 */
export const RESUME_GAPS = [
  {
    id: "fact-text",
    needs: "GET /api/v1/facts/{id} batch lookup, or fact text on ClaimRead",
    blocks: "Showing the evidence sentence behind a claim, not just its id",
    note: "Claims carry fact_ids; nothing resolves them to prose yet.",
  },
  {
    id: "ats",
    needs: "A real ATS/keyword-coverage model",
    blocks: "An ATS score",
    note: "No scoring algorithm exists, so the panel reports verification facts only.",
  },
  {
    id: "rendering",
    needs: "The rendering worker (resume.render.requested is emitted, nothing consumes it)",
    blocks: "PDF and DOCX download",
    note: "B6 requests rendering; the renderer is a later phase.",
  },
] as const;

/** Sections in the order a resume reads. Mirrors the API's SECTION_ORDER. */
export const SECTION_ORDER = [
  "summary",
  "experience",
  "projects",
  "skills",
  "certifications",
  "education",
] as const;

export const SECTION_LABEL: Record<string, string> = {
  summary: "Professional Summary",
  experience: "Experience",
  projects: "Projects",
  skills: "Skills",
  certifications: "Certifications",
  education: "Education",
};

export const VERDICT_LABEL: Record<ClaimVerdict, string> = {
  supported: "Supported",
  overreach: "Overreach",
  unsupported: "Unsupported",
  not_checked: "Not checked",
};

/**
 * Verdict tones.
 *
 * `overreach` is review-toned and `unsupported` is risk-toned: a claim that
 * stretches the evidence and a claim that invents it are different problems
 * and should not look identical. Neither is sensationalised — the badge
 * always carries its label as text.
 */
export const VERDICT_TONE: Record<ClaimVerdict, StatusTone> = {
  supported: "strong",
  overreach: "review",
  unsupported: "risk",
  not_checked: "neutral",
};

export const VERIFICATION_LABEL: Record<VerificationStatus, string> = {
  pending: "Verification pending",
  passed: "Verified",
  failed: "Verification failed",
};

export const VERIFICATION_TONE: Record<VerificationStatus, StatusTone> = {
  pending: "neutral",
  passed: "strong",
  failed: "risk",
};

export function applicationLabel(status: ApplicationStatus | null): string {
  if (!status) return "Unknown";
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function applicationTone(status: ApplicationStatus | null): StatusTone {
  switch (status) {
    case "resume_ready":
      return "brass";
    case "verification_failed":
    case "policy_blocked":
    case "error":
      return "risk";
    case "awaiting_approval":
    case "changes_requested":
      return "review";
    case "approved":
    case "submitted":
      return "strong";
    default:
      return "neutral";
  }
}

export interface ResumeSection {
  key: string;
  label: string;
  claims: ApiClaim[];
}

function sectionRank(section: string): number {
  const index = (SECTION_ORDER as readonly string[]).indexOf(section);
  return index === -1 ? SECTION_ORDER.length : index;
}

function pathIndex(path: string): number {
  const tail = path.split("/").pop() ?? "";
  const parsed = Number.parseInt(tail, 10);
  return Number.isNaN(parsed) ? 0 : parsed;
}

/**
 * Group claims into ordered sections.
 *
 * Ordering is deterministic and comes from the claim's own section and path,
 * never from the order rows arrived in. An unrecognised section still renders,
 * sorted after the known ones, rather than disappearing.
 */
export function groupSections(claims: ApiClaim[]): ResumeSection[] {
  const groups = new Map<string, ApiClaim[]>();
  for (const claim of claims) {
    const existing = groups.get(claim.section);
    if (existing) existing.push(claim);
    else groups.set(claim.section, [claim]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => sectionRank(a) - sectionRank(b) || a.localeCompare(b))
    .map(([key, items]) => ({
      key,
      label: SECTION_LABEL[key] ?? key.replace(/\b\w/g, (c) => c.toUpperCase()),
      claims: [...items].sort(
        (x, y) => pathIndex(x.path) - pathIndex(y.path) || x.path.localeCompare(y.path),
      ),
    }));
}

/** Claims that need a human's attention, in display order. */
export function failedClaims(claims: ApiClaim[]): ApiClaim[] {
  return groupSections(claims)
    .flatMap((section) => section.claims)
    .filter((claim) => claim.verdict === "overreach" || claim.verdict === "unsupported");
}

export function isVerified(resume: Pick<ApiResume, "verification_status">): boolean {
  return resume.verification_status === "passed";
}

/** "2 of 5 claims need review" — or null when there is nothing wrong. */
export function reviewSummary(resume: Pick<ApiResume, "counts">): string | null {
  const failing = resume.counts.overreach + resume.counts.unsupported;
  if (failing === 0) return null;
  const noun = failing === 1 ? "claim needs" : "claims need";
  return `${failing} of ${resume.counts.total} ${noun} review`;
}

export function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ---------------------------------------------------------------------------
// Review (C2)
// ---------------------------------------------------------------------------

export const REVIEW_LABEL: Record<ReviewStatus, string> = {
  not_started: "Not reviewed",
  in_review: "Awaiting your approval",
  approved: "Approved",
  changes_requested: "Changes requested",
};

export const REVIEW_TONE: Record<ReviewStatus, StatusTone> = {
  not_started: "neutral",
  in_review: "review",
  approved: "strong",
  changes_requested: "review",
};

/**
 * Whether any review action is on offer.
 *
 * Reads the server's booleans rather than re-deriving them from the
 * application status. The API is the authority on what it will accept, and a
 * second copy of the rule in the browser is a second copy to get wrong.
 */
export function hasReviewAction(review: ApiResumeReview | null): boolean {
  if (!review) return false;
  return review.can_start_review || review.can_approve || review.can_request_changes;
}

/** A note is required to request changes; blank whitespace does not count. */
export function isValidChangeNote(note: string): boolean {
  return note.trim().length > 0;
}

/** "Approved by ada@example.com on 14 Sep 2026, 10:00" — or null. */
export function decisionLine(review: ApiResumeReview | null): string | null {
  if (!review || !review.decided_at) return null;
  const verb = review.approval_status === "approved" ? "Approved" : "Changes requested";
  const who = review.decided_by_email ?? "a reviewer";
  return `${verb} by ${who} on ${formatTimestamp(review.decided_at)}`;
}

/** The header line: "Senior Cloud Engineer · Acme" with either part missing. */
export function jobLine(
  resume: Pick<ApiResume, "job_title" | "job_company">,
): string {
  return [resume.job_title, resume.job_company].filter(Boolean).join(" · ") || "Untitled role";
}
