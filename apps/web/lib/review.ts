/**
 * Review and submission client (M13 Phase 1).
 *
 * Mirrors the FastAPI contracts exactly:
 * apps/api/routers/review_status.py (inbox, readiness, history, cover-letter
 * content), cover_letter_review.py, applications.py (answers package, submit)
 * and settings.py. Every mutation goes through the shared transport, which
 * attaches the CSRF token. Nothing here decides anything: the server is the
 * authority for every state shown, and the UI re-reads it after each action.
 */

import { ApiError, apiRequest, type ApiClaim, type ApplicationStatus, type ApprovalStatus } from "./api";
import { env } from "./env";

const prefix = () => env.apiPrefix;
const app = (id: string) => `${prefix()}/applications/${encodeURIComponent(id)}`;
const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

// ------------------------------------------------------------------ inbox
export type ApprovalKind = "application" | "resume" | "answers" | "cover_letter" | "policy_exception";

export interface ApprovalInboxItem {
  approval_id: string;
  kind: ApprovalKind;
  status: ApprovalStatus;
  requested_at: string;
  decided_at: string | null;
  application_id: string;
  application_status: ApplicationStatus;
  job_title: string | null;
  job_company: string | null;
}

export interface ApprovalQuery {
  status?: ApprovalStatus;
  kind?: ApprovalKind;
  application_id?: string;
  limit?: number;
  offset?: number;
}

// ------------------------------------------------------------- readiness
export interface ReadinessCheck {
  key: string;
  passed: boolean;
  summary: string;
}

export interface SubmissionGate {
  submission_mode: "dry_run" | "enabled";
  persisted_enabled: boolean;
  live: boolean;
}

export interface SubmissionReadiness {
  application_id: string;
  application_status: ApplicationStatus;
  ready: boolean;
  can_submit: boolean;
  gate: SubmissionGate;
  destination_domain: string | null;
  attempt: number | null;
  checks: ReadinessCheck[];
}

export type SubmissionOutcome =
  | "pending"
  | "sent"
  | "preflight_blocked"
  | "ambiguous"
  | "retry_scheduled"
  | "failed"
  | "handoff_required"
  | "blocked_by_policy";

export interface SubmissionAttempt {
  submission_id: string;
  attempt: number;
  method: string;
  target_domain: string;
  result: string;
  outcome: SubmissionOutcome;
  smtp_status: number | null;
  started_at: string | null;
  finished_at: string | null;
  next_attempt_after: string | null;
}

export interface SubmitResponse {
  application_id: string;
  submission_id: string;
  status: string;
  attempt: number;
}

// ---------------------------------------------------------- cover letter
export type CoverLetterReviewStatus =
  | "not_started"
  | "in_review"
  | "approved"
  | "changes_requested"
  | "failed_verification";

export interface CoverLetterReview {
  application_id: string;
  application_status: ApplicationStatus;
  cover_letter_id: string | null;
  cover_letter_version: number | null;
  verification_status: string | null;
  review_status: CoverLetterReviewStatus;
  approval_id: string | null;
  approval_status: ApprovalStatus | null;
  approval_is_current: boolean;
  decision_note: string | null;
  changes_requested: string | null;
  requested_at: string | null;
  decided_at: string | null;
  decided_by: string | null;
  decided_by_email: string | null;
  can_approve: boolean;
  can_request_changes: boolean;
  blocked_reason: string | null;
}

export interface CoverLetterContent {
  cover_letter_id: string;
  application_id: string;
  version: number;
  verification_status: string;
  created_at: string;
  claims: ApiClaim[];
}

// ------------------------------------------------------- answers package
export type QuestionClass =
  | "factual_safe"
  | "derived"
  | "uncertain"
  | "judgement"
  | "sensitive"
  | "unanswerable";

export interface Answer {
  id: string;
  question_id: string;
  value: string | null;
  value_structured: Record<string, unknown>;
  source: "agent" | "human" | "derived" | "blank";
  fact_ids: string[];
  confidence: string | null;
  requires_human: boolean;
  answered_by: string | null;
  edited_from_agent_value: string | null;
}

export interface Question {
  id: string;
  question_text: string;
  input_type: string;
  options: Record<string, unknown>;
  is_required: boolean;
  max_length: number | null;
  display_order: number;
  question_class: QuestionClass;
  class_reason: string | null;
  answer: Answer | null;
}

export interface PackageDecision {
  approval_id: string;
  application_id: string;
  status: ApprovalStatus;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  changes_requested: string | null;
}

