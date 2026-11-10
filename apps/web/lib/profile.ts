/**
 * Profile, facts and settings client (M13 Phase 2).
 *
 * Mirrors apps/api/routers/profile.py, facts.py and settings.py. Every
 * mutation goes through the shared transport, which attaches the CSRF token,
 * and every one is owner-only on the server. Human attribution (who verified
 * a fact, who added evidence) is taken from the session by the server, so
 * this client never sends it.
 */

import { apiRequest } from "./api";
import { env } from "./env";
import type { SubmissionGate } from "./review";

const base = () => `${env.apiPrefix}/profile`;
const facts = () => `${base()}/facts`;
const send = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

// ----------------------------------------------------------------- profile
export interface Profile {
  id: string;
  full_name: string;
  headline: string | null;
  email: string | null;
  phone: string | null;
  location_city: string | null;
  location_state: string | null;
  location_country: string | null;
  work_authorization: string | null;
  willing_to_relocate: boolean;
  target_roles: string[];
  min_salary: number | null;
  max_salary: number | null;
  links: Record<string, string>;
  version: number;
  updated_at: string;
}

export type SectionKind = "experiences" | "educations" | "certifications" | "skills" | "projects";

export const SECTIONS: SectionKind[] = ["experiences", "educations", "certifications", "skills", "projects"];

/** `GET /profile` returns the profile with every section. */
export type ProfileDetail = Profile & Record<SectionKind, RawEntry[]>;

/** The editable fields. PUT replaces all of them, so edits start from the current record. */
export type ProfileInput = Omit<Profile, "id" | "version" | "updated_at">;

const EDITABLE: (keyof ProfileInput)[] = [
  "full_name", "headline", "email", "phone", "location_city", "location_state", "location_country",
  "work_authorization", "willing_to_relocate", "target_roles", "min_salary", "max_salary", "links",
];

/** Exactly the fields PUT accepts, taken from the current record so nothing is reset. */
export function profileInput(profile: Profile): ProfileInput {
  return Object.fromEntries(EDITABLE.map((key) => [key, profile[key]])) as unknown as ProfileInput;
}

export interface SectionEntry {
  id: string;
  /** What a person would call this entry, derived per section. */
  title: string;
  detail: string | null;
}

export interface RawEntry {
  id: string;
  [key: string]: unknown;
}

const str = (value: unknown): string | null => (typeof value === "string" && value.trim() ? value : null);

/** One readable line per entry, from whichever fields the section has. */
export function sectionEntry(kind: SectionKind, raw: RawEntry): SectionEntry {
  const join = (...parts: unknown[]) => parts.map(str).filter(Boolean).join(" · ") || null;
  switch (kind) {
    case "experiences":
      return { id: raw.id, title: `${str(raw.title) ?? "Role"} — ${str(raw.company_name) ?? "Company"}`, detail: join(raw.start_date, raw.end_date ?? (raw.is_current ? "present" : null)) };
    case "educations":
      return { id: raw.id, title: str(raw.institution) ?? "Institution", detail: join(raw.degree, raw.field_of_study) };
    case "certifications":
      return { id: raw.id, title: str(raw.name) ?? "Certification", detail: join(raw.issuer, raw.status) };
    case "skills":
      return { id: raw.id, title: str(raw.name) ?? "Skill", detail: join(raw.category, raw.proficiency) };
    case "projects":
      return { id: raw.id, title: str(raw.name) ?? "Project", detail: join(raw.role, str(raw.summary)?.slice(0, 140)) };
  }
}

export const SECTION_LABEL: Record<SectionKind, string> = {
  experiences: "Experience",
  educations: "Education",
  certifications: "Certifications",
  skills: "Skills",
  projects: "Projects",
};

// ------------------------------------------------------------------- facts
export type FactStatus = "draft" | "verified" | "retired";
export type FactType =
  | "experience" | "education" | "certification" | "skill" | "project"
  | "achievement" | "tool" | "contact" | "preference" | "authorization";

