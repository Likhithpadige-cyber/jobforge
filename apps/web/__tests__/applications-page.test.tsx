import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/components/AppShell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

import Page from "@/app/(workspace)/applications/page";
import { api, type ApiApplication } from "@/lib/api";

const base: ApiApplication = {
  id: "a1", job_id: "j1", status: "manual_handoff", created_at: "2026-11-01T00:00:00Z", updated_at: "2026-11-05T00:00:00Z",
  job_title: "Synthetic Engineer", job_company: "Example Co", resume_count: 1, latest_resume_version: 1, latest_verification_status: null,
  outcome: "interviewing", applied_on: "2026-11-04", applied_via: "manual", follow_up_on: "2026-11-12",
};

describe("applications page tracking", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("shows each application's outcome and counts manual sends as applied", async () => {
    vi.spyOn(api, "listApplications").mockResolvedValue([
      base,
      { ...base, id: "a2", status: "resume_ready", outcome: "pending", applied_on: null, applied_via: null, follow_up_on: null },
    ]);
    render(<Page />);
    const lines = await screen.findAllByTestId("tracking-line");
    expect(lines[0]).toHaveTextContent("Interview · sent by you on 2026-11-04 · follow up 2026-11-12");
    expect(lines[1]).toHaveTextContent("Not applied yet");
    expect(screen.getByText("Applied").previousSibling).toHaveTextContent("1");
    expect(screen.queryByText(/not yet included in the application API/)).toBeNull();
  });

  it("filters by outcome on the server", async () => {
    const spy = vi.spyOn(api, "listApplications").mockResolvedValue([base]);
    render(<Page />);
    await screen.findAllByTestId("tracking-line");
    fireEvent.change(screen.getByLabelText("Outcome"), { target: { value: "offer" } });
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith({ limit: 200, outcome: "offer" }));
    fireEvent.change(screen.getByLabelText("Outcome"), { target: { value: "not_applied" } });
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith({ limit: 200, applied: false }));
  });
});
