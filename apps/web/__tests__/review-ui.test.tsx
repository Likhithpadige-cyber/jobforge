import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ApiError, api } from "@/lib/api";
import {
  review,
  type ApprovalInboxItem,
  type CoverLetterContent,
  type CoverLetterReview,
  type Question,
  type SubmissionAttempt,
  type SubmissionReadiness,
} from "@/lib/review";

const auth = vi.hoisted(() => ({ role: "owner" as string | null }));

vi.mock("@/components/AuthGuard", () => ({
  useAuth: () => ({
    user: auth.role ? { id: "u1", email: "owner@example.com", role: auth.role } : null,
    loading: false,
    signOut: async () => {},
  }),
}));

import { ApplicationWorkspace, nextStep } from "@/components/review/ApplicationWorkspace";
import { AnswersPackageView } from "@/components/review/AnswersPackageView";
import { ApprovalsInbox } from "@/components/review/ApprovalsInbox";
import { CoverLetterReviewView } from "@/components/review/CoverLetterReviewView";
import { SubmissionView } from "@/components/review/SubmissionView";

const CHECK_KEYS = [
  "application_status",
  "submission_enabled",
  "review_complete",
  "policy_allows_email",
  "policy_review_current",
  "attempt_recorded",
  "attempt_not_executed",
  "destination_authoritative",
];

function readiness(overrides: Partial<SubmissionReadiness> = {}): SubmissionReadiness {
  return {
    application_id: "a1",
    application_status: "submission_ready",
    ready: false,
    can_submit: false,
    gate: { submission_mode: "dry_run", persisted_enabled: false, live: false },
    destination_domain: "example.invalid",
    attempt: 1,
    checks: CHECK_KEYS.map((key) => ({ key, passed: key !== "submission_enabled", summary: `${key} summary` })),
    ...overrides,
  };
}

const LIVE = { submission_mode: "enabled" as const, persisted_enabled: true, live: true };

function ready(): SubmissionReadiness {
  return readiness({
    ready: true,
    can_submit: true,
    gate: LIVE,
    checks: CHECK_KEYS.map((key) => ({ key, passed: true, summary: `${key} ok` })),
  });
}

function attempt(overrides: Partial<SubmissionAttempt> = {}): SubmissionAttempt {
  return {
    submission_id: "s1",
    attempt: 1,
    method: "email",
    target_domain: "example.invalid",
    result: "failed",
    outcome: "ambiguous",
    smtp_status: null,
    started_at: "2026-11-03T10:00:00Z",
    finished_at: "2026-11-03T10:00:05Z",
    next_attempt_after: null,
    ...overrides,
  };
}

function approval(overrides: Partial<ApprovalInboxItem> = {}): ApprovalInboxItem {
  return {
    approval_id: "ap1",
    kind: "cover_letter",
    status: "pending",
    requested_at: "2026-11-03T09:00:00Z",
    decided_at: null,
    application_id: "a1",
    application_status: "awaiting_approval",
    job_title: "Cloud Engineer",
    job_company: "Demo Cloud",
    ...overrides,
  };
}

function letterState(overrides: Partial<CoverLetterReview> = {}): CoverLetterReview {
  return {
    application_id: "a1",
    application_status: "awaiting_approval",
    cover_letter_id: "c1",
    cover_letter_version: 1,
    verification_status: "passed",
    review_status: "in_review",
    approval_id: "ap1",
    approval_status: "pending",
    approval_is_current: true,
    decision_note: null,
    changes_requested: null,
    requested_at: null,
    decided_at: null,
    decided_by: null,
    decided_by_email: null,
    can_approve: true,
    can_request_changes: true,
    blocked_reason: null,
    ...overrides,
  };
}

const LETTER: CoverLetterContent = {
  cover_letter_id: "c1",
  application_id: "a1",
  version: 1,
  verification_status: "passed",
  created_at: "2026-11-03T09:00:00Z",
  claims: [
    {
      id: "cl1",
      section: "body",
      path: "/body/0",
      claim_text: "I built AWS infrastructure.",
      fact_ids: ["f1"],
      numerals_used: [],
      verdict: "supported",
      verdict_reason: "Matches the cited fact.",
      verified_at: null,
    },
  ],
};