export interface Fact {
  id: string;
  fact_type: FactType;
  text: string;
  structured: Record<string, unknown>;
  source_table: string;
  source_id: string;
  status: FactStatus;
  confidence: string | null;
  usage_count: number;
  verified_by: string | null;
  verified_at: string | null;
  updated_at: string;
}

export interface FactSummary {
  total: number;
  by_status: Record<string, number>;
  citable: number;
  never_used: number;
  draft_awaiting_verification: number;
}

export type EvidenceKind = "document" | "url" | "attestation" | "credential";

export interface Evidence {
  id: string;
  kind: EvidenceKind;
  url: string | null;
  note: string | null;
  added_by: string | null;
  created_at: string;
}

export const FACT_STATUS_LABEL: Record<FactStatus, { label: string; tone: "strong" | "review" | "neutral" }> = {
  verified: { label: "Verified · citable", tone: "strong" },
  draft: { label: "Draft · not citable", tone: "review" },
  retired: { label: "Retired", tone: "neutral" },
};

// ---------------------------------------------------------------- settings
export type { SubmissionGate };

export const profile = {
  get: () => apiRequest<ProfileDetail>(base()),
  update: (input: ProfileInput, version: number) => apiRequest<Profile>(base(), send("PUT", { ...input, version })),

  /** GET /api/v1/profiles/{id}/master-resumes/latest — owner-only; 404 when none imported. */
  latestMasterResume: (profileId: string) =>
    apiRequest<{ resume_id: string; version: number; created_at: string; source_label: string | null }>(
      `${env.apiPrefix}/profiles/${profileId}/master-resumes/latest`,
    ),

  /** POST /api/v1/profiles/{id}/master-resumes — owner-reviewed lines; never verifies facts. */
  importMasterResume: (profileId: string, data: MasterResumeData) =>
    apiRequest<{ resume_id: string; profile_id: string; version: number; kind: string }>(
      `${env.apiPrefix}/profiles/${profileId}/master-resumes`,
      send("POST", data),
    ),

  facts: (status?: FactStatus) =>
    apiRequest<Fact[]>(`${facts()}${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  factSummary: () => apiRequest<FactSummary>(`${facts()}/summary`),
  editFact: (id: string, text: string) => apiRequest<Fact>(`${facts()}/${encodeURIComponent(id)}`, send("PATCH", { text })),
  verifyFact: (id: string) => apiRequest<Fact>(`${facts()}/${encodeURIComponent(id)}/verify`, send("POST", {})),
  unverifyFact: (id: string) => apiRequest<Fact>(`${facts()}/${encodeURIComponent(id)}/unverify`, send("POST")),
  retireFact: (id: string) => apiRequest<Fact>(`${facts()}/${encodeURIComponent(id)}/retire`, send("POST")),
  suggestFacts: (kind: SectionKind, sourceId: string) =>
    apiRequest<Fact[]>(`${facts()}/suggest`, send("POST", { source_table: kind, source_id: sourceId })),

  evidence: (factId: string) => apiRequest<Evidence[]>(`${facts()}/${encodeURIComponent(factId)}/evidence`),
  addEvidence: (factId: string, input: { kind: EvidenceKind; url?: string; note?: string }) =>
    apiRequest<Evidence>(`${facts()}/${encodeURIComponent(factId)}/evidence`, send("POST", {
      kind: input.kind,
      url: input.url?.trim() || null,
      note: input.note?.trim() || null,
    })),

  submissionGate: () => apiRequest<SubmissionGate>(`${env.apiPrefix}/settings/submission-gate`),
  setSubmissionEnabled: (enabled: boolean) =>
    apiRequest<{ submission_enabled: boolean }>(`${env.apiPrefix}/settings/submission-enabled`, send("PUT", { enabled })),
};

/** Sections accepted by the master resume import (MasterSection.name). */
export type MasterSectionName = "summary" | "experience" | "skills" | "projects" | "education" | "certifications";

/** Body of POST /profiles/{id}/master-resumes (MasterResumeData, schema_version 1). */
export interface MasterResumeData {
  schema_version: 1;
  source_label: string;
  sections: { name: MasterSectionName; entries: { text: string; fact_ids?: string[] }[] }[];
}
