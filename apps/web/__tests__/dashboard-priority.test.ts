import { describe, expect, it } from "vitest";
import { getDashboardPriority, type DashboardSignals } from "@/lib/dashboard-priority";

const base: DashboardSignals = { approvalsWaiting: 4, strongMatches: 31 };

describe("dashboard priority", () => {
  it("puts a resume waiting for review before other work", () => {
    expect(getDashboardPriority({ ...base, resumesWaitingReview: 1, interviewsWithinDay: 1 }).href).toBe("/resume-studio");
  });
  it("surfaces an imminent interview before routine approvals", () => {
    expect(getDashboardPriority({ ...base, interviewsWithinDay: 1 }).href).toBe("/interviews");
  });
  it("uses available demo signals without inventing missing ones", () => {
    expect(getDashboardPriority(base).href).toBe("/approvals");
  });
});
