import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ApiError } from "@/lib/api";
import { operations, type MonitoringSummary, type OperatorAlert, type PauseStatus } from "@/lib/operations";

const auth = vi.hoisted(() => ({ role: "owner" as string | null }));

vi.mock("@/components/AuthGuard", () => ({
  useAuth: () => ({
    user: auth.role ? { id: "u1", email: "owner@example.com", role: auth.role } : null,
    loading: false,
    signOut: async () => {},
  }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/alerts",
  useSearchParams: () => new URLSearchParams(""),
}));

import { AlertDetailView } from "@/components/operations/AlertDetailView";
import { AlertsView } from "@/components/operations/AlertsView";
import { GenerationPauseBanner } from "@/components/operations/GenerationPauseBanner";
import { MonitoringView } from "@/components/operations/MonitoringView";
import { OperationsProvider, useOperations } from "@/components/operations/OperationsProvider";
import { OwnerOnly } from "@/components/operations/OperatorStates";
import { Header } from "@/components/Header";
import { Sidebar } from "@/components/Sidebar";

const PAUSING: OperatorAlert = {
  id: "a1",
  kind: "fabrication",
  title: "Guardrail incident: fabrication",
  severity: "critical",
  status: "open",
  auto_paused: true,
  requires_human: true,
  resolution: null,
  application_id: null,
  job_id: null,
  created_at: "2026-11-03T10:00:00Z",
  acknowledged_by: null,
  acknowledged_at: null,
  resolved_at: null,
};

function paused(count: number): PauseStatus {
  return { generation_paused: count > 0, unresolved_auto_pause_count: count };
}

function summary(overrides: Partial<MonitoringSummary> = {}): MonitoringSummary {
  return {
    generation_paused: false,
    open_alerts: { total: 0, by_severity: {}, by_kind: {} },
    latest_snapshot: null,
    ...overrides,
  };
}

function withProvider(ui: React.ReactNode) {
  return render(
    <OperationsProvider>
      <GenerationPauseBanner />
      {ui}
    </OperationsProvider>,
  );
}

beforeEach(() => {
  auth.role = "owner";
  vi.spyOn(operations, "summary").mockResolvedValue(summary());
});

// ----------------------------------------------------------- pause banner
describe("generation pause banner", () => {
  it("shows the server's count while paused", async () => {
    vi.spyOn(operations, "pauseStatus").mockResolvedValue(paused(2));
    withProvider(null);
    expect(await screen.findByTestId("generation-pause-banner")).toHaveTextContent(
      "2 safety incidents need your review",
    );
    expect(screen.getByRole("link", { name: /Review incidents/ })).toHaveAttribute("href", "/alerts?pausing=1");
  });

  it("is absent when the server says generation is enabled", async () => {
    const spy = vi.spyOn(operations, "pauseStatus").mockResolvedValue(paused(0));
    withProvider(null);
    await waitFor(() => expect(spy).toHaveBeenCalled());
    expect(screen.queryByTestId("generation-pause-banner")).not.toBeInTheDocument();
  });

  it("keeps the last confirmed pause and warns when a refresh fails", async () => {
    const spy = vi.spyOn(operations, "pauseStatus").mockResolvedValueOnce(paused(1));
    let refresh: () => Promise<void> = async () => {};
    function Capture() {
      refresh = useOperations().refresh;
      return null;
    }
    withProvider(<Capture />);
    await screen.findByTestId("generation-pause-banner");
    spy.mockRejectedValueOnce(new ApiError("down", 503));
    await act(async () => refresh());
    const banner = screen.getByTestId("generation-pause-banner");
    expect(banner).toHaveTextContent("Generation is paused");
    expect(banner).toHaveTextContent("Status may be out of date");
  });

  it("never calls owner endpoints for a reviewer", async () => {
    auth.role = "reviewer";
    const pause = vi.spyOn(operations, "pauseStatus");
    withProvider(
      <OwnerOnly>
        <AlertsView />
      </OwnerOnly>,
    );
    expect(screen.getByTestId("operations-unauthorized")).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(pause).not.toHaveBeenCalled();
    expect(operations.summary).not.toHaveBeenCalled();
    expect(screen.queryByTestId("generation-pause-banner")).not.toBeInTheDocument();
  });
});

// ------------------------------------------------------- resolution flow
describe("alert resolution", () => {
  async function openDialog() {
    fireEvent.click(await screen.findByRole("button", { name: "Resolve incident…" }));
    return screen.getByRole("dialog", { name: "Resolve this incident?" });
  }

  it("requires explicit confirmation and states the impact first", async () => {
    vi.spyOn(operations, "pauseStatus").mockResolvedValue(paused(2));
    vi.spyOn(operations, "getAlert").mockResolvedValue(PAUSING);
    const resolve = vi.spyOn(operations, "resolveAlert");
    withProvider(<AlertDetailView id="a1" />);
    await screen.findByTestId("generation-pause-banner");
    const dialog = await openDialog();
    expect(within(dialog).getByTestId("resolution-impact")).toHaveTextContent(
      "Generation stays paused: 1 other pausing incident will remain unresolved.",
    );
    const confirm = within(dialog).getByRole("button", { name: "Resolve incident" });
    expect(confirm).toBeDisabled();
    fireEvent.click(confirm);
    expect(resolve).not.toHaveBeenCalled();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(resolve).not.toHaveBeenCalled();
  });

  it("follows the server after resolving one of two pausing incidents", async () => {
    const pause = vi.spyOn(operations, "pauseStatus").mockResolvedValueOnce(paused(2));
    const get = vi.spyOn(operations, "getAlert").mockResolvedValueOnce(PAUSING);
    const resolve = vi.spyOn(operations, "resolveAlert").mockResolvedValue({ ...PAUSING, status: "resolved", resolution: "human" });
    withProvider(<AlertDetailView id="a1" />);
    await screen.findByTestId("generation-pause-banner");
    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByRole("checkbox"));

    // The server still reports one pausing incident after this resolution.
    pause.mockResolvedValueOnce(paused(1));
    get.mockResolvedValueOnce({ ...PAUSING, status: "resolved", resolution: "human", resolved_at: "2026-11-03T11:00:00Z" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Resolve incident" }));

    await screen.findByText("Incident resolved. Generation status was re-checked with the server.");
    expect(resolve).toHaveBeenCalledTimes(1);
    expect(resolve).toHaveBeenCalledWith("a1");
    expect(screen.getByTestId("generation-pause-banner")).toHaveTextContent("1 safety incident needs your review");
    expect(screen.getByText(/by the workspace owner/)).toBeInTheDocument();
  });

  it("clears the banner only when the server confirms the final resolution", async () => {
    const pause = vi.spyOn(operations, "pauseStatus").mockResolvedValueOnce(paused(1));
    const get = vi.spyOn(operations, "getAlert").mockResolvedValueOnce(PAUSING);
    let release: (value: OperatorAlert) => void = () => {};
    vi.spyOn(operations, "resolveAlert").mockImplementation(
      () => new Promise<OperatorAlert>((done) => { release = done; }),
    );
    withProvider(<AlertDetailView id="a1" />);
    await screen.findByTestId("generation-pause-banner");
    const dialog = await openDialog();
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Resolve incident" }));
    // In flight: nothing has been cleared optimistically.
    expect(screen.getByTestId("generation-pause-banner")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Resolving…" })).toBeDisabled();

    pause.mockResolvedValueOnce(paused(0));
    get.mockResolvedValueOnce({ ...PAUSING, status: "resolved", resolution: "human" });
    await act(async () => release({ ...PAUSING, status: "resolved", resolution: "human" }));
    await waitFor(() => expect(screen.queryByTestId("generation-pause-banner")).not.toBeInTheDocument());
  });

  it("a failed resolution changes nothing", async () => {
    const pause = vi.spyOn(operations, "pauseStatus").mockResolvedValue(paused(1));
    const get = vi.spyOn(operations, "getAlert").mockResolvedValue(PAUSING);
    vi.spyOn(operations, "resolveAlert").mockRejectedValue(new ApiError("forbidden", 403));
    withProvider(<AlertDetailView id="a1" />);
    await screen.findByTestId("generation-pause-banner");
    const dialog = await openDialog();
    const pauseCalls = pause.mock.calls.length;
    const getCalls = get.mock.calls.length;
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Resolve incident" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Only the workspace owner can resolve incidents.");
    expect(pause.mock.calls.length).toBe(pauseCalls);
    expect(get.mock.calls.length).toBe(getCalls);
    expect(screen.getByTestId("generation-pause-banner")).toBeInTheDocument();
  });

  it("shows navigation links only for navigable references", async () => {
    vi.spyOn(operations, "pauseStatus").mockResolvedValue(paused(0));
    vi.spyOn(operations, "getAlert").mockResolvedValue({
      ...PAUSING,
      kind: "application_stalled",
      title: "Application stalled",
      auto_paused: false,
      requires_human: false,
      application_id: "app-1",
    });
    withProvider(<AlertDetailView id="a1" />);
    expect(await screen.findByRole("link", { name: "Open application" })).toHaveAttribute("href", "/applications/app-1");
    expect(screen.queryByRole("link", { name: "Open job" })).not.toBeInTheDocument();
    expect(screen.getByText(/Clears itself when recovery is verified/)).toBeInTheDocument();
  });

  it("reports a missing alert as not found", async () => {
    vi.spyOn(operations, "pauseStatus").mockResolvedValue(paused(0));
    vi.spyOn(operations, "getAlert").mockRejectedValue(new ApiError("missing", 404));
    withProvider(<AlertDetailView id="nope" />);
    expect(await screen.findByTestId("operations-not-found")).toBeInTheDocument();
  });
});

// --------------------------------------------------------------- list view
describe("alerts list", () => {
  beforeEach(() => {
    vi.spyOn(operations, "pauseStatus").mockResolvedValue(paused(0));
  });

  it("asks the server for pausing incidents when opened from the banner", async () => {
    const list = vi.spyOn(operations, "listAlerts").mockResolvedValue([PAUSING]);
    withProvider(<AlertsView initialPausingOnly />);
    expect(await screen.findByText("Guardrail incident: fabrication")).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith(expect.objectContaining({ status: "open", auto_paused: true, offset: 0 }));
    expect(screen.getByText("Pauses generation")).toBeInTheDocument();
    expect(screen.getByText(/Needs owner resolution/)).toBeInTheDocument();
  });

  it("filters on the server and pages with load more", async () => {
    const page = Array.from({ length: 50 }, (_, i) => ({ ...PAUSING, id: `a${i}`, auto_paused: false }));
    const list = vi.spyOn(operations, "listAlerts").mockResolvedValueOnce(page).mockResolvedValue([]);
    withProvider(<AlertsView />);
    fireEvent.click(await screen.findByRole("button", { name: "Load more alerts" }));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 50 })));
    list.mockResolvedValue([]);
    fireEvent.click(screen.getByRole("button", { name: "Resolved" }));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(expect.objectContaining({ status: "resolved", offset: 0 })));
    expect(await screen.findByText("No resolved alerts yet")).toBeInTheDocument();
  });

  it("has distinct empty, error and unauthorized states", async () => {
    const list = vi.spyOn(operations, "listAlerts").mockResolvedValueOnce([]);
    const { unmount } = withProvider(<AlertsView />);
    expect(await screen.findByText("No open alerts")).toBeInTheDocument();
    unmount();

    list.mockRejectedValueOnce(new ApiError("boom", 500)).mockResolvedValueOnce([PAUSING]);
    const second = withProvider(<AlertsView />);
    expect(await screen.findByTestId("operations-error")).toHaveTextContent("The API returned an error (500).");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Guardrail incident: fabrication")).toBeInTheDocument();
    second.unmount();

    list.mockRejectedValueOnce(new ApiError("no", 403));
    withProvider(<AlertsView />);
    expect(await screen.findByTestId("operations-unauthorized")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------- monitoring view
describe("monitoring dashboard", () => {
  const recent = () => new Date(Date.now() - 2 * 60_000).toISOString();

  beforeEach(() => {
    vi.spyOn(operations, "snapshots").mockResolvedValue([]);
  });

  it("renders only reported values and marks missing ones", async () => {
    vi.spyOn(operations, "summary").mockResolvedValue(
      summary({
        open_alerts: { total: 3, by_severity: { high: 3 }, by_kind: { dead_letter: 2, worker_heartbeat_missing: 1 } },
        latest_snapshot: {
          bucket_start: "2026-11-03T10:00:00Z",
          grain: "5min",
          job_counts: {},
          operations: {
            evaluated_at: recent(),
            redis_available: true,
            workers: { relay: { live: 1, missing: 0 }, submission: { live: 0, missing: 1 } },
            consumer_lag: { "evt.jobs/cg.optimizer": 12, "evt.system/cg.monitor": null },
            dead_letters: 2,
            // submission_window, stalled_applications and pending_approvals absent
          },
        },
      }),
    );
    render(<MonitoringView />);
    const [deadLetters] = await screen.findAllByText("Dead-lettered deliveries");
    expect(deadLetters!.parentElement).toHaveTextContent("2");
    for (const label of ["Submission failures", "Stalled applications", "Pending approvals"]) {
      expect(screen.getAllByText(label)[0]!.parentElement).toHaveTextContent("Not available");
    }
    expect(screen.getByText("1 missing")).toBeInTheDocument();
    expect(screen.getByText("Unknown")).toBeInTheDocument();
    expect(screen.getByText("Dead-lettered delivery")).toBeInTheDocument();
    expect(screen.queryByTestId("stale-snapshot")).not.toBeInTheDocument();
  });

  it("warns when the monitor has not evaluated recently", async () => {
    vi.spyOn(operations, "summary").mockResolvedValue(
      summary({
        latest_snapshot: {
          bucket_start: "2026-11-03T08:00:00Z",
          grain: "5min",
          job_counts: {},
          operations: { evaluated_at: new Date(Date.now() - 3 * 3_600_000).toISOString(), redis_available: false },
        },
      }),
    );
    render(<MonitoringView />);
    expect(await screen.findByTestId("stale-snapshot")).toHaveTextContent("3 hours ago");
    expect(screen.getByText(/Redis was unavailable/)).toBeInTheDocument();
    expect(screen.getAllByText(/Not available/).length).toBeGreaterThan(0);
  });

  it("keeps previous data when a refresh fails, with a well-formed message", async () => {
    const summarySpy = vi.spyOn(operations, "summary").mockResolvedValueOnce(
      summary({
        latest_snapshot: {
          bucket_start: "2026-11-03T10:00:00Z",
          grain: "5min",
          job_counts: {},
          operations: { evaluated_at: recent(), pending_approvals: 7 },
        },
      }),
    );
    render(<MonitoringView />);
    const [pending] = await screen.findAllByText("Pending approvals");
    expect(pending!.parentElement).toHaveTextContent("7");
    summarySpy.mockRejectedValueOnce(new ApiError("down", 500));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(
      await screen.findByText(
        "The latest refresh failed: The API returned an error (500). Showing the previous data.",
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Pending approvals")[0]!.parentElement).toHaveTextContent("7");
  });

  it("explains when monitoring has never run", async () => {
    vi.spyOn(operations, "summary").mockResolvedValue(summary());
    render(<MonitoringView />);
    expect(await screen.findByText("Monitoring has not run yet")).toBeInTheDocument();
  });

  it("shows an error with retry, and 403 as unauthorized", async () => {
    vi.spyOn(operations, "summary").mockRejectedValueOnce(new ApiError("down", 503));
    const { unmount } = render(<MonitoringView />);
    expect(await screen.findByTestId("operations-error")).toHaveTextContent("(503)");
    unmount();
    vi.spyOn(operations, "summary").mockRejectedValueOnce(new ApiError("no", 403));
    render(<MonitoringView />);
    expect(await screen.findByTestId("operations-unauthorized")).toBeInTheDocument();
  });
});

// -------------------------------------------------------- bell and nav
describe("header bell and navigation", () => {
  it("shows the real unresolved count to owners", async () => {
    vi.spyOn(operations, "pauseStatus").mockResolvedValue(paused(0));
    vi.spyOn(operations, "summary").mockResolvedValue(summary({ open_alerts: { total: 3, by_severity: {}, by_kind: {} } }));
    render(<OperationsProvider><Header title="Alerts" /></OperationsProvider>);
    await waitFor(() => expect(screen.getByTestId("alert-bell")).toHaveAccessibleName("Alerts: 3 unresolved"));
    expect(screen.getByTestId("alert-bell")).toHaveAttribute("href", "/alerts");
    expect(screen.queryByLabelText(/4 items/)).not.toBeInTheDocument();
  });

  it("shows no count before the server answers and no bell for reviewers", async () => {
    vi.spyOn(operations, "pauseStatus").mockReturnValue(new Promise(() => {}));
    vi.spyOn(operations, "summary").mockReturnValue(new Promise(() => {}));
    const { unmount } = render(<OperationsProvider><Header title="Alerts" /></OperationsProvider>);
    expect(screen.getByTestId("alert-bell")).toHaveAccessibleName("Alerts");
    unmount();
    auth.role = "reviewer";
    render(<OperationsProvider><Header title="Alerts" /></OperationsProvider>);
    expect(screen.queryByTestId("alert-bell")).not.toBeInTheDocument();
  });

  it("lists the Operations section for owners only", () => {
    const { unmount } = render(<Sidebar />);
    expect(screen.getByRole("link", { name: "Monitoring" })).toHaveAttribute("href", "/monitoring");
    expect(screen.getByRole("link", { name: "Alerts" })).toHaveAttribute("href", "/alerts");
    unmount();
    auth.role = "reviewer";
    render(<Sidebar />);
    expect(screen.queryByRole("link", { name: "Monitoring" })).not.toBeInTheDocument();
  });
});

describe("no demo data in operator views", () => {
  it("operations components and pages never import demo fixtures", () => {
    const root = path.resolve(__dirname, "..");
    const files = [
      ...readdirSync(path.join(root, "components/operations")).map((f) => `components/operations/${f}`),
      "lib/operations.ts",
      "app/alerts/page.tsx",
      "app/alerts/[id]/page.tsx",
      "app/monitoring/page.tsx",
    ];
    for (const file of files) {
      const source = readFileSync(path.join(root, file), "utf8");
      expect(source, file).not.toMatch(/demo-data|DemoDataNotice|PlaceholderPage/);
    }
  });
});
