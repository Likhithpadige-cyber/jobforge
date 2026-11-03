import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, CSRF_COOKIE, CSRF_HEADER, api } from "@/lib/api";
import {
  NOT_AVAILABLE,
  STALE_AFTER_MS,
  formatMetric,
  isStale,
  loadError,
  operations,
  resolutionImpact,
  series,
  snapshotAgeMs,
  type OperatorAlert,
  type Snapshot,
} from "@/lib/operations";

function response(body: unknown, status = 200, raw?: string): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => raw ?? JSON.stringify(body),
  } as Response;
}

const ALERT: OperatorAlert = {
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

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = `${CSRF_COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
});

describe("missing metrics are never zero", () => {
  it.each([undefined, null, Number.NaN, Number.POSITIVE_INFINITY])("%s is not available", (value) => {
    expect(formatMetric(value as number | null | undefined)).toBe(NOT_AVAILABLE);
  });

  it("a reported zero is shown as zero", () => {
    expect(formatMetric(0)).toBe("0");
    expect(formatMetric(1234)).toBe("1,234");
  });

  it("series keep gaps instead of inventing values", () => {
    const snapshots: Snapshot[] = [
      { bucket_start: "2026-11-03T10:10:00Z", grain: "5min", job_counts: {}, operations: { pending_approvals: 4 } },
      { bucket_start: "2026-11-03T10:00:00Z", grain: "5min", job_counts: {}, operations: null },
      { bucket_start: "2026-11-03T10:05:00Z", grain: "5min", job_counts: {}, operations: {} },
    ];
    expect(series(snapshots, (ops) => ops.pending_approvals)).toEqual([null, null, 4]);
  });
});

describe("staleness", () => {
  const now = Date.parse("2026-11-03T12:00:00Z");

  it("no evaluation time is stale and unknown", () => {
    expect(snapshotAgeMs(null, now)).toBeNull();
    expect(snapshotAgeMs({}, now)).toBeNull();
    expect(isStale(null)).toBe(true);
  });

  it("is stale strictly after fifteen minutes", () => {
    expect(isStale(STALE_AFTER_MS)).toBe(false);
    expect(isStale(STALE_AFTER_MS + 1)).toBe(true);
    expect(snapshotAgeMs({ evaluated_at: "2026-11-03T11:50:00Z" }, now)).toBe(10 * 60_000);
  });
});

describe("resolution impact is stated before the click", () => {
  it("non-pausing alerts do not touch generation", () => {
    expect(resolutionImpact({ ...ALERT, auto_paused: false }, null)).toMatch(/does not pause generation/);
  });

  it("other pausing incidents keep generation paused", () => {
    expect(resolutionImpact(ALERT, { generation_paused: true, unresolved_auto_pause_count: 3 })).toMatch(
      /stays paused: 2 other pausing incidents/,
    );
  });

  it("the last incident still waits for server confirmation", () => {
    expect(resolutionImpact(ALERT, { generation_paused: true, unresolved_auto_pause_count: 1 })).toMatch(
      /once the server confirms/,
    );
  });

  it("unknown pause state does not guess", () => {
    expect(resolutionImpact(ALERT, null)).toMatch(/re-read from the server/);
  });
});

describe("errors", () => {
  it.each([
    [403, "unauthorized"],
    [404, "not_found"],
    [0, "failed"],
    [500, "failed"],
  ])("status %s is %s", (status, kind) => {
    expect(loadError(new ApiError("x", status)).kind).toBe(kind);
  });

  it("a 503 is an error everywhere except readiness", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ detail: "down" }, 503)));
    await expect(operations.summary()).rejects.toMatchObject({ status: 503 });
    await expect(operations.pauseStatus()).rejects.toBeInstanceOf(ApiError);
    await expect(api.jobCounts()).rejects.toBeInstanceOf(ApiError);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ status: "unavailable", checks: {} }, 503)));
    await expect(api.readiness()).resolves.toMatchObject({ status: "unavailable" });
  });

  it("a non-JSON proxy error is still an ApiError", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(null, 502, "<html>Bad gateway</html>")));
    await expect(operations.summary()).rejects.toMatchObject({ status: 502 });
  });

  it("a malformed success body is an ApiError, not data", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(null, 200, "not json")));
    await expect(operations.summary()).rejects.toBeInstanceOf(ApiError);
  });
});

describe("requests", () => {
  it("builds alert queries without undefined filters", async () => {
    const fetch = vi.fn().mockResolvedValue(response([]));
    vi.stubGlobal("fetch", fetch);
    await operations.listAlerts({ status: "all", severity: "high", auto_paused: true, kind: undefined, limit: 50, offset: 0 });
    const url = String(fetch.mock.calls[0]![0]);
    expect(url).toContain("/api/v1/alerts?");
    expect(url).toContain("status=all");
    expect(url).toContain("severity=high");
    expect(url).toContain("auto_paused=true");
    expect(url).not.toContain("kind=");
  });

  it("resolution is a CSRF-protected POST to the one alert", async () => {
    document.cookie = `${CSRF_COOKIE}=token-123`;
    const fetch = vi.fn().mockResolvedValue(response({ ...ALERT, status: "resolved", resolution: "human" }));
    vi.stubGlobal("fetch", fetch);
    await operations.resolveAlert("a/1");
    const [url, init] = fetch.mock.calls[0]!;
    expect(String(url)).toContain("/api/v1/alerts/a%2F1/resolve");
    expect(init.method).toBe("POST");
    expect(init.headers[CSRF_HEADER]).toBe("token-123");
    expect(init.credentials).toBe("include");
  });
});
