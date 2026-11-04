import { readFileSync, readdirSync, existsSync } from "node:fs";
import path from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ApiError, api, type ApiApplication } from "@/lib/api";
import { activity, formatCost, formatDuration, type AgentRun, type AgentRunSummary } from "@/lib/activity";
import { profile } from "@/lib/profile";
import { review, type ApprovalInboxItem } from "@/lib/review";
import { PRIMARY_NAV, SECONDARY_NAV } from "@/lib/navigation";

const auth = vi.hoisted(() => ({ role: "owner" as string | null }));

vi.mock("@/components/AuthGuard", () => ({
  useAuth: () => ({
    user: auth.role ? { id: "u1", email: "owner@example.com", role: auth.role } : null,
    loading: false,
    signOut: async () => {},
  }),
}));

import { Dashboard } from "@/components/dashboard/Dashboard";
import { AgentActivityView } from "@/components/activity/AgentActivityView";

function run(overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    id: "r1", agent: "resume_optimizer", pipeline: "tailoring", status: "succeeded",
    steps_total: 3, steps_completed: 3, input_tokens: 100, output_tokens: 50, cost_usd: "0.0000",
    duration_ms: 1200, error_type: null, started_at: "2026-11-03T10:00:00Z", finished_at: "2026-11-03T10:00:01Z",
    application_id: "a1", ...overrides,
  };
}

const SUMMARY: AgentRunSummary = { since: "2026-10-27T00:00:00Z", runs: 4, by_status: { succeeded: 3, failed: 1 }, input_tokens: 400, output_tokens: 200, cost_usd: "0.0123" };

function application(status: ApiApplication["status"], id: string = status): ApiApplication {
  return { id, job_id: "j", status, created_at: "", updated_at: "", job_title: "Cloud Engineer", job_company: "Demo Co", resume_count: 1, latest_resume_version: 1, latest_verification_status: null };
}

const APPROVAL: ApprovalInboxItem = {
  approval_id: "ap1", kind: "resume", status: "pending", requested_at: "2026-11-03T09:00:00Z", decided_at: null,
  application_id: "a1", application_status: "awaiting_approval", job_title: "Cloud Engineer", job_company: "Demo Co",
};

function mockDashboard() {
  vi.spyOn(review, "approvals").mockResolvedValue([APPROVAL]);
  vi.spyOn(api, "listApplications").mockResolvedValue([application("awaiting_approval", "a1"), application("submitted", "a2")]);
  vi.spyOn(api, "jobCounts").mockResolvedValue({ counts: { evaluated: 7 }, total: 7 });
  vi.spyOn(activity, "runs").mockResolvedValue([run()]);
  vi.spyOn(activity, "summary").mockResolvedValue(SUMMARY);
  vi.spyOn(profile, "factSummary").mockResolvedValue({ total: 2, by_status: {}, citable: 2, never_used: 0, draft_awaiting_verification: 0 });
  vi.spyOn(profile, "get").mockResolvedValue({ full_name: "Demo Candidate" } as never);
}

beforeEach(() => {
  auth.role = "owner";
});