const question: Question = {
  id: "q1",
  question_text: "Are you authorized to work here?",
  input_type: "text",
  options: {},
  is_required: true,
  max_length: null,
  display_order: 1,
  question_class: "judgement",
  class_reason: null,
  answer: {
    id: "ans1",
    question_id: "q1",
    value: null,
    value_structured: {},
    source: "blank",
    fact_ids: [],
    confidence: null,
    requires_human: true,
    answered_by: null,
    edited_from_agent_value: null,
  },
};

beforeEach(() => {
  auth.role = "owner";
});

// ---------------------------------------------------------------- submit
describe("submission", () => {
  it("dry-run blocks Submit and never calls the API", async () => {
    vi.spyOn(review, "readiness").mockResolvedValue(readiness());
    vi.spyOn(review, "submissions").mockResolvedValue([]);
    const submit = vi.spyOn(review, "submit");
    render(<SubmissionView applicationId="a1" />);
    expect(await screen.findByTestId("submission-gate")).toHaveTextContent("Dry-run mode");
    const button = screen.getByRole("button", { name: "Submit application…" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("submit-unavailable")).toHaveTextContent("never sends email");
    expect(screen.getByText("7 of 8 conditions met")).toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
  });

  it("requires confirmation, sends exactly once and re-reads the server", async () => {
    const read = vi.spyOn(review, "readiness").mockResolvedValueOnce(ready());
    vi.spyOn(review, "submissions").mockResolvedValueOnce([]).mockResolvedValue([attempt({ outcome: "pending", result: "pending", started_at: null, finished_at: null })]);
    let release: () => void = () => {};
    const submit = vi.spyOn(review, "submit").mockImplementation(
      () => new Promise((done) => { release = () => done({ application_id: "a1", submission_id: "s1", status: "submitting", attempt: 1 }); }),
    );
    read.mockResolvedValue(readiness({ application_status: "submitting", ready: false, can_submit: false, gate: LIVE }));
    render(<SubmissionView applicationId="a1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Submit application…" }));
    const dialog = screen.getByRole("dialog", { name: "Send this application now?" });
    expect(dialog).toHaveTextContent("This sends a real email to the employer");
    expect(dialog).toHaveTextContent("example.invalid");
    const send = within(dialog).getByRole("button", { name: "Send application" });
    expect(send).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(send);
    fireEvent.click(send); // double click while in flight
    expect(submit).toHaveBeenCalledTimes(1);
    release();
    expect(await screen.findByText(/Submission requested \(attempt 1\)/)).toBeInTheDocument();
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: "Submit application…" })).toBeDisabled();
    expect(screen.getByText("Not sent yet")).toBeInTheDocument();
    expect(screen.getByText(/Authorized by the owner; the submission worker is sending it/)).toBeInTheDocument();
  });

  it("a prepared attempt is not described as queued for sending", async () => {
    vi.spyOn(review, "readiness").mockResolvedValue(readiness());
    vi.spyOn(review, "submissions").mockResolvedValue([attempt({ outcome: "pending", result: "pending", started_at: null, finished_at: null })]);
    render(<SubmissionView applicationId="a1" />);
    expect(await screen.findByText("Not sent yet")).toBeInTheDocument();
    expect(screen.getByText(/Nothing is sent until the owner submits/)).toBeInTheDocument();
    expect(screen.queryByText(/worker is sending/)).not.toBeInTheDocument();
  });

  it("Escape cancels without sending", async () => {
    vi.spyOn(review, "readiness").mockResolvedValue(ready());
    vi.spyOn(review, "submissions").mockResolvedValue([]);
    const submit = vi.spyOn(review, "submit");
    render(<SubmissionView applicationId="a1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Submit application…" }));
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
  });

  it("a refused submit keeps the dialog open with the server's reason", async () => {
    vi.spyOn(review, "readiness").mockResolvedValue(ready());
    vi.spyOn(review, "submissions").mockResolvedValue([]);
    vi.spyOn(review, "submit").mockRejectedValue(new ApiError("x", 409, { detail: "Real submission is disabled in system configuration" }));
    render(<SubmissionView applicationId="a1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Submit application…" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Send application" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Real submission is disabled");
  });

  it("reviewers see readiness but never a Submit control", async () => {
    auth.role = "reviewer";
    vi.spyOn(review, "readiness").mockResolvedValue(ready());
    vi.spyOn(review, "submissions").mockResolvedValue([]);
    const submit = vi.spyOn(review, "submit");
    render(<SubmissionView applicationId="a1" />);
    expect(await screen.findByTestId("reviewer-no-submit")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Submit application/ })).not.toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
  });

  it("explains ambiguous outcomes and retries from structured history", async () => {
    vi.spyOn(review, "readiness").mockResolvedValue(readiness({ application_status: "submission_failed" }));
    vi.spyOn(review, "submissions").mockResolvedValue([
      attempt(),
      attempt({ submission_id: "s2", attempt: 2, outcome: "retry_scheduled", smtp_status: 421, next_attempt_after: "2026-11-03T11:00:00Z" }),
    ]);
    render(<SubmissionView applicationId="a1" />);
    expect(await screen.findByText("Outcome unknown — needs your review")).toBeInTheDocument();
    expect(screen.getByText(/never retried automatically; check the destination/)).toBeInTheDocument();
    expect(screen.getByText("Temporary failure — retry scheduled")).toBeInTheDocument();
    expect(screen.getByText(/Next automatic retry after/)).toBeInTheDocument();
  });

  it("a missing application is not found", async () => {
    vi.spyOn(review, "readiness").mockRejectedValue(new ApiError("x", 404, { detail: "application not found" }));
    vi.spyOn(review, "submissions").mockResolvedValue([]);
    render(<SubmissionView applicationId="nope" />);
    expect(await screen.findByTestId("review-not-found")).toHaveTextContent("application not found");
  });
});

