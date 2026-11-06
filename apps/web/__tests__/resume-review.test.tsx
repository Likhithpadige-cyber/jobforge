import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ResumeReviewPanel } from "@/components/ResumeReviewPanel";
import { ApiError, api, type ApiResumeReview } from "@/lib/api";
import { decisionLine, hasReviewAction, isValidChangeNote } from "@/lib/resumes";

/**
 * C2 — Resume review and approval.
 *
 * The panel is driven by the server's `can_*` booleans, so these tests set
 * those rather than the application status. That is the contract worth pinning:
 * if the UI ever started deciding for itself which buttons to show, it could
 * offer an action the API refuses, and the reviewer would learn about it only
 * from a 409.
 */

const exportDigest = "a".repeat(64);
beforeEach(() => {
  vi.spyOn(api, "exportPreview").mockResolvedValue({ digest: exportDigest, document: { schema_version: 1, profile_id: "p1", profile_version: 1, contact: { name: "Synthetic Candidate", email: "candidate@example.invalid" }, links: {}, records: [] } });
});
async function consentToExport() {
  fireEvent.click(await screen.findByRole("checkbox", { name: /I approve these exact/ }));
}

function review(overrides: Partial<ApiResumeReview> = {}): ApiResumeReview {
  return {
    application_id: "a1",
    application_status: "resume_ready",
    resume_id: "r1",
    resume_version: 3,
    review_status: "not_started",
    approval_id: null,
    approval_status: null,
    approval_is_current: false,
    decision_note: null,
    changes_requested: null,
    requested_at: null,
    decided_at: null,
    decided_by: null,
    decided_by_email: null,
    can_start_review: true,
    can_approve: false,
    can_request_changes: false,
    blocked_reason: null,
    ...overrides,
  };
}

const awaiting = review({
  application_status: "awaiting_approval",
  review_status: "in_review",
  approval_id: "ap1",
  approval_status: "pending",
  approval_is_current: true,
  requested_at: "2026-09-13T09:00:00Z",
  can_start_review: false,
  can_approve: true,
  can_request_changes: true,
});

const approved = review({
  application_status: "approved",
  review_status: "approved",
  approval_id: "ap1",
  approval_status: "approved",
  approval_is_current: true,
  decision_note: "Cleared for sending.",
  decided_at: "2026-09-13T10:00:00Z",
  decided_by: "u1",
  decided_by_email: "reviewer@example.invalid",
  can_start_review: false,
  can_approve: false,
  can_request_changes: false,
});

const changesRequested = review({
  application_status: "changes_requested",
  review_status: "changes_requested",
  approval_id: "ap1",
  approval_status: "changes_requested",
  approval_is_current: true,
  changes_requested: "Tighten the summary; it over-claims scope.",
  decision_note: "Tighten the summary; it over-claims scope.",
  decided_at: "2026-09-13T10:00:00Z",
  decided_by: "u1",
  decided_by_email: "reviewer@example.invalid",
  can_start_review: false,
  can_approve: false,
  can_request_changes: false,
});

// vitest.setup.ts already restores spies after every test, so nothing here
// needs its own teardown.

// --- which actions appear ---------------------------------------------------

describe("available actions", () => {
  it("offers Start Review when the resume is ready", () => {
    render(<ResumeReviewPanel review={review()} onReviewed={() => {}} />);
    expect(screen.getByRole("button", { name: /Start Review/ })).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: /Approve Resume/ }),
    ).not.toBeInTheDocument();
  });

  it("offers Approve and Request Changes only while awaiting approval", () => {
    render(<ResumeReviewPanel review={awaiting} onReviewed={() => {}} />);
    expect(screen.getByRole("button", { name: /Approve Resume/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Request Changes/ })).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: /Start Review/ }),
    ).not.toBeInTheDocument();
  });

  it("offers nothing once approved, and says who decided", () => {
    render(<ResumeReviewPanel review={approved} onReviewed={() => {}} />);
    for (const label of [/Start Review/, /Approve Resume/, /Request Changes/]) {
      expect(screen.queryByRole("button", { name: label })).not.toBeInTheDocument();
    }
    expect(screen.getByText("Approved")).toBeInTheDocument();
    expect(screen.getByTestId("decision-line")).toHaveTextContent(
      "reviewer@example.invalid",
    );
  });

  it("shows the submitted note and no approval action after changes are requested", () => {
    render(<ResumeReviewPanel review={changesRequested} onReviewed={() => {}} />);
    expect(screen.getByTestId("changes-note")).toHaveTextContent(
      "Tighten the summary; it over-claims scope.",
    );
    expect(
      screen.queryByRole("button", { name: /Approve Resume/ }),
    ).not.toBeInTheDocument();
  });

  it("explains why nothing is on offer when the review is stale", () => {
    render(
      <ResumeReviewPanel
        review={review({
          application_status: "awaiting_approval",
          review_status: "in_review",
          approval_is_current: false,
          can_start_review: false,
          blocked_reason: "a newer resume version exists; this review is stale",
        })}
        onReviewed={() => {}}
      />,
    );
    expect(screen.getByTestId("blocked-reason")).toHaveTextContent("stale");
  });
});

