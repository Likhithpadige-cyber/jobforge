import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ user: { id: "u1", email: "o@example.com", role: "owner" } }));
vi.mock("@/components/AuthGuard", () => ({ useAuth: () => ({ user: auth.user, loading: false, signOut: async () => {} }) }));

import { FollowUpsCard } from "@/components/dashboard/FollowUpsCard";
import { TrackingPanel, changesFrom } from "@/components/tracking/TrackingPanel";
import { ApiError } from "@/lib/api";
import { dueText, localToday, tracking, trackingLabel, type FollowUp, type Tracking } from "@/lib/tracking";

const SAVED: Tracking = {
  application_id: "a1",
  status: "submitted",
  outcome: "pending",
  applied_on: null,
  follow_up_on: null,
  notes: null,
  submitted_at: "2026-11-03T10:00:00Z",
  outcome_updated_at: null,
};

describe("tracking helpers", () => {
  it("sends only changed fields and clears empty inputs", () => {
    const saved = { ...SAVED, applied_on: "2026-11-01", notes: "old" };
    expect(changesFrom(saved, { outcome: "pending", applied_on: "2026-11-01", follow_up_on: "", notes: "old" })).toEqual({});
    expect(changesFrom(saved, { outcome: "interviewing", applied_on: "", follow_up_on: "2026-11-12", notes: "  " })).toEqual({
      outcome: "interviewing",
      applied_on: null,
      follow_up_on: "2026-11-12",
      notes: null,
    });
  });

  it("labels and due text", () => {
    expect(trackingLabel({ outcome: "pending", applied_on: null })).toBe("Not applied yet");
    expect(trackingLabel({ outcome: "pending", applied_on: "2026-11-01" })).toBe("Applied");
    expect(trackingLabel({ outcome: "interviewing", applied_on: "2026-11-01" })).toBe("Interview");
    expect([dueText(0), dueText(1), dueText(3), dueText(-1), dueText(-4)]).toEqual([
      "due today",
      "1 day overdue",
      "3 days overdue",
      "in 1 day",
      "in 4 days",
    ]);
    expect(localToday(new Date(2026, 0, 5, 23, 30))).toBe("2026-01-05");
  });
});

describe("TrackingPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    auth.user = { id: "u1", email: "o@example.com", role: "owner" };
  });

  it("owner records applied, interview and a follow-up, sending only the changes", async () => {
    vi.spyOn(tracking, "get").mockResolvedValue(SAVED);
    const update = vi.spyOn(tracking, "update").mockResolvedValue({ ...SAVED, outcome: "interviewing", applied_on: "2026-11-03", follow_up_on: "2026-11-14" });
    render(<TrackingPanel applicationId="a1" />);
    fireEvent.click(await screen.findByRole("button", { name: /Use the recorded submission date/ }));
    fireEvent.change(screen.getByLabelText("Where it stands"), { target: { value: "interviewing" } });
    fireEvent.change(screen.getByLabelText("Follow up on"), { target: { value: "2026-11-14" } });
    fireEvent.click(screen.getByRole("button", { name: "Save tracking" }));
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith("a1", { outcome: "interviewing", applied_on: "2026-11-03", follow_up_on: "2026-11-14" }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("nothing was sent");
  });

  it("shows the server's refusal", async () => {
    vi.spyOn(tracking, "get").mockResolvedValue({ ...SAVED, submitted_at: null });
    vi.spyOn(tracking, "update").mockRejectedValue(new ApiError("x", 422, { detail: "record the date you applied before marking it offer" }));
    render(<TrackingPanel applicationId="a1" />);
    fireEvent.change(await screen.findByLabelText("Where it stands"), { target: { value: "offer" } });
    fireEvent.click(screen.getByRole("button", { name: "Save tracking" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("record the date you applied");
  });

  it("reviewers see the record read-only", async () => {
    auth.user = { id: "u2", email: "r@example.com", role: "reviewer" };
    vi.spyOn(tracking, "get").mockResolvedValue({ ...SAVED, outcome: "offer", applied_on: "2026-11-01", notes: "Synthetic note" });
    render(<TrackingPanel applicationId="a1" />);
    expect(await screen.findByText("Offer")).toBeInTheDocument();
    expect(screen.getByText("Synthetic note")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save tracking" })).toBeNull();
  });
});

describe("FollowUpsCard", () => {
  beforeEach(() => vi.restoreAllMocks());
  const row = (over: Partial<FollowUp>): FollowUp => ({
    application_id: "a1",
    job_title: "Synthetic Role",
    company_name: "Example Co",
    outcome: "pending",
    applied_on: "2026-11-01",
    follow_up_on: "2026-11-08",
    days_overdue: 2,
    ...over,
  });

  it("groups due and upcoming follow-ups with links to the application", async () => {
    const spy = vi.spyOn(tracking, "followUps").mockResolvedValue([
      row({}),
      row({ application_id: "a2", job_title: "Later Role", outcome: "interviewing", follow_up_on: "2026-11-13", days_overdue: -3 }),
    ]);
    render(<FollowUpsCard />);
    expect(await screen.findByText("Due now (1)")).toBeInTheDocument();
    expect(screen.getByText("Coming up (1)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Synthetic Role/ })).toHaveAttribute("href", "/applications/a1");
    expect(screen.getByRole("link", { name: /Later Role/ })).toHaveTextContent("in 3 days");
    expect(spy).toHaveBeenCalledWith(localToday(), 7);
  });

  it("empty and unavailable are different states", async () => {
    vi.spyOn(tracking, "followUps").mockResolvedValue([]);
    const { unmount } = render(<FollowUpsCard />);
    expect(await screen.findByText(/No follow-ups due/)).toBeInTheDocument();
    unmount();
    vi.spyOn(tracking, "followUps").mockRejectedValue(new ApiError("x", 500));
    render(<FollowUpsCard />);
    expect(await screen.findByText(/unavailable right now/)).toBeInTheDocument();
  });
});
