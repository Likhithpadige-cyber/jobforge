import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { ResumeStudioView } from "@/components/ResumeStudioView";
import { TailoringSnapshot } from "@/components/TailoringSnapshot";
import { VerdictBadge } from "@/components/VerdictBadge";
import { api, type ApiClaim, type ApiResume } from "@/lib/api";

// The export panel asks the API which files exist; no test here needs a network.
beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api, "listResumeArtifacts").mockResolvedValue([]);
});

function claim(overrides: Partial<ApiClaim> = {}): ApiClaim {
  return {
    id: "c1",
    section: "experience",
    path: "/experience/0",
    claim_text: "Delivered AWS infrastructure work.",
    fact_ids: ["11111111-1111-1111-1111-111111111111"],
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
    version: 3,
    kind: "tailored",
    verification_status: "passed",
    summary_text: "Cloud engineer with verified AWS depth.",
    content: {},
    created_at: "2026-09-12T10:00:00Z",
    model: "claude-sonnet-5",
    prompt_version: "b2/b3",
    application_status: "resume_ready",
    job_id: "j1",
    job_title: "Cloud Engineer",
    job_company: "Acme",
    claims: [
      claim({ id: "s1", section: "summary", path: "/summary/0", claim_text: "Summary one." }),
      claim(),
    ],
    counts: { supported: 2, overreach: 0, unsupported: 0, not_checked: 0, total: 2 },
    review: null,
    ...overrides,
  };
}

const failing = resume({
  verification_status: "failed",
  application_status: "verification_failed",
  claims: [
    claim({ id: "s1", section: "summary", path: "/summary/0", claim_text: "Summary one." }),
    claim({
      id: "bad",
      verdict: "unsupported",
      claim_text: "Architected the global platform.",
      verdict_reason: "No cited fact supports this scope.",
    }),
  ],
  counts: { supported: 1, overreach: 0, unsupported: 1, not_checked: 0, total: 2 },
});

describe("VerdictBadge", () => {
  it("renders each verdict as readable text, never colour alone", () => {
    const { rerender } = render(<VerdictBadge verdict="supported" />);
    expect(screen.getByText("Supported")).toBeInTheDocument();
    rerender(<VerdictBadge verdict="overreach" />);
    expect(screen.getByText("Overreach")).toBeInTheDocument();
    rerender(<VerdictBadge verdict="unsupported" />);
    expect(screen.getByText("Unsupported")).toBeInTheDocument();
  });
});

describe("Resume Studio — verified resume", () => {
  it("shows the role, version and both status badges", () => {
    render(<ResumeStudioView resume={resume()} />);
    // Scoped to the header: these labels intentionally appear again in the
    // snapshot panel, so an unscoped query would match twice.
    const header = within(screen.getByTestId("studio-header"));
    expect(header.getByText("Cloud Engineer · Acme")).toBeInTheDocument();
    expect(header.getByText(/Version 3/)).toBeInTheDocument();
    expect(header.getByText("Verified")).toBeInTheDocument();
    expect(header.getByText("Resume Ready")).toBeInTheDocument();
  });

  it("renders sections in reading order with their claims", () => {
    render(<ResumeStudioView resume={resume()} />);
    const headings = screen.getAllByRole("heading", { level: 2 });
    const labels = headings.map((h) => h.textContent);
    expect(labels).toContain("Professional Summary");
    expect(labels).toContain("Experience");
    expect(screen.getByText("Summary one.")).toBeInTheDocument();
    expect(screen.getByText("Delivered AWS infrastructure work.")).toBeInTheDocument();
  });

  it("shows no review banner when everything is supported", () => {
    render(<ResumeStudioView resume={resume()} />);
    expect(screen.queryByText(/need[s]? review/)).not.toBeInTheDocument();
  });
});

