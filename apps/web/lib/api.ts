/**
 * Minimal typed API client.
 *
 * Phase 1 only needs the health endpoints. Once the API surface exists,
 * `npm run gen:types` regenerates types/api.ts from the OpenAPI schema so
 * the frontend contract cannot drift from the backend (ARCHITECTURE.md §8).
 */

import { env } from "./env";

export type HealthState = "ok" | "degraded" | "unavailable";

export interface DependencyHealth {
  status: HealthState;
  latency_ms: number | null;
  error: string | null;
}

export interface Liveness {
  status: "ok";
  service: string;
  version: string;
}

export interface Readiness {
  status: HealthState;
  service: string;
  version: string;
  environment: string;
  timestamp: string;
  uptime_seconds: number;
  checks: Record<string, DependencyHealth>;
  migration_revision: string | null;
}


// ---------------------------------------------------------------------------
// Jobs
//
// These mirror the FastAPI response models exactly (apps/api/schemas/jobs.py).
// Nothing here is invented: every field below is one the backend already
// returns, and every call maps to an endpoint that already exists.
//
// Note what is ABSENT, because it shapes the UI: there is no fit score, no
// evaluation, no match matrix, no evidence, no security-scan flag, and no
// company name. See lib/jobs.ts BACKEND_GAPS.
// ---------------------------------------------------------------------------

export type JobStatus =
  | "discovered" | "normalized" | "evaluated" | "shortlisted" | "queued"
  | "claimed" | "applied" | "discarded" | "duplicate" | "expired";

export interface ApiJob {
  id: string;
  source_id: string;
  company_id: string | null;
  external_id: string | null;
  url: string;
  title: string;
  normalized_title: string | null;
  seniority: string | null;
  location_city: string | null;
  location_state: string | null;
  location_country: string | null;
  is_remote: boolean;
  is_hybrid: boolean;
  employment_type: string | null;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  salary_period: string | null;
  status: JobStatus;
  discard_reason: string | null;
  duplicate_of_id: string | null;
  priority: number;
  posted_at: string | null;
  first_seen_at: string;
  last_seen_at: string;
}

/** A1 output, as stored on jobs.requirements. */
export interface ApiRequirement {
  text: string;
  necessity: "required" | "preferred";
  kind: "skill" | "certification" | "education" | "experience" | "responsibility";
  years_min: number | null;
  years_max: number | null;
  section: string | null;
}

export interface ApiRequirements {
  requirements?: ApiRequirement[];
  confidence?: number;
  needs_llm_fallback?: boolean;
  counts?: { total: number; required: number; preferred: number };
  warnings?: string[];
}

export interface ApiJobDetail extends ApiJob {
  description_raw: string | null;
  description_hash: string | null;
  requirements: ApiRequirements;
}

export interface ApiSource {
  id: string;
  name: string;
  slug: string;
  domain: string;
  enabled: boolean;
}

export interface JobStatusCounts {
  counts: Record<string, number>;
  total: number;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Name of the readable CSRF cookie the API sets at login (double submit). */
export const CSRF_COOKIE = "jobforge_csrf";
export const CSRF_HEADER = "X-CSRF-Token";

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/**
 * Read the CSRF cookie.
 *
 * Deliberately readable: the session cookie is HttpOnly and does the
 * authenticating, while this one exists only to be echoed back in a header so
 * the server can confirm the request came from our own page.
 */
export function readCsrfToken(): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie
    .split("; ")
    .find((row) => row.startsWith(`${CSRF_COOKIE}=`));
  return match ? decodeURIComponent(match.slice(CSRF_COOKIE.length + 1)) : null;
}

interface RequestOptions {
  /**
   * Only the readiness probe opts in: its 503 body says which dependency is
   * down. Everywhere else a 503 is an error, never data.
   */
  allowServiceUnavailable?: boolean;
}