// ------------------------------------------------------------ cover letter
describe("cover-letter review", () => {
  it("shows the text with evidence and requires confirmation to approve", async () => {
    vi.spyOn(review, "coverLetterReview").mockResolvedValue(letterState());
    vi.spyOn(review, "coverLetter").mockResolvedValue(LETTER);
    const approve = vi.spyOn(review, "approveCoverLetter").mockResolvedValue(letterState({ review_status: "approved", can_approve: false, can_request_changes: false }));
    render(<CoverLetterReviewView applicationId="a1" />);
    expect(await screen.findByText("I built AWS infrastructure.")).toBeInTheDocument();
    expect(screen.getByText("1 cited fact")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve cover letter…" }));
    const dialog = screen.getByRole("dialog", { name: "Approve this cover letter?" });
    expect(dialog).toHaveTextContent("it does not submit anything");
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith("a1", undefined));
    expect(await screen.findByText(/Cover letter approved/)).toBeInTheDocument();
  });

  it("requesting changes needs a note", async () => {
    vi.spyOn(review, "coverLetterReview").mockResolvedValue(letterState());
    vi.spyOn(review, "coverLetter").mockResolvedValue(LETTER);
    const changes = vi.spyOn(review, "requestCoverLetterChanges").mockResolvedValue(letterState({ review_status: "changes_requested" }));
    render(<CoverLetterReviewView applicationId="a1" />);
    const button = await screen.findByRole("button", { name: "Request changes…" });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText("What should change? (required)"), { target: { value: "  Mention Terraform.  " } });
    fireEvent.click(button);
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Request changes" }));
    await waitFor(() => expect(changes).toHaveBeenCalledWith("a1", "Mention Terraform."));
  });

  it("no letter yet is a state, and a version mismatch blocks deciding", async () => {
    vi.spyOn(review, "coverLetterReview").mockResolvedValue(letterState({ cover_letter_version: 2 }));
    vi.spyOn(review, "coverLetter").mockResolvedValue(LETTER); // version 1
    const { unmount } = render(<CoverLetterReviewView applicationId="a1" />);
    expect(await screen.findByTestId("decision-blocked")).toHaveTextContent("Reload to see the latest version");
    unmount();
    vi.spyOn(review, "coverLetterReview").mockResolvedValue(letterState({ cover_letter_version: null, review_status: "not_started", can_approve: false, can_request_changes: false, blocked_reason: "No cover letter yet." }));
    vi.spyOn(review, "coverLetter").mockRejectedValue(new ApiError("x", 404));
    render(<CoverLetterReviewView applicationId="a1" />);
    expect(await screen.findByText("No cover letter yet")).toBeInTheDocument();
  });
});

