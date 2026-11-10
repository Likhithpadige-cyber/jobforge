import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ user: { id: "u1", email: "o@example.com", role: "owner" } }));
vi.mock("@/components/AuthGuard", () => ({ useAuth: () => ({ user: auth.user, loading: false, signOut: async () => {} }) }));

import { FollowUpsCard } from "@/components/dashboard/FollowUpsCard";
import { TrackingPanel, appliedText, changesFrom } from "@/components/tracking/TrackingPanel";
import { ApiError } from "@/lib/api";
import { addDays, dueText, localToday, tracking, trackingLabel, type FollowUp, type Tracking } from "@/lib/tracking";

const SAVED: Tracking = {
  application_id: "a1",
  status: "submitted",
  outcome: "pending",
  applied_on: null,
  follow_up_on: null,
  notes: null,
  submitted_at: null,
  outcome_updated_at: null,
  applied_via: null,
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
    expect(trackingLabel({ outcome: "pending", applied_via: null })).toBe("Not applied yet");
    expect(trackingLabel({ outcome: "pending", applied_via: "manual" })).toBe("Applied");
    expect(trackingLabel({ outcome: "pending", applied_via: "jobforge" })).toBe("Applied");
    expect(trackingLabel({ outcome: "interviewing", applied_via: "manual" })).toBe("Interview");
    expect(addDays("2026-12-29", 7)).toBe("2027-01-05");
    expect(appliedText({ applied_via: "jobforge", applied_on: null, submitted_at: "2026-11-03T10:00:00Z" })).toBe("Sent by JobForge on 2026-11-03");
    expect(appliedText({ applied_via: "manual", applied_on: "2026-11-04", submitted_at: null })).toBe("Sent by you on 2026-11-04");
    expect(appliedText({ applied_via: null, applied_on: null, submitted_at: null })).toBe("Not recorded as sent");
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

  it("owner records applied, interview and a follow-up preset, sending only the changes", async () => {
    vi.spyOn(tracking, "get").mockResolvedValue(SAVED);
    const update = vi.spyOn(tracking, "update").mockResolvedValue({ ...SAVED, outcome: "interviewing", applied_on: "2026-11-03", applied_via: "manual" });
    render(<TrackingPanel applicationId="a1" />);
    fireEvent.change(await screen.findByLabelText("Date applied"), { target: { value: "2026-11-03" } });
    fireEvent.change(screen.getByLabelText("Where it stands"), { target: { value: "interviewing" } });
    fireEvent.click(screen.getByRole("button", { name: "In 1 week" }));
    fireEvent.click(screen.getByRole("button", { name: "Save tracking" }));
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith("a1", { outcome: "interviewing", applied_on: "2026-11-03", follow_up_on: addDays(localToday(), 7) }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("nothing was sent");
    expect(screen.getByTestId("applied-source")).toHaveTextContent("Sent by you on 2026-11-03");
  });

  it("a manual handoff is recorded as sent by the owner, with a default follow-up", async () => {
    vi.spyOn(tracking, "get").mockResolvedValue({ ...SAVED, status: "manual_handoff" });
    const update = vi.spyOn(tracking, "update").mockResolvedValue({ ...SAVED, status: "manual_handoff", applied_on: localToday(), applied_via: "manual" });
    render(<TrackingPanel applicationId="a1" />);
    expect(await screen.findByText(/JobForge has not sent this application/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "I sent it today (follow up in 1 week)" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("a1", { applied_on: localToday(), follow_up_on: addDays(localToday(), 7) }));
    expect(await screen.findByTestId("applied-source")).toHaveTextContent("Sent by you on");
    expect(screen.queryByRole("button", { name: /I sent it today/ })).toBeNull();
  });

  it("a JobForge submission is labelled as such and offers no manual record", async () => {
    vi.spyOn(tracking, "get").mockResolvedValue({ ...SAVED, status: "submitted", submitted_at: "2026-11-03T10:00:00Z", applied_via: "jobforge" });
    render(<TrackingPanel applicationId="a1" />);
    expect(await screen.findByTestId("applied-source")).toHaveTextContent("Sent by JobForge on 2026-11-03");
    expect(screen.queryByRole("button", { name: /I sent it today/ })).toBeNull();
  });

  it("shows the server's refusal", async () => {
    vi.spyOn(tracking, "get").mockResolvedValue(SAVED);
    vi.spyOn(tracking, "update").mockRejectedValue(new ApiError("x", 422, { detail: "record the date you applied before marking it offer" }));
    render(<TrackingPanel applicationId="a1" />);
    fireEvent.change(await screen.findByLabelText("Where it stands"), { target: { value: "offer" } });
    fireEvent.click(screen.getByRole("button", { name: "Save tracking" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("record the date you applied");
  });

  it("reviewers see the record read-only", async () => {
    auth.user = { id: "u2", email: "r@example.com", role: "reviewer" };
    vi.spyOn(tracking, "get").mockResolvedValue({ ...SAVED, outcome: "offer", applied_on: "2026-11-01", applied_via: "manual", notes: "Synthetic note" });
    render(<TrackingPanel applicationId="a1" />);
    expect(await screen.findByText("Offer")).toBeInTheDocument();
    expect(screen.getByText("Synthetic note")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save tracking" })).toBeNull();
  });
});

describe("FollowUpsCard", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    auth.user = { id: "u1", email: "o@example.com", role: "owner" };
  });
  const row = (over: Partial<FollowUp>): FollowUp => ({
    application_id: "a1",
    job_title: "Synthetic Role",
    company_name: "Example Co",
    outcome: "pending",
    applied_on: "2026-11-01",
    applied_via: "manual",
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

  it("owner marks a follow-up done or snoozes it, then the list reloads", async () => {
    const list = vi.spyOn(tracking, "followUps").mockResolvedValueOnce([row({})]).mockResolvedValue([]);
    const update = vi.spyOn(tracking, "update").mockResolvedValue({} as Tracking);
    render(<FollowUpsCard />);
    expect(await screen.findByRole("link", { name: /Synthetic Role/ })).toHaveTextContent("Applied, sent by you");
    fireEvent.click(screen.getByRole("button", { name: "Snooze one week: Synthetic Role" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("a1", { follow_up_on: addDays(localToday(), 7) }));
    expect(await screen.findByText(/No follow-ups due/)).toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(2);
  });

  it("reviewers see follow-ups without the actions", async () => {
    auth.user = { id: "u2", email: "r@example.com", role: "reviewer" };
    vi.spyOn(tracking, "followUps").mockResolvedValue([row({})]);
    render(<FollowUpsCard />);
    expect(await screen.findByRole("link", { name: /Synthetic Role/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Mark follow-up done/ })).toBeNull();
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