// -------------------------------------------------------------------- api
export const review = {
  approvals: (query: ApprovalQuery = {}) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== "") params.set(key, String(value));
    }
    const suffix = params.toString() ? `?${params}` : "";
    return apiRequest<ApprovalInboxItem[]>(`${prefix()}/approvals${suffix}`);
  },

  readiness: (applicationId: string) =>
    apiRequest<SubmissionReadiness>(`${app(applicationId)}/submission-readiness`),

  submissions: (applicationId: string) =>
    apiRequest<SubmissionAttempt[]>(`${app(applicationId)}/submissions`),

  /** Owner only, CSRF-protected. The body is empty: the server forbids fields. */
  submit: (applicationId: string) => apiRequest<SubmitResponse>(`${app(applicationId)}/submit`, json({})),

  coverLetterReview: (applicationId: string) =>
    apiRequest<CoverLetterReview>(`${app(applicationId)}/cover-letter-review`),

  coverLetter: (applicationId: string) =>
    apiRequest<CoverLetterContent>(`${app(applicationId)}/cover-letter`),

  approveCoverLetter: (applicationId: string, decisionNote?: string) =>
    apiRequest<CoverLetterReview>(
      `${app(applicationId)}/cover-letter-review/approve`,
      json({ decision_note: decisionNote?.trim() || null }),
    ),

  requestCoverLetterChanges: (applicationId: string, note: string) =>
    apiRequest<CoverLetterReview>(
      `${app(applicationId)}/cover-letter-review/request-changes`,
      json({ note }),
    ),

  questions: (applicationId: string) => apiRequest<Question[]>(`${app(applicationId)}/questions`),

  editAnswer: (applicationId: string, answerId: string, value: string | null) =>
    apiRequest<Answer>(`${app(applicationId)}/answers/${encodeURIComponent(answerId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    }),

  approvePackage: (applicationId: string, decisionNote?: string) =>
    apiRequest<PackageDecision>(
      `${app(applicationId)}/package/approve`,
      json({ decision_note: decisionNote?.trim() || null }),
    ),

  requestPackageChanges: (applicationId: string, note: string) =>
    apiRequest<PackageDecision>(`${app(applicationId)}/package/request-changes`, json({ note })),
};

// ---------------------------------------------------------------- display
export const APPROVAL_KIND_LABEL: Record<ApprovalKind, string> = {
  application: "Application",
  resume: "Resume",
  answers: "Application answers",
  cover_letter: "Cover letter",
  policy_exception: "Policy exception",
};

/** Where a reviewer goes to decide an approval of this kind. */
export function approvalHref(item: Pick<ApprovalInboxItem, "kind" | "application_id">): string {
  const base = `/applications/${item.application_id}`;
  switch (item.kind) {
    case "resume":
      return `/resume-studio/${item.application_id}`;
    case "cover_letter":
      return `${base}/cover-letter`;
    case "answers":
      return `${base}/answers`;
    default:
      return base;
  }
}

export const OUTCOME_LABEL: Record<SubmissionOutcome, string> = {
  pending: "Not sent yet",
  sent: "Sent",
  preflight_blocked: "Blocked before sending",
  ambiguous: "Outcome unknown — needs your review",
  retry_scheduled: "Temporary failure — retry scheduled",
  failed: "Failed",
  handoff_required: "Needs manual handoff",
  blocked_by_policy: "Blocked by site policy",
};

export const OUTCOME_HELP: Partial<Record<SubmissionOutcome, string>> = {
  ambiguous:
    "The mail server's response was lost, so JobForge cannot know whether it was delivered. It is never retried automatically; check the destination before doing anything else.",
  retry_scheduled: "A bounded automatic retry is scheduled for this transient failure.",
  preflight_blocked: "A safety check failed at send time, so nothing was sent.",
};

export function outcomeTone(outcome: SubmissionOutcome): "strong" | "review" | "risk" | "neutral" | "info" {
  if (outcome === "sent") return "strong";
  if (outcome === "ambiguous" || outcome === "failed" || outcome === "blocked_by_policy") return "risk";
  if (outcome === "retry_scheduled" || outcome === "handoff_required" || outcome === "preflight_blocked") return "review";
  return "info";
}

/** Plain-language mode label; dry-run never sends. */
export function gateLabel(gate: SubmissionGate): string {
  if (gate.live) return "Live submission is on: Submit sends a real email.";
  if (gate.submission_mode === "dry_run") {
    return "Dry-run mode: this deployment never sends email. Submit is unavailable.";
  }
  return "The submission switch is off: Submit is unavailable until an owner turns it on.";
}

// ----------------------------------------------------------------- errors
export type ReviewError = {
  kind: "forbidden" | "not_found" | "conflict" | "invalid" | "rate_limited" | "failed";
  message: string;
};

/** The server's own problem `detail` where it is a plain sentence, else a fixed message. */
export function reviewError(cause: unknown): ReviewError {
  if (!(cause instanceof ApiError)) {
    return { kind: "failed", message: "Something went wrong. Please try again." };
  }
  const detail =
    typeof cause.detail === "object" && cause.detail !== null && "detail" in cause.detail
      ? String((cause.detail as { detail: unknown }).detail)
      : null;
  switch (cause.status) {
    case 0:
      return { kind: "failed", message: "Cannot reach the JobForge API." };
    case 401:
    case 403:
      return { kind: "forbidden", message: "Your account does not have permission for this." };
    case 404:
      return { kind: "not_found", message: detail ?? "This record does not exist." };
    case 409:
      return { kind: "conflict", message: detail ?? "The state changed. Reload and try again." };
    case 422:
      return { kind: "invalid", message: detail ?? "Some of the information is not valid." };
    case 429:
      return { kind: "rate_limited", message: "Too many requests. Wait a moment and try again." };
    default:
      return { kind: "failed", message: `The API returned an error (${cause.status}).` };
  }
}
