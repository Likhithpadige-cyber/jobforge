import { describe, expect, it } from "vitest";
import type { ApiClaim, ApiResume } from "@/lib/api";
import {
  RESUME_GAPS,
  VERDICT_TONE,
  applicationLabel,
  applicationTone,
  failedClaims,
  formatTimestamp,
  groupSections,
  isVerified,
  jobLine,
  reviewSummary,
} from "@/lib/resumes";

function claim(overrides: Partial<ApiClaim> = {}): ApiClaim {
  return {
    id: "c1",
    section: "experience",
    path: "/experience/0",
    claim_text: "Delivered AWS infrastructure work.",
    fact_ids: ["f1"],
    numerals_used: [],
    verdict: "supported",
    verdict_reason: "Matches the cited fact.",
    verified_at: "2026-09-12T10:00:00Z",
    ...overrides,
  };
}

function resume(overrides: Partial<ApiResume> = {}): ApiResume {
  return {
    id: "r1",
    application_id: "a1",
    version: 1,
    kind: "tailored",
    verification_status: "passed",
    summary_text: "Cloud engineer.",
    content: {},
    created_at: "2026-09-12T10:00:00Z",
    model: "claude-sonnet-5",
    prompt_version: "b2/b3",
    application_status: "resume_ready",
    job_id: "j1",
    job_title: "Cloud Engineer",
    job_company: "Acme",
    claims: [claim()],
    counts: { supported: 1, overreach: 0, unsupported: 0, not_checked: 0, total: 1 },
    review: null,
    ...overrides,
  };
}

describe("section grouping", () => {
  it("orders sections the way a resume reads, not the order rows arrived", () => {
    const sections = groupSections([
      claim({ id: "c1", section: "skills", path: "/skills/2" }),
      claim({ id: "c2", section: "summary", path: "/summary/0" }),
      claim({ id: "c3", section: "experience", path: "/experience/1" }),
    ]);
    expect(sections.map((s) => s.key)).toEqual(["summary", "experience", "skills"]);
  });

  it("orders claims within a section by path index, not string order", () => {
    const sections = groupSections([
      claim({ id: "a", path: "/experience/10" }),
      claim({ id: "b", path: "/experience/2" }),
    ]);
    expect(sections).toHaveLength(1);
    const [experience] = sections;
    if (!experience) throw new Error("expected one grouped section");
    expect(experience.claims.map((c) => c.id)).toEqual(["b", "a"]);
  });

  it("renders an unrecognised section rather than dropping it", () => {
    const sections = groupSections([
      claim({ id: "c1", section: "summary", path: "/summary/0" }),
      claim({ id: "c2", section: "awards", path: "/awards/1" }),
    ]);
    expect(sections.map((s) => s.key)).toEqual(["summary", "awards"]);
    const [, awards] = sections;
    expect(awards).toBeDefined();
    if (!awards) throw new Error("expected an awards section");
    expect(awards.label).toBe("Awards");
  });

  it("returns nothing for no claims", () => {
    expect(groupSections([])).toEqual([]);
  });
});

describe("verdicts", () => {
  it("distinguishes overreach from unsupported by tone", () => {
    expect(VERDICT_TONE.overreach).not.toBe(VERDICT_TONE.unsupported);
    expect(VERDICT_TONE.supported).toBe("strong");
    expect(VERDICT_TONE.unsupported).toBe("risk");
  });

  it("collects only the claims a human must look at", () => {
    const failing = failedClaims([
      claim({ id: "ok" }),
      claim({ id: "over", verdict: "overreach", path: "/experience/1" }),
      claim({ id: "un", verdict: "unsupported", path: "/experience/2" }),
    ]);
    expect(failing.map((c) => c.id)).toEqual(["over", "un"]);
  });
});

describe("review summary", () => {
  it("is null when nothing failed", () => {
    expect(reviewSummary(resume())).toBeNull();
  });

  it("counts overreach and unsupported together, singular and plural", () => {
    expect(
      reviewSummary({
        counts: { supported: 2, overreach: 1, unsupported: 0, not_checked: 0, total: 3 },
      }),
    ).toBe("1 of 3 claim needs review");
    expect(
      reviewSummary({
        counts: { supported: 1, overreach: 1, unsupported: 1, not_checked: 0, total: 3 },
      }),
    ).toBe("2 of 3 claims need review");
  });
});

describe("labels", () => {
  it("humanises application status", () => {
    expect(applicationLabel("verification_failed")).toBe("Verification Failed");
    expect(applicationLabel(null)).toBe("Unknown");
  });

  it("tones failure states as risk and ready as brass", () => {
    expect(applicationTone("verification_failed")).toBe("risk");
    expect(applicationTone("resume_ready")).toBe("brass");
    expect(applicationTone("created")).toBe("neutral");
  });

  it("builds the header line with either part missing", () => {
    expect(jobLine({ job_title: "Cloud Engineer", job_company: "Acme" })).toBe(
      "Cloud Engineer · Acme",
    );
    expect(jobLine({ job_title: "Cloud Engineer", job_company: null })).toBe(
      "Cloud Engineer",
    );
    expect(jobLine({ job_title: null, job_company: null })).toBe("Untitled role");
  });

  it("reports verification state", () => {
    expect(isVerified(resume())).toBe(true);
    expect(isVerified({ verification_status: "failed" })).toBe(false);
  });
});

describe("timestamps", () => {
  it("renders a dash rather than Invalid Date", () => {
    expect(formatTimestamp(null)).toBe("—");
    expect(formatTimestamp("not-a-date")).toBe("—");
  });

  it("formats a real timestamp", () => {
    expect(formatTimestamp("2026-09-12T10:00:00Z")).not.toBe("—");
  });
});

describe("declared gaps", () => {
  it("names ATS scoring as absent rather than faking one", () => {
    expect(RESUME_GAPS.some((gap) => gap.id === "ats")).toBe(true);
  });
});