// --- approve ----------------------------------------------------------------

describe("approve flow", () => {
  it("confirms before approving, and does not call the API until confirmed", async () => {
    const spy = vi.spyOn(api, "approveResume").mockResolvedValue(approved);
    render(<ResumeReviewPanel review={awaiting} onReviewed={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Approve Resume/ }));
    expect(screen.getByTestId("approve-confirm")).toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled();

    await consentToExport();
    fireEvent.click(screen.getByRole("button", { name: /Confirm Approval/ }));
    await waitFor(() => expect(spy).toHaveBeenCalledWith("a1", undefined, exportDigest));
  });

  it("sends the optional note when one was typed", async () => {
    const spy = vi.spyOn(api, "approveResume").mockResolvedValue(approved);
    render(<ResumeReviewPanel review={awaiting} onReviewed={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Approve Resume/ }));
    fireEvent.change(screen.getByLabelText(/Note \(optional\)/), {
      target: { value: "  Checked every citation.  " },
    });
    await consentToExport();
    fireEvent.click(screen.getByRole("button", { name: /Confirm Approval/ }));

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith("a1", "Checked every citation.", exportDigest),
    );
  });

  it("hands the server's new state back rather than guessing at it", async () => {
    vi.spyOn(api, "approveResume").mockResolvedValue(approved);
    const onReviewed = vi.fn();
    render(<ResumeReviewPanel review={awaiting} onReviewed={onReviewed} />);

    fireEvent.click(screen.getByRole("button", { name: /Approve Resume/ }));
    await consentToExport();
    fireEvent.click(screen.getByRole("button", { name: /Confirm Approval/ }));

    await waitFor(() => expect(onReviewed).toHaveBeenCalledWith(approved));
  });

  it("can be cancelled without calling the API", () => {
    const spy = vi.spyOn(api, "approveResume");
    render(<ResumeReviewPanel review={awaiting} onReviewed={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Approve Resume/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Cancel$/ }));

    expect(screen.queryByTestId("approve-confirm")).not.toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled();
  });
});

// --- request changes --------------------------------------------------------

describe("request-changes flow", () => {
  it("keeps submit disabled until a real note is typed", () => {
    render(<ResumeReviewPanel review={awaiting} onReviewed={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /Request Changes/ }));

    const submit = screen.getByRole("button", { name: /Send Request/ });
    expect(submit).toBeDisabled();

    // Whitespace is not a note.
    fireEvent.change(screen.getByLabelText(/What should change\?/), {
      target: { value: "   " },
    });
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/What should change\?/), {
      target: { value: "Trim the summary." },
    });
    expect(submit).toBeEnabled();
  });

  it("submits the trimmed note", async () => {
    const spy = vi
      .spyOn(api, "requestResumeChanges")
      .mockResolvedValue(changesRequested);
    render(<ResumeReviewPanel review={awaiting} onReviewed={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /Request Changes/ }));
    fireEvent.change(screen.getByLabelText(/What should change\?/), {
      target: { value: "  Trim the summary.  " },
    });
    fireEvent.click(screen.getByRole("button", { name: /Send Request/ }));

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith("a1", "Trim the summary.", "ap1"),
    );
  });
});

// --- loading and errors -----------------------------------------------------