async function request<T>(path: string, init?: RequestInit, options: RequestOptions = {}): Promise<T> {
  // In local development, use the Next.js same-origin API proxy. This keeps
  // browser requests on the current host and port, including when dev runs on
  // a port other than the API's configured CORS origin.
  const baseUrl = process.env.NODE_ENV === "development" ? "" : env.apiBaseUrl;
  const url = `${baseUrl}${path}`;
  const method = (init?.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...((init?.headers as Record<string, string>) ?? {}),
  };
  // Mutating requests carry the CSRF token; reads never need it.
  if (!SAFE_METHODS.has(method)) {
    const token = readCsrfToken();
    if (token) headers[CSRF_HEADER] = token;
  }
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers,
      // Send the session cookie; the API is a different origin in dev.
      credentials: "include",
      cache: "no-store",
    });
  } catch (cause) {
    throw new ApiError(`Cannot reach the API at ${env.apiBaseUrl}`, 0, cause);
  }

  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      // A proxy error page is not JSON; it must still surface as ApiError.
      if (response.ok) throw new ApiError("Malformed API response", response.status);
      body = null;
    }
  }

  // 503 from the readiness endpoint still carries a useful body, so that one
  // caller may receive it; every other caller gets an ApiError.
  if (!response.ok && !(response.status === 503 && options.allowServiceUnavailable)) {
    throw new ApiError(`Request failed: ${response.status}`, response.status, body);
  }
  return body as T;
}

/** Shared transport for feature clients such as lib/operations.ts. */
export const apiRequest = request;

/** The server's own explanation from an RFC 7807 body, else a fallback. */
export function apiErrorText(cause: unknown, fallback: string): string {
  if (cause instanceof ApiError) {
    const body = cause.detail;
    if (typeof body === "object" && body !== null && typeof (body as { detail?: unknown }).detail === "string") {
      return (body as { detail: string }).detail;
    }
    return cause.message;
  }
  return cause instanceof Error ? cause.message : fallback;
}

/**
 * GET an artifact's bytes. The server re-checks ownership, approval, current
 * evidence and the stored checksum before answering; this never bypasses it.
 */
export async function downloadArtifactBytes(
  artifactId: string,
): Promise<{ bytes: ArrayBuffer; filename: string | null; contentType: string }> {
  const baseUrl = process.env.NODE_ENV === "development" ? "" : env.apiBaseUrl;
  let response: Response;
  try {
    response = await fetch(`${baseUrl}${env.apiPrefix}/artifacts/${artifactId}/content`, {
      credentials: "include",
      cache: "no-store",
    });
  } catch (cause) {
    throw new ApiError(`Cannot reach the API at ${env.apiBaseUrl}`, 0, cause);
  }
  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    throw new ApiError(`Request failed: ${response.status}`, response.status, body);
  }
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const match = /filename="([^"]+)"/.exec(disposition);
  return {
    bytes: await response.arrayBuffer(),
    filename: match?.[1] ?? null,
    contentType: response.headers.get("Content-Type") ?? "application/octet-stream",
  };
}

