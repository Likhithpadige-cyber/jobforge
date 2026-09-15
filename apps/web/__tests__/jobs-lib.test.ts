import { describe, expect, it } from "vitest";
import type { ApiJob } from "@/lib/api";
import {
  BACKEND_GAPS, DEFAULT_FILTERS, applyFilters, filtersAreDefault,
  formatLocation, formatSalary, formatYears, matchesSearch, needsHumanDecision,
  relativeTime, sortJobs, splitRequirements, workMode,
} from "@/lib/jobs";

function job(overrides: Partial<ApiJob> = {}): ApiJob {
  return {
    id: "j1", source_id: "s1", company_id: null, external_id: null,
    url: "https://example.invalid/j/1", title: "Cloud Engineer",
    normalized_title: "cloud engineer", seniority: "mid",
    location_city: "Dallas", location_state: "TX", location_country: "US",
    is_remote: false, is_hybrid: false, employment_type: "full_time",
    salary_min: null, salary_max: null, salary_currency: null, salary_period: null,
    status: "normalized", discard_reason: null, duplicate_of_id: null, priority: 0,
    posted_at: null, first_seen_at: "2026-09-10T00:00:00Z",
    last_seen_at: "2026-09-10T00:00:00Z",
    ...overrides,
  };
}

describe("work mode", () => {
  it("treats hybrid as hybrid even when remote is also set", () => {
    expect(workMode(job({ is_remote: true, is_hybrid: true }))).toBe("hybrid");
  });
  it("falls back to on-site", () => {
    expect(workMode(job())).toBe("onsite");
  });
});

describe("formatLocation", () => {
  it("joins city and state", () => {
    expect(formatLocation(job())).toBe("Dallas, TX");
  });
  it("says Remote when there is nothing else", () => {
    expect(formatLocation(job({
      location_city: null, location_state: null, location_country: null, is_remote: true,
    }))).toBe("Remote");
  });
  it("never invents a location", () => {
    expect(formatLocation(job({
      location_city: null, location_state: null, location_country: null,
    }))).toBe("Location not given");
  });
});

describe("formatSalary", () => {
  it("returns null when nothing is stated", () => {
    expect(formatSalary(job())).toBeNull();
  });
  it("formats a range", () => {
    expect(formatSalary(job({
      salary_min: 120000, salary_max: 150000, salary_currency: "USD", salary_period: "yearly",
    }))).toBe("$120k–$150k");
  });
});

describe("relativeTime", () => {
  const now = new Date("2026-09-10T12:00:00Z");
  it.each([
    ["2026-09-10T11:30:00Z", "30m ago"],
    ["2026-09-10T08:00:00Z", "4h ago"],
    ["2026-09-07T12:00:00Z", "3d ago"],
  ])("formats %s as %s", (iso, expected) => {
    expect(relativeTime(iso, now)).toBe(expected);
  });
  it("handles a missing date", () => {
    expect(relativeTime(null)).toBe("—");
  });
});

describe("search", () => {
  it("matches on title", () => {
    expect(matchesSearch(job(), "cloud")).toBe(true);
  });
  it("matches on location", () => {
    expect(matchesSearch(job(), "dallas")).toBe(true);
  });
  it("returns everything for an empty term", () => {
    expect(matchesSearch(job(), "   ")).toBe(true);
  });
  it("excludes non-matches", () => {
    expect(matchesSearch(job(), "kubernetes")).toBe(false);
  });
});

describe("filters", () => {
  // `job()` defaults normalized_title to "cloud engineer", so a fixture that
  // overrides only `title` would still match a search for "engineer" through
  // its stale normalized title. Each fixture therefore states both.
  const jobs = [
    job({
      id: "a", title: "Cloud Engineer", normalized_title: "cloud engineer",
      status: "shortlisted", priority: 1,
    }),
    job({
      id: "b", title: "DevOps Engineer", normalized_title: "devops engineer",
      status: "evaluated", is_remote: true,
    }),
    job({
      id: "c", title: "Data Scientist", normalized_title: "data scientist",
      status: "discarded", source_id: "s2",
    }),
  ];

  it("defaults to best-available sort", () => {
    expect(DEFAULT_FILTERS.sort).toBe("best-fit");
    expect(filtersAreDefault(DEFAULT_FILTERS)).toBe(true);
  });

  it("best-available puts higher priority first", () => {
    const sorted = sortJobs(jobs, "best-fit");
    const [first] = sorted;
    expect(first).toBeDefined();
    if (!first) throw new Error("expected sortJobs to return at least one job");
    expect(first.id).toBe("a");
  });

  it("filters by status", () => {
    const out = applyFilters(jobs, { ...DEFAULT_FILTERS, status: "evaluated" });
    expect(out.map((j) => j.id)).toEqual(["b"]);
  });

  it("filters by work mode", () => {
    const out = applyFilters(jobs, { ...DEFAULT_FILTERS, mode: "remote" });
    expect(out.map((j) => j.id)).toEqual(["b"]);
  });

  it("filters by source", () => {
    const out = applyFilters(jobs, { ...DEFAULT_FILTERS, sourceId: "s2" });
    expect(out.map((j) => j.id)).toEqual(["c"]);
  });

  it("combines search with filters", () => {
    const out = applyFilters(jobs, { ...DEFAULT_FILTERS, search: "engineer" });
    expect(out).toHaveLength(2);
  });

  it("returns nothing when no job matches", () => {
    const out = applyFilters(jobs, { ...DEFAULT_FILTERS, search: "astronaut" });
    expect(out).toEqual([]);
  });

  it("detects a dirty filter set", () => {
    expect(filtersAreDefault({ ...DEFAULT_FILTERS, search: "aws" })).toBe(false);
  });
});

describe("human decision", () => {
  it("flags evaluated jobs as waiting on the person", () => {
    expect(needsHumanDecision(job({ status: "evaluated" }))).toBe(true);
  });
  it("does not flag shortlisted jobs", () => {
    expect(needsHumanDecision(job({ status: "shortlisted" }))).toBe(false);
  });
});

describe("requirements", () => {
  const reqs = [
    { text: "AWS", necessity: "required" as const, kind: "skill" as const,
      years_min: 3, years_max: null, section: "Requirements" },
    { text: "K8s", necessity: "preferred" as const, kind: "skill" as const,
      years_min: null, years_max: null, section: null },
  ];

  it("splits required from preferred", () => {
    const { required, preferred } = splitRequirements(reqs);
    expect(required).toHaveLength(1);
    expect(preferred).toHaveLength(1);
  });

  it("handles an absent requirements payload", () => {
    expect(splitRequirements(undefined).required).toEqual([]);
  });

  it("formats years only when stated", () => {
    const [withYears, withoutYears] = reqs;
    expect(withYears).toBeDefined();
    expect(withoutYears).toBeDefined();
    if (!withYears || !withoutYears) {
      throw new Error("expected both requirement fixtures");
    }
    expect(formatYears(withYears)).toBe("3+ years");
    expect(formatYears(withoutYears)).toBeNull();
  });
});

describe("backend gaps", () => {
  it("names the contract each gap needs", () => {
    expect(BACKEND_GAPS.length).toBeGreaterThan(0);
    for (const gap of BACKEND_GAPS) {
      expect(gap.needs.length).toBeGreaterThan(0);
      expect(gap.blocks.length).toBeGreaterThan(0);
    }
  });

  it("records that no evaluation endpoint exists", () => {
    expect(BACKEND_GAPS.some((g) => g.id === "evaluation")).toBe(true);
  });
});