describe("Resume Studio — failed resume", () => {
  it("still shows the text that failed", () => {
    render(<ResumeStudioView resume={failing} />);
    expect(screen.getByText("Architected the global platform.")).toBeInTheDocument();
  });

  it("says how many claims need review", () => {
    render(<ResumeStudioView resume={failing} />);
    expect(screen.getByText(/1 of 2 claim needs review/)).toBeInTheDocument();
  });

  it("marks the failing claim and the resume state", () => {
    render(<ResumeStudioView resume={failing} />);
    const header = within(screen.getByTestId("studio-header"));
    expect(header.getByText("Verification failed")).toBeInTheDocument();
    expect(header.getByText("Verification Failed")).toBeInTheDocument();
    // The badge on the claim itself, not the snapshot's row label.
    const claimLine = screen
      .getByText("Architected the global platform.")
      .closest("li");
    expect(claimLine).not.toBeNull();
    if (!claimLine) throw new Error("expected the claim to render inside a list item");
    expect(within(claimLine).getByText("Unsupported")).toBeInTheDocument();
  });
});

describe("claim details", () => {
  it("stays collapsed until asked, then shows evidence", () => {
    render(<ResumeStudioView resume={resume()} />);
    expect(screen.queryByText("Cited facts")).not.toBeInTheDocument();
    const buttons = screen.getAllByRole("button", { name: "Show evidence" });
    expect(buttons).toHaveLength(2);
    const [summaryButton] = buttons;
    if (!summaryButton) throw new Error("expected an evidence toggle per claim");
    fireEvent.click(summaryButton);
    expect(screen.getByText("Cited facts")).toBeInTheDocument();
    expect(
      screen.getByText("11111111-1111-1111-1111-111111111111"),
    ).toBeInTheDocument();
    expect(screen.getByText("Matches the cited fact.")).toBeInTheDocument();
  });

  it("shows the reason a claim failed", () => {
    render(<ResumeStudioView resume={failing} />);
    const buttons = screen.getAllByRole("button", { name: "Show evidence" });
    expect(buttons).toHaveLength(2);
    const failingButton = buttons[1];
    if (!failingButton) throw new Error("expected an evidence toggle per claim");
    fireEvent.click(failingButton);
    expect(screen.getByText("No cited fact supports this scope.")).toBeInTheDocument();
  });
});

describe("Tailoring Snapshot", () => {
  it("is named Tailoring Snapshot, not an ATS score", () => {
    render(<TailoringSnapshot resume={resume()} />);
    // The panel deliberately explains itself as "not an ATS score", so a
    // plain text query would match that disclaimer. What matters is that
    // nothing is *titled* ATS Score - assert against the heading role.
    expect(
      screen.getByRole("heading", { name: "Tailoring Snapshot" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: /ATS Score/i }),
    ).not.toBeInTheDocument();
    // The disclaimer itself stays: it is useful copy, not a test failure.
    expect(screen.getByText(/not an ATS score/i)).toBeInTheDocument();
  });

  it("reports counts and version from the payload", () => {
    render(<TailoringSnapshot resume={failing} />);
    const panel = screen.getByLabelText("Tailoring Snapshot");
    expect(within(panel).getByText("v3")).toBeInTheDocument();
    expect(within(panel).getByText("Verification failed")).toBeInTheDocument();
    expect(within(panel).getByText("Total claims")).toBeInTheDocument();
  });
});

describe("read-only scope", () => {
  it("offers export through the API and says editing is not available", async () => {
    // Export is real now (resume-export.test.tsx covers downloads); editing
    // is still unbuilt and says so rather than showing a dead button.
    render(<ResumeStudioView resume={resume()} />);
    expect(await screen.findByText(/No PDF or DOCX exists for version/)).toBeInTheDocument();
    expect(screen.getByText(/Direct editing is not available yet/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Edit resume/ })).toBeNull();
  });

  it("shows no review panel when the API returned no review state", () => {
    // `review: null` is what the backend sends for a superseded version. The
    // studio must render the resume and simply offer nothing, rather than
    // inventing a panel from the application status.
    render(<ResumeStudioView resume={resume()} />);
    expect(screen.queryByTestId("review-panel")).not.toBeInTheDocument();
  });
});