export const api = {
  liveness: () => request<Liveness>("/health"),
  readiness: () =>
    request<Readiness>(`${env.apiPrefix}/health`, undefined, { allowServiceUnavailable: true }),

  /**
   * GET /api/v1/jobs
   *
   * The endpoint supports `status`, `limit` and `offset` only. Search,
   * sorting and every other filter are applied client-side over what has
   * been loaded, and the UI says so rather than implying server-side
   * filtering it does not have.
   */
  listJobs: (params: { status?: JobStatus; limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.status) query.set("status", params.status);
    query.set("limit", String(params.limit ?? 100));
    query.set("offset", String(params.offset ?? 0));
    return request<ApiJob[]>(`${env.apiPrefix}/jobs?${query.toString()}`);
  },

  /** GET /api/v1/jobs/{id} */
  getJob: (id: string) => request<ApiJobDetail>(`${env.apiPrefix}/jobs/${id}`),

  /** GET /api/v1/jobs/counts */
  jobCounts: () => request<JobStatusCounts>(`${env.apiPrefix}/jobs/counts`),

  /** GET /api/v1/sources — the only way to turn source_id into a name. */
  listSources: () => request<ApiSource[]>(`${env.apiPrefix}/sources`),

  /**
   * POST /api/v1/jobs/import — owner-only manual import of pasted or
   * uploaded postings. Nothing is fetched from the internet; the server
   * parses, validates, de-duplicates and reports each posting's outcome.
   */
  /** GET /api/v1/resumes/{id}/artifacts — rendered files for a resume. */
  listResumeArtifacts: (resumeId: string) =>
    request<ApiArtifact[]>(`${env.apiPrefix}/resumes/${resumeId}/artifacts`),

  /** POST /api/v1/applications/{id}/prepare — owner-supplied questions; worker grounds answers offline. */
  prepareApplication: (applicationId: string, payload: { questions: { question_text: string; input_type: string }[] }) =>
    request<{ application_id: string; event_id: string }>(`${env.apiPrefix}/applications/${applicationId}/prepare`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),

  /**
   * POST /api/v1/jobs/manual — one posting the owner typed or pasted. Exempt
   * from employer-domain site policies because nothing is ever fetched; bulk
   * imports (`importJobs`) stay subject to them.
   */
  importPastedJob: (payload: PastedJobRequest) =>
    request<ImportJobsResponse>(`${env.apiPrefix}/jobs/manual`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),

  importJobs: (payload: ImportJobsRequest) =>
    request<ImportJobsResponse>(`${env.apiPrefix}/jobs/import`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),

  /** GET /api/v1/applications — how Resume Studio finds an application. */
  listApplications: (params: { limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams();
    query.set("limit", String(params.limit ?? 50));
    query.set("offset", String(params.offset ?? 0));
    return request<ApiApplication[]>(
      `${env.apiPrefix}/applications?${query.toString()}`,
    );
  },

  /** GET /api/v1/applications/{id}/resumes/latest */
  latestResume: (applicationId: string) =>
    request<ApiResume>(
      `${env.apiPrefix}/applications/${applicationId}/resumes/latest`,
    ),

  /** GET /api/v1/resumes/{id} */
  getResume: (id: string) => request<ApiResume>(`${env.apiPrefix}/resumes/${id}`),

  /** GET /api/v1/applications/{id}/resume-review */
  resumeReview: (applicationId: string) =>
    request<ApiResumeReview>(
      `${env.apiPrefix}/applications/${applicationId}/resume-review`,
    ),

  /**
   * POST /api/v1/applications/{id}/resume-review — open human review.
   *
   * The empty body is deliberate: everything the server needs is the
   * application id in the path and the session behind the cookie.
   */
  startResumeReview: (applicationId: string) =>
    request<ApiResumeReview>(
      `${env.apiPrefix}/applications/${applicationId}/resume-review`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      },
    ),

  exportPreview: (applicationId: string) => request<ApiExportPreview>(`${env.apiPrefix}/applications/${applicationId}/resume-review/export-preview`),

  /** POST /api/v1/applications/{id}/resume-review/approve */
  approveResume: (applicationId: string, decisionNote?: string, exportDigest?: string) =>
    request<ApiResumeReview>(
      `${env.apiPrefix}/applications/${applicationId}/resume-review/approve`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          { decision_note: decisionNote, export_digest: exportDigest },
        ),
      },
    ),

  /** POST /api/v1/applications/{id}/resume-review/request-changes */
  requestResumeChanges: (applicationId: string, note: string, approvalId: string) =>
    request<ApiResumeReview>(
      `${env.apiPrefix}/applications/${applicationId}/resume-review/request-changes`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note, approval_id: approvalId }),
      },
    ),

  /** GET /api/v1/auth/config — which credentials this deployment demands. */
  authConfig: () => request<ApiAuthConfig>(`${env.apiPrefix}/auth/config`),

  /** POST /api/v1/auth/login — TOTP required outside local/test. */
  login: (body: { email: string; password: string; totp_code?: string }) =>
    request<ApiLoginResponse>(`${env.apiPrefix}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  /** POST /api/v1/auth/logout — revokes the session server-side. */
  logout: () =>
    request<{ revoked: boolean }>(`${env.apiPrefix}/auth/logout`, {
      method: "POST",
    }),

  /** GET /api/v1/auth/me — session bootstrap. 401 when signed out. */
  me: () => request<ApiUser>(`${env.apiPrefix}/auth/me`),
};

export interface ApiUser {
  id: string;
  email: string;
  role: string;
  last_login_at: string | null;
}

export interface ApiAuthConfig {
  totp_required: boolean;
}

export interface ApiLoginResponse {
  user: ApiUser;
  csrf_token: string;
}

// ---------------------------------------------------------------------------
// Resumes (C1 — Resume Studio)
//
// Mirrors apps/api/schemas/resumes.py exactly. Every field below is one the
// backend already returns from data B6 persisted. There is no fact text here:
// a claim carries fact_ids only, because no endpoint resolves them yet.
// ---------------------------------------------------------------------------

export type ClaimVerdict = "supported" | "overreach" | "unsupported" | "not_checked";
export type VerificationStatus = "pending" | "passed" | "failed";

export type ApplicationStatus =
  | "created" | "resume_generating" | "resume_ready" | "verification_failed"
  | "questions_pending" | "awaiting_approval" | "changes_requested" | "approved"
  | "submission_ready" | "submitting" | "submitted" | "submission_failed"
  | "manual_handoff" | "policy_blocked" | "abandoned" | "error";

export interface ApiClaim {
  id: string;
  section: string;
  path: string;
  claim_text: string;
  fact_ids: string[];
  numerals_used: string[];
  verdict: ClaimVerdict;
  verdict_reason: string | null;
  verified_at: string | null;
}

export interface ApiVerdictCounts {
  supported: number;
  overreach: number;
  unsupported: number;
  not_checked: number;
  total: number;
}

export interface ApiResume {
  id: string;
  application_id: string | null;
  version: number;
  kind: string;
  verification_status: VerificationStatus;
  summary_text: string | null;
  content: Record<string, Record<string, ApiContentEntry>>;
  created_at: string;
  model: string | null;
  prompt_version: string | null;
  application_status: ApplicationStatus | null;
  job_id: string | null;
  job_title: string | null;
  job_company: string | null;
  claims: ApiClaim[];
  counts: ApiVerdictCounts;
  /**
   * C2 review state for this resume.
   *
   * Null when this is not the current tailored version — the backend refuses
   * to attach a live review to superseded text, so the UI never offers a
   * decision about a resume that would not be the one submitted.
   */
  review: ApiResumeReview | null;
}

export interface ApiContentEntry {
  text: string;
  fact_ids: string[];
  numerals_used: string[];
  order: number;
}

// ---------------------------------------------------------------------------
// Resume review (C2)
//
// Mirrors apps/api/schemas/resume_review.py exactly.
//
// The three `can_*` booleans come from the server, which is the point: the
// same service decides what the buttons offer and what the endpoints accept,
// so the UI can never present an action the API would refuse. Nothing here is
// recomputed in the browser from the application status.
//
// Note the absence of any reviewer field on the request bodies. The deciding
// human is read from the session cookie server-side; there is deliberately no
// way to name one from the client.
// ---------------------------------------------------------------------------

export type ApprovalStatus =
  | "pending" | "approved" | "rejected" | "changes_requested" | "superseded";

export type ReviewStatus =
  | "not_started" | "in_review" | "approved" | "changes_requested";

export interface ApiResumeReview {
  application_id: string;
  application_status: ApplicationStatus;
  resume_id: string;
  resume_version: number;
  review_status: ReviewStatus;
  approval_id: string | null;
  approval_status: ApprovalStatus | null;
  approval_is_current: boolean;
  decision_note: string | null;
  changes_requested: string | null;
  requested_at: string | null;
  decided_at: string | null;
  decided_by: string | null;
  decided_by_email: string | null;
  can_start_review: boolean;
  can_approve: boolean;
  can_request_changes: boolean;
  blocked_reason: string | null;
  export_consent_required?: boolean;
}

export interface ApiApplication {
  id: string;
  job_id: string;
  status: ApplicationStatus;
  created_at: string;
  updated_at: string;
  job_title: string | null;
  job_company: string | null;
  resume_count: number;
  latest_resume_version: number | null;
  latest_verification_status: VerificationStatus | null;
}

export interface ApiExportPreview {
  digest: string;
  rendered_lines?: string[];
  document: {
    schema_version: number; profile_id: string; profile_version: number;
    contact: Record<string, string>; links: Record<string, string>;
    records: { section: string; source_table: string; source_id: string; fields: Record<string, string>; fact_ids: string[] }[];
  };
}

/** Body of POST /api/v1/jobs/import (apps/api/schemas/jobs.py ImportRequest). */
export interface ImportJobsRequest {
  source_slug: string;
  format: "csv" | "json";
  content: string;
  max_age_days?: number;
}

/** One posting's server-side outcome (OutcomeRead). */
export interface ImportOutcome {
  url: string;
  result: string;
  job_id: string | null;
  canonical_id: string | null;
  reason: string | null;
  warnings: string[];
}

/** Response of POST /api/v1/jobs/import (ImportResponse). */
export interface ImportJobsResponse {
  source_slug: string;
  total: number;
  created: number;
  duplicates: number;
  updated: number;
  rejected: number;
  outcomes: ImportOutcome[];
}

/** One rendered file (apps/api/schemas/artifacts.py ArtifactRead). */
export interface ApiArtifact {
  id: string;
  kind: string;
  content_type: string | null;
  size_bytes: number | null;
  sha256: string | null;
  resume_id: string | null;
  resume_version: number | null;
  approval_id: string | null;
  template_version: string | null;
  created_at: string;
}

/** Body of POST /api/v1/jobs/manual (ManualPostingRequest). */
export interface PastedJobRequest {
  title: string;
  company_name: string;
  url: string;
  location?: string;
  description: string;
}