// -------------------------------------------------------------- dashboard
describe("dashboard", () => {
  it("shows only real counts and the real next decision", async () => {
    mockDashboard();
    render(<Dashboard />);
    const metrics = await screen.findByTestId("metrics");
    expect(metrics).toHaveTextContent("Jobs found7");
    expect(metrics).toHaveTextContent("Applications2");
    expect(metrics).toHaveTextContent("Waiting on you1");
    expect(metrics).toHaveTextContent("Applied1");
    expect(screen.getByTestId("priority")).toHaveTextContent("1 item is waiting for your review");
    expect(screen.getByRole("link", { name: /Resume · Cloud Engineer at Demo Co/ })).toHaveAttribute("href", "/resume-studio/a1");
    expect(screen.getByText(/4 runs in 7 days · \$0\.01 model cost/)).toBeInTheDocument();
    expect(document.getElementById("welcome-heading")?.textContent).toMatch(/, Demo\.$/);
  });

  it("a failed source is unknown, never zero, and retry reloads", async () => {
    mockDashboard();
    const approvals = vi.spyOn(review, "approvals").mockRejectedValueOnce(new ApiError("x", 500)).mockResolvedValue([]);
    render(<Dashboard />);
    expect(await screen.findByText(/Counts marked “—” are unknown, not zero/)).toBeInTheDocument();
    expect(screen.getByLabelText("Waiting on you: unknown")).toHaveTextContent("—");
    expect(screen.getByText("Approvals are unavailable right now.")).toBeInTheDocument();
    expect(screen.getByTestId("priority")).toHaveTextContent(/could not be loaded/);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Nothing is waiting for a decision.")).toBeInTheDocument();
    expect(approvals).toHaveBeenCalledTimes(2);
  });

  it("reviewers are not greeted with the candidate's name and the profile is not read", async () => {
    auth.role = "reviewer";
    mockDashboard();
    render(<Dashboard />);
    await screen.findByTestId("metrics");
    expect(profile.get).not.toHaveBeenCalled();
    expect(screen.queryByText(/Demo\./)).not.toBeInTheDocument();
  });

  it("an empty workspace says how to start", async () => {
    mockDashboard();
    vi.spyOn(review, "approvals").mockResolvedValue([]);
    vi.spyOn(api, "listApplications").mockResolvedValue([]);
    vi.spyOn(api, "jobCounts").mockResolvedValue({ counts: {}, total: 0 });
    vi.spyOn(activity, "runs").mockResolvedValue([]);
    render(<Dashboard />);
    expect(await screen.findByText("No jobs have been found yet")).toBeInTheDocument();
    expect(screen.getByText("No agent has run yet.")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------- activity
describe("agent activity", () => {
  it("lists runs with outcome, cost and steps on demand", async () => {
    vi.spyOn(activity, "runs").mockResolvedValue([run(), run({ id: "r2", status: "failed", error_type: "TimeoutError", application_id: null, pipeline: "evaluation", agent: "job_application" })]);
    vi.spyOn(activity, "summary").mockResolvedValue(SUMMARY);
    const detail = vi.spyOn(activity, "run").mockResolvedValue({ ...run(), steps: [{ step_index: 0, step_name: "draft_resume", status: "succeeded", duration_ms: 400, started_at: "", finished_at: null }] });
    render(<AgentActivityView />);
    expect(await screen.findByText("Stopped with TimeoutError")).toBeInTheDocument();
    expect(screen.getByTestId("activity-summary")).toHaveTextContent("Failed or stopped1");
    expect(screen.getAllByRole("link", { name: "Open application →" })).toHaveLength(1);
    fireEvent.click(screen.getAllByRole("button", { name: "Show steps" })[0]!);
    expect(await screen.findByText("1. draft resume")).toBeInTheDocument();
    expect(detail).toHaveBeenCalledWith("r1");
  });

  it("filters through the API and paginates", async () => {
    const runs = vi.spyOn(activity, "runs").mockResolvedValue(Array.from({ length: 25 }, (_, index) => run({ id: `r${index}` })));
    vi.spyOn(activity, "summary").mockResolvedValue(SUMMARY);
    render(<AgentActivityView />);
    fireEvent.change(await screen.findByLabelText("Outcome"), { target: { value: "failed" } });
    await waitFor(() => expect(runs).toHaveBeenLastCalledWith({ agent: undefined, status: "failed", limit: 25, offset: 0 }));
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await waitFor(() => expect(runs).toHaveBeenLastCalledWith({ agent: undefined, status: "failed", limit: 25, offset: 25 }));
  });

  it("an empty history and a load failure are distinct", async () => {
    vi.spyOn(activity, "runs").mockRejectedValueOnce(new ApiError("x", 500)).mockResolvedValue([]);
    vi.spyOn(activity, "summary").mockResolvedValue({ ...SUMMARY, runs: 0, by_status: {} });
    render(<AgentActivityView />);
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
  });

  it("formats cost and duration plainly", () => {
    expect(formatCost("0")).toBe("$0.00");
    expect(formatCost("0.0042")).toBe("$0.0042");
    expect(formatCost("1.5")).toBe("$1.50");
    expect(formatCost("nope")).toBe("—");
    expect(formatDuration(450)).toBe("450 ms");
    expect(formatDuration(12_300)).toBe("12.3 s");
    expect(formatDuration(125_000)).toBe("2 min 5 s");
    expect(formatDuration(null)).toBe("—");
  });

  it("builds queries without empty filters", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, text: async () => "[]" } as Response);
    vi.stubGlobal("fetch", fetch);
    await activity.runs({ agent: undefined, status: "failed", limit: 5 });
    const url = String(fetch.mock.calls[0]![0]);
    expect(url).toContain("/agent-runs?status=failed&limit=5");
    vi.unstubAllGlobals();
  });
});

// -------------------------------------------------------------- no demos
describe("no demo data in production views", () => {
  const root = path.resolve(__dirname, "..");

  it("demo fixtures and placeholders are gone", () => {
    for (const file of ["lib/demo-data.ts", "components/DemoDataNotice.tsx", "components/PlaceholderPage.tsx", "app/interviews/page.tsx", "app/analytics/page.tsx"]) {
      expect(existsSync(path.join(root, file)), file).toBe(false);
    }
  });

  it("no app or component source mentions them", () => {
    const walk = (dir: string): string[] => readdirSync(path.join(root, dir), { withFileTypes: true }).flatMap((entry) =>
      entry.isDirectory() ? walk(`${dir}/${entry.name}`) : /\.(ts|tsx)$/.test(entry.name) ? [`${dir}/${entry.name}`] : []);
    for (const file of [...walk("app"), ...walk("components"), ...walk("lib")]) {
      expect(readFileSync(path.join(root, file), "utf8"), file).not.toMatch(/demo-data|DemoDataNotice|PlaceholderPage|DEMO_/);
    }
  });

  it("navigation has no Interviews or Analytics", () => {
    const hrefs = [...PRIMARY_NAV, ...SECONDARY_NAV].map((item) => item.href);
    expect(hrefs).not.toContain("/interviews");
    expect(hrefs).not.toContain("/analytics");
  });
});