describe("loading and errors", () => {
  it("shows a pending label while the request is in flight", async () => {
    let resolve: ((value: ApiResumeReview) => void) | undefined;
    vi.spyOn(api, "startResumeReview").mockReturnValue(
      new Promise<ApiResumeReview>((r) => {
        resolve = r;
      }),
    );
    render(<ResumeReviewPanel review={review()} onReviewed={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /Start Review/ }));
    expect(
      await screen.findByRole("button", { name: /Starting review…/ }),
    ).toBeDisabled();

    // Settle it before the test ends, so the state update that follows is not
    // left dangling past teardown.
    resolve?.(awaiting);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Start Review/ })).toBeEnabled(),
    );
  });

  it("shows the server's own message when a decision is refused", async () => {
    // A 409 is the expected outcome of a stale tab, not a crash. The problem
    // detail explains which precondition failed, so it is surfaced verbatim.
    vi.spyOn(api, "approveResume").mockRejectedValue(
      new ApiError("Request failed: 409", 409, {
        detail: "a newer resume version exists; this review is stale",
      }),
    );
    const onReviewed = vi.fn();
    render(<ResumeReviewPanel review={awaiting} onReviewed={onReviewed} />);

    fireEvent.click(screen.getByRole("button", { name: /Approve Resume/ }));
    await consentToExport();
    fireEvent.click(screen.getByRole("button", { name: /Confirm Approval/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("stale");
    // A refusal must never look like a decision that landed.
    expect(onReviewed).not.toHaveBeenCalled();
  });

  it("explains the three-cycle limit without showing raw API text", async () => {
    vi.spyOn(api, "requestResumeChanges").mockRejectedValue(
      new ApiError("Request failed: 409", 409, {
        detail: "This application has already used its three request-changes cycles",
      }),
    );
    render(<ResumeReviewPanel review={awaiting} onReviewed={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /Request Changes/ }));
    fireEvent.change(screen.getByLabelText("What should change?"), { target: { value: "Clarify the summary" } });
    fireEvent.click(screen.getByRole("button", { name: "Send Request" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("reached its three revision requests");
  });
});

// --- helpers ----------------------------------------------------------------

describe("review helpers", () => {
  it("treats a whitespace-only note as empty", () => {
    expect(isValidChangeNote("")).toBe(false);
    expect(isValidChangeNote("  \n ")).toBe(false);
    expect(isValidChangeNote(" x ")).toBe(true);
  });

  it("reads the server's booleans rather than the application status", () => {
    expect(hasReviewAction(null)).toBe(false);
    expect(hasReviewAction(approved)).toBe(false);
    expect(hasReviewAction(awaiting)).toBe(true);
    // Status says awaiting_approval, but the server says no: the server wins.
    expect(
      hasReviewAction(
        review({
          application_status: "awaiting_approval",
          can_start_review: false,
        }),
      ),
    ).toBe(false);
  });

  it("names the decider, or says so plainly when it cannot", () => {
    expect(decisionLine(null)).toBeNull();
    expect(decisionLine(awaiting)).toBeNull(); // undecided
    expect(decisionLine(approved)).toContain("Approved by reviewer@example.invalid");
    expect(decisionLine(changesRequested)).toContain("Changes requested by");
    expect(
      decisionLine({ ...approved, decided_by_email: null }),
    ).toContain("a reviewer");
  });
});


describe("export approval safety", () => {
  it("requires explicit contact consent after showing exact details", async () => {
    const spy = vi.spyOn(api, "approveResume");
    render(<ResumeReviewPanel review={awaiting} onReviewed={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /Approve Resume/ }));
    expect(await screen.findByText("email: candidate@example.invalid")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Confirm Approval/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /Confirm Approval/ }));
    expect(spy).not.toHaveBeenCalled();
    await consentToExport();
    expect(screen.getByRole("button", { name: /Confirm Approval/ })).toBeEnabled();
  });
  it("cannot approve when preview access is refused", async () => {
    vi.mocked(api.exportPreview).mockRejectedValue(new ApiError("Owner access required", 403));
    render(<ResumeReviewPanel review={awaiting} onReviewed={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /Approve Resume/ }));
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: /Confirm Approval/ })).toBeDisabled();
  });
});