// ----------------------------------------------------------------- answers
describe("answers package", () => {
  it("approval is offered while awaiting approval and the server's refusal is shown", async () => {
    vi.spyOn(review, "questions").mockResolvedValue([question]);
    vi.spyOn(review, "readiness").mockResolvedValue(readiness({ application_status: "awaiting_approval" }));
    vi.spyOn(review, "approvals").mockResolvedValue([]);
    vi.spyOn(review, "approvePackage").mockRejectedValue(new ApiError("x", 409, { detail: "1 required answer is unresolved" }));
    render(<AnswersPackageView applicationId="a1" />);
    expect(await screen.findByText("1 need your answer")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve answers package…" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("approval will be refused until they have one");
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Approve" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("1 required answer is unresolved");
  });

  it("an approved package is shown as done", async () => {
    vi.spyOn(review, "questions").mockResolvedValue([question]);
    vi.spyOn(review, "readiness").mockResolvedValue(readiness({ application_status: "awaiting_approval" }));
    vi.spyOn(review, "approvals").mockImplementation(async (query) =>
      query?.status === "approved" ? [approval({ kind: "answers", status: "approved", decided_at: "2026-11-03T12:00:00Z" })] : [],
    );
    render(<AnswersPackageView applicationId="a1" />);
    expect(await screen.findByTestId("decision-blocked")).toHaveTextContent("These answers are approved.");
  });

  it("edits answers only while a review is open, then re-reads", async () => {
    const questions = vi.spyOn(review, "questions").mockResolvedValue([question]);
    vi.spyOn(review, "readiness").mockResolvedValue(readiness({ application_status: "awaiting_approval" }));
    vi.spyOn(review, "approvals").mockResolvedValue([]);
    const edit = vi.spyOn(review, "editAnswer").mockResolvedValue(question.answer!);
    render(<AnswersPackageView applicationId="a1" />);
    const field = await screen.findByLabelText(/Are you authorized/);
    fireEvent.change(field, { target: { value: "Yes" } });
    fireEvent.click(screen.getByRole("button", { name: "Save answer" }));
    await waitFor(() => expect(edit).toHaveBeenCalledWith("a1", "ans1", "Yes"));
    await waitFor(() => expect(questions).toHaveBeenCalledTimes(2));
  });

  it("outside awaiting approval nothing is editable or decidable", async () => {
    vi.spyOn(review, "questions").mockResolvedValue([question]);
    vi.spyOn(review, "readiness").mockResolvedValue(readiness({ application_status: "submission_ready" }));
    vi.spyOn(review, "approvals").mockResolvedValue([]);
    render(<AnswersPackageView applicationId="a1" />);
    expect(await screen.findByLabelText(/Are you authorized/)).toBeDisabled();
    expect(screen.getByTestId("decision-blocked")).toBeInTheDocument();
  });
});

// -------------------------------------------------------------------- inbox
describe("approvals inbox", () => {
  it("lists real approvals with links to the right review", async () => {
    vi.spyOn(review, "approvals").mockResolvedValue([approval(), approval({ approval_id: "ap2", kind: "resume" })]);
    render(<ApprovalsInbox />);
    expect(await screen.findAllByText("Cloud Engineer")).toHaveLength(2);
    const links = screen.getAllByRole("link").map((link) => link.getAttribute("href"));
    expect(links).toEqual(["/applications/a1/cover-letter", "/resume-studio/a1"]);
  });

  it("filters on the server and pages", async () => {
    const page = Array.from({ length: 50 }, (_, i) => approval({ approval_id: `ap${i}` }));
    const call = vi.spyOn(review, "approvals").mockResolvedValueOnce(page).mockResolvedValue([]);
    render(<ApprovalsInbox />);
    fireEvent.click(await screen.findByRole("button", { name: "Load more approvals" }));
    await waitFor(() => expect(call).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 50 })));
    fireEvent.click(screen.getByRole("button", { name: "Cover letters" }));
    await waitFor(() => expect(call).toHaveBeenLastCalledWith(expect.objectContaining({ kind: "cover_letter", offset: 0 })));
    expect(await screen.findByText("Nothing is waiting on you")).toBeInTheDocument();
  });

  it("errors offer a retry", async () => {
    vi.spyOn(review, "approvals").mockRejectedValueOnce(new ApiError("x", 500)).mockResolvedValue([]);
    render(<ApprovalsInbox />);
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Nothing is waiting on you")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- workspace
describe("application workspace", () => {
  it("guides through reviews in order: resume, cover letter, answers", () => {
    const base = { resume: null, resumeReview: null, coverLetter: null, answersApproved: false, history: [] };
    const awaiting = readiness({ application_status: "awaiting_approval" });
    expect(nextStep({ ...base, readiness: awaiting, questions: [question], pending: [approval({ kind: "cover_letter" }), approval({ kind: "resume" })] }, true)).toMatchObject({ href: "/resume-studio/a1" });
    expect(nextStep({ ...base, readiness: awaiting, questions: [question], pending: [approval({ kind: "cover_letter" })] }, true)).toMatchObject({ href: "/applications/a1/cover-letter" });
    // Answers have no pending approval row: they are next once the others are decided.
    expect(nextStep({ ...base, readiness: awaiting, questions: [question], pending: [] }, true)).toMatchObject({ href: "/applications/a1/answers" });
    expect(nextStep({ ...base, readiness: awaiting, questions: [question], pending: [], answersApproved: true }, true).href).toBeUndefined();
  });

  it("distinguishes owner and reviewer at the submission step", () => {
    const base = { resume: null, resumeReview: null, coverLetter: null, questions: null, pending: [], answersApproved: false, history: [] };
    expect(nextStep({ ...base, readiness: ready() }, true).title).toBe("Ready to submit");
    expect(nextStep({ ...base, readiness: readiness() }, true).body).toMatch(/Dry-run mode/);
    expect(nextStep({ ...base, readiness: readiness() }, false).title).toBe("Waiting for the owner");
  });

  it("tolerates materials that do not exist yet", async () => {
    vi.spyOn(review, "readiness").mockResolvedValue(readiness({ application_status: "created" }));
    vi.spyOn(api, "latestResume").mockRejectedValue(new ApiError("x", 404));
    vi.spyOn(api, "resumeReview").mockRejectedValue(new ApiError("x", 404));
    vi.spyOn(review, "coverLetterReview").mockRejectedValue(new ApiError("x", 404));
    vi.spyOn(review, "questions").mockResolvedValue([]);
    vi.spyOn(review, "approvals").mockResolvedValue([]);
    vi.spyOn(review, "submissions").mockResolvedValue([]);
    render(<ApplicationWorkspace applicationId="a1" />);
    expect(await screen.findByText("No tailored resume yet.")).toBeInTheDocument();
    expect(screen.getByText("No cover letter yet.")).toBeInTheDocument();
    expect(screen.getByText("7 of 8 readiness conditions met")).toBeInTheDocument();
    expect(screen.getByTestId("next-step")).toHaveTextContent("Saved");
  });

  function mockWorkspace() {
    vi.spyOn(review, "readiness").mockResolvedValue(readiness({ application_status: "awaiting_approval" }));
    vi.spyOn(api, "latestResume").mockRejectedValue(new ApiError("x", 404));
    vi.spyOn(api, "resumeReview").mockRejectedValue(new ApiError("x", 404));
    vi.spyOn(review, "coverLetterReview").mockResolvedValue(letterState());
    vi.spyOn(review, "questions").mockResolvedValue([]);
    vi.spyOn(review, "approvals").mockResolvedValue([]);
    vi.spyOn(review, "submissions").mockResolvedValue([]);
  }

  it("a server error marks only that section unavailable, never empty", async () => {
    mockWorkspace();
    vi.spyOn(api, "latestResume").mockRejectedValueOnce(new ApiError("x", 500)).mockRejectedValue(new ApiError("x", 404));
    render(<ApplicationWorkspace applicationId="a1" />);
    expect(await screen.findByText(/Part of this application could not be loaded/)).toBeInTheDocument();
    expect(screen.queryByText("No tailored resume yet.")).not.toBeInTheDocument();
    expect(screen.getByText(/Unavailable right now/)).toBeInTheDocument();
    expect(screen.getByText("7 of 8 readiness conditions met")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No tailored resume yet.")).toBeInTheDocument();
    expect(screen.queryByText(/could not be loaded/)).not.toBeInTheDocument();
  });

  it("unreadable decisions are never presented as nothing to decide", async () => {
    mockWorkspace();
    vi.spyOn(review, "approvals").mockRejectedValue(new ApiError("x", 0));
    render(<ApplicationWorkspace applicationId="a1" />);
    expect(await screen.findByTestId("next-step")).toHaveTextContent("Some review information could not be loaded");
    expect(screen.queryByText(/Nothing on this application is waiting/)).not.toBeInTheDocument();
  });

  it("without readiness the whole page is an error with retry", async () => {
    mockWorkspace();
    vi.spyOn(review, "readiness").mockRejectedValue(new ApiError("x", 500));
    render(<ApplicationWorkspace applicationId="a1" />);
    expect(await screen.findByTestId("review-error")).toBeInTheDocument();
  });

  it("a missing application is not found", async () => {
    mockWorkspace();
    vi.spyOn(review, "readiness").mockRejectedValue(new ApiError("x", 404, { detail: "application not found" }));
    render(<ApplicationWorkspace applicationId="a1" />);
    expect(await screen.findByTestId("review-not-found")).toHaveTextContent("application not found");
  });
});

describe("no demo data in review views", () => {
  it("review components and pages never import demo fixtures", () => {
    const root = path.resolve(__dirname, "..");
    const files = [
      ...readdirSync(path.join(root, "components/review")).map((f) => `components/review/${f}`),
      "lib/review.ts",
      "app/(workspace)/approvals/page.tsx",
      "app/(workspace)/applications/[id]/page.tsx",
      "app/(workspace)/applications/[id]/cover-letter/page.tsx",
      "app/(workspace)/applications/[id]/answers/page.tsx",
      "app/(workspace)/applications/[id]/submission/page.tsx",
    ];
    for (const file of files) {
      expect(readFileSync(path.join(root, file), "utf8"), file).not.toMatch(/demo-data|DemoDataNotice|PlaceholderPage/);
    }
  });
});

// Manual exports do not call the live submission action.
describe("manual application package", () => {
  it("is available in dry-run mode and reports missing approved artifacts", async () => {
    vi.spyOn(review, "readiness").mockResolvedValue(readiness());
    vi.spyOn(review, "submissions").mockResolvedValue([]);
    const submit = vi.spyOn(review, "submit");
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("", { status: 409 }));
    render(<SubmissionView applicationId="a1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Download manual package" }));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(String(fetch.mock.calls[0]?.[0])).toContain("/applications/a1/manual-packet");
    expect(fetch.mock.calls[0]?.[1]).toMatchObject({ method: "POST", credentials: "include" });
    expect(await screen.findByRole("alert")).toHaveTextContent("PDF, DOCX");
    expect(submit).not.toHaveBeenCalled();
  });
  it("does not offer contact-bearing packages to reviewers", async () => {
    auth.role = "reviewer";
    vi.spyOn(review, "readiness").mockResolvedValue(readiness());
    vi.spyOn(review, "submissions").mockResolvedValue([]);
    render(<SubmissionView applicationId="a1" />);
    await screen.findByTestId("submission-gate");
    expect(screen.queryByRole("button", { name: "Download manual package" })).not.toBeInTheDocument();
  });
});
