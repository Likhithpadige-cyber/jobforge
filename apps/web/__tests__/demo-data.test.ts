import { describe, expect, it } from "vitest";
import {
  DEMO_ACTIVITY, DEMO_ATTENTION, DEMO_FIT_DISTRIBUTION, DEMO_JOBS,
  DEMO_PIPELINE, IS_DEMO_DATA,
} from "@/lib/demo-data";
import { bandFor } from "@/components/FitScore";

/**
 * These guard the demo data's honesty, not its content. When real endpoints
 * arrive, this file is what proves the shapes still line up.
 */
describe("demo data", () => {
  it("is flagged as demo data", () => {
    expect(IS_DEMO_DATA).toBe(true);
  });

  it("uses fictional companies only", () => {
    const real = ["amazon", "google", "microsoft", "meta", "apple", "netflix"];
    for (const job of DEMO_JOBS) {
      expect(real).not.toContain(job.company.toLowerCase());
    }
  });

  it("keeps fit scores in range", () => {
    for (const job of DEMO_JOBS) {
      expect(job.fit).toBeGreaterThanOrEqual(0);
      expect(job.fit).toBeLessThanOrEqual(100);
    }
  });

  it("shows only jobs worth surfacing on the overview", () => {
    for (const job of DEMO_JOBS) {
      expect(["strong", "moderate"]).toContain(bandFor(job.fit));
    }
  });

  it("labels every activity item with a provenance", () => {
    for (const item of DEMO_ACTIVITY) {
      expect(["deterministic", "ai-assisted", "human"]).toContain(item.provenance);
    }
  });

  it("claims no AI-assisted work, because none is implemented yet", () => {
    expect(DEMO_ACTIVITY.every((a) => a.provenance !== "ai-assisted")).toBe(true);
  });

  it("gives every attention item an action", () => {
    for (const item of DEMO_ATTENTION) {
      expect(item.cta.length).toBeGreaterThan(0);
    }
  });

  it("has a pipeline matching the documented workflow", () => {
    expect(DEMO_PIPELINE.map((s) => s.key)).toEqual([
      "discovered", "evaluated", "shortlisted", "resume",
      "approval", "applied", "interview",
    ]);
  });

  it("has fit bands that sum to something plottable", () => {
    const total = DEMO_FIT_DISTRIBUTION.reduce((s, b) => s + b.count, 0);
    expect(total).toBeGreaterThan(0);
  });
});
