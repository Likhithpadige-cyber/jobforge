import { describe, expect, it } from "vitest";
import type { ApiApplication, ApplicationStatus } from "@/lib/api";
import { getDashboardPriority, pipelineStages, submissionProblems, type DashboardSignals } from "@/lib/dashboard-priority";

const base: DashboardSignals = {
  isOwner: true,
  pendingApprovals: 0,
  submissionProblems: 0,
  readyToSubmit: 0,
  draftFacts: 0,
  jobsFound: 5,
  applications: 2,
};

function app(status: ApplicationStatus): ApiApplication {
  return { id: status, job_id: "j", status, created_at: "", updated_at: "", job_title: null, job_company: null, resume_count: 0, latest_resume_version: null, latest_verification_status: null };
}

describe("dashboard priority", () => {
  it("a submission that did not complete comes first", () => {
    expect(getDashboardPriority({ ...base, submissionProblems: 1, pendingApprovals: 3 }).title).toBe("1 submission did not complete");
  });
  it("unknown approvals are never presented as nothing waiting", () => {
    const priority = getDashboardPriority({ ...base, pendingApprovals: null });
    expect(priority.title).toMatch(/could not be loaded/);
    expect(priority.href).toBe("/approvals");
  });
  it("pending approvals come before ready submissions", () => {
    expect(getDashboardPriority({ ...base, pendingApprovals: 2, readyToSubmit: 1 }).title).toBe("2 items are waiting for your review");
  });
  it("reviewers are told only the owner can submit", () => {
    expect(getDashboardPriority({ ...base, readyToSubmit: 1 }).body).toMatch(/explicit confirmation/);
    expect(getDashboardPriority({ ...base, readyToSubmit: 1, isOwner: false }).body).toMatch(/Only the workspace owner can submit/);
  });
  it("then draft facts, then an empty feed, then a first application", () => {
    expect(getDashboardPriority({ ...base, draftFacts: 1 }).href).toBe("/profile");
    expect(getDashboardPriority({ ...base, jobsFound: 0 }).title).toBe("No jobs have been found yet");
    expect(getDashboardPriority({ ...base, applications: 0 }).title).toBe("Start your first application");
    expect(getDashboardPriority(base).title).toBe("Nothing needs your decision right now");
  });
  it("never mentions interviews", () => {
    expect(JSON.stringify(getDashboardPriority(base))).not.toMatch(/interview/i);
  });
});

describe("pipeline", () => {
  it("counts real applications per group and marks attention", () => {
    const stages = pipelineStages([app("created"), app("awaiting_approval"), app("submission_failed"), app("submitted")], 9);
    expect(stages.map((stage) => [stage.label, stage.count])).toEqual([
      ["Jobs found", 9], ["Preparing", 1], ["Needs attention", 2], ["Ready", 0], ["Applied", 1], ["Closed", 0],
    ]);
    expect(stages.find((stage) => stage.label === "Needs attention")?.note).toBe("waiting on you");
  });
  it("omits jobs found when unknown", () => {
    expect(pipelineStages([], null)[0]!.label).toBe("Preparing");
  });
  it("counts submission problems", () => {
    expect(submissionProblems([app("submission_failed"), app("manual_handoff"), app("submitted")])).toBe(2);
  });
});
