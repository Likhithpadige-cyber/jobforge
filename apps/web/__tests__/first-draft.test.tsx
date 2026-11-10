import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({ user: { id: "u1", email: "o@example.com", role: "owner" } as { id: string; email: string; role: string } | null }));
vi.mock("@/components/AuthGuard", () => ({ useAuth: () => ({ user: auth.user, loading: false, signOut: async () => {} }) }));

import { FirstDraftPanel } from "@/components/FirstDraftPanel";
import { ApiError, api, type ApiEvaluation, type RuntimeModes } from "@/lib/api";
import { profile, type ProfileDetail } from "@/lib/profile";

const REPLAY: RuntimeModes = { llm_mode: "replay", live_ai_enabled: false, submission_mode: "dry_run", email_transport_mode: "fake" };
const notFound = () => new ApiError("Request failed: 404", 404, { detail: "not found" });

function ready(modes: RuntimeModes = REPLAY) {
  vi.spyOn(api, "runtimeModes").mockResolvedValue(modes);
  vi.spyOn(profile, "get").mockResolvedValue({ id: "p1" } as unknown as ProfileDetail);
  vi.spyOn(profile, "latestMasterResume").mockResolvedValue({ resume_id: "m1", version: 2, created_at: "2026-11-10T00:00:00Z", source_label: "resume.txt" });
  vi.spyOn(api, "jobEvaluation").mockResolvedValue({ id: "e1" } as ApiEvaluation);
}

describe("FirstDraftPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    auth.user = { id: "u1", email: "o@example.com", role: "owner" };
  });

  it("tells reviewers the action is owner-only and loads nothing", () => {
    auth.user = { id: "u2", email: "r@example.com", role: "reviewer" };
    const spy = vi.spyOn(api, "runtimeModes");
    render(<FirstDraftPanel jobId="j1" />);
    expect(screen.getByText(/Only the workspace owner/)).toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled();
  });

  it("labels replay mode as a demonstration and requests a draft with the three ids", async () => {
    ready();
    const spy = vi.spyOn(api, "requestFirstDraft").mockResolvedValue({ application_id: "a9", event_id: "ev", profile_id: "p1" });
    render(<FirstDraftPanel jobId="j1" />);
    expect(await screen.findByTestId("draft-mode")).toHaveTextContent(/Replay demonstration only.*Live AI is disabled/);
    fireEvent.click(screen.getByRole("button", { name: "Request replay draft" }));
    await waitFor(() => expect(spy).toHaveBeenCalledWith("j1", { profile_id: "p1", master_resume_id: "m1", evaluation_id: "e1" }));
    expect(await screen.findByRole("link", { name: "open the application" })).toHaveAttribute("href", "/applications/a9");
  });

  it("warns that live mode may be billable", async () => {
    ready({ ...REPLAY, llm_mode: "live", live_ai_enabled: true });
    render(<FirstDraftPanel jobId="j1" />);
    expect(await screen.findByTestId("draft-mode")).toHaveTextContent(/may be billable/);
    expect(screen.getByRole("button", { name: "Request AI draft" })).toBeInTheDocument();
  });

  it("lists missing prerequisites instead of offering the request", async () => {
    vi.spyOn(api, "runtimeModes").mockResolvedValue(REPLAY);
    vi.spyOn(profile, "get").mockResolvedValue({ id: "p1" } as unknown as ProfileDetail);
    vi.spyOn(profile, "latestMasterResume").mockRejectedValue(notFound());
    vi.spyOn(api, "jobEvaluation").mockRejectedValue(notFound());
    render(<FirstDraftPanel jobId="j1" />);
    expect(await screen.findByText(/Import a master resume/)).toBeInTheDocument();
    expect(screen.getByText(/has not been evaluated yet/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Request/ })).toBeNull();
  });

  it("shows the server's refusal", async () => {
    ready();
    vi.spyOn(api, "requestFirstDraft").mockRejectedValue(new ApiError("Request failed: 409", 409, { detail: "job is not eligible for a first draft" }));
    render(<FirstDraftPanel jobId="j1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Request replay draft" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/not eligible/);
  });
});
