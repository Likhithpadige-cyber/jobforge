import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { HealthBadge } from "@/components/HealthBadge";
import * as apiModule from "@/lib/api";

const READY: apiModule.Readiness = {
  status: "ok",
  service: "jobforge",
  version: "0.1.0",
  environment: "local",
  timestamp: "2026-09-08T00:00:00Z",
  uptime_seconds: 2,
  checks: {
    postgres: { status: "ok", latency_ms: 1.2, error: null },
    redis: { status: "ok", latency_ms: 0.4, error: null },
  },
  migration_revision: "0001_baseline",
};

afterEach(() => vi.restoreAllMocks());

describe("HealthBadge", () => {
  it("renders each dependency check", async () => {
    vi.spyOn(apiModule.api, "readiness").mockResolvedValue(READY);
    render(<HealthBadge />);
    await waitFor(() => expect(screen.getByTestId("overall-status")).toHaveTextContent("ok"));
    expect(screen.getByTestId("check-postgres")).toHaveTextContent("ok");
    expect(screen.getByTestId("check-redis")).toHaveTextContent("0.4 ms");
  });

  it("shows degraded when Redis is down but Postgres is up", async () => {
    vi.spyOn(apiModule.api, "readiness").mockResolvedValue({
      ...READY,
      status: "degraded",
      checks: {
        postgres: { status: "ok", latency_ms: 1, error: null },
        redis: { status: "unavailable", latency_ms: null, error: "ConnectionError" },
      },
    });
    render(<HealthBadge />);
    await waitFor(() =>
      expect(screen.getByTestId("overall-status")).toHaveTextContent("degraded"),
    );
  });

  it("surfaces an error when the API cannot be reached", async () => {
    vi.spyOn(apiModule.api, "readiness").mockRejectedValue(
      new apiModule.ApiError("Cannot reach the API at http://localhost:8000", 0),
    );
    render(<HealthBadge />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/Cannot reach/));
    expect(screen.getByTestId("overall-status")).toHaveTextContent("unavailable");
  });

  it("shows 'not applied' when no migration has run", async () => {
    vi.spyOn(apiModule.api, "readiness").mockResolvedValue({
      ...READY,
      migration_revision: null,
    });
    render(<HealthBadge />);
    await waitFor(() => expect(screen.getByText("not applied")).toBeInTheDocument());
  });
});
