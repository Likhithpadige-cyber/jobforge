import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { ApiError, api } from "@/lib/api";

const READY = {
  status: "ok",
  service: "jobforge",
  version: "0.1.0",
  environment: "local",
  timestamp: "2026-09-08T00:00:00Z",
  uptime_seconds: 1.5,
  checks: {
    postgres: { status: "ok", latency_ms: 1.2, error: null },
    redis: { status: "ok", latency_ms: 0.4, error: null },
  },
  migration_revision: "0001_baseline",
};

function mockFetch(body: unknown, status = 200) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  });
}

describe("api client", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", mockFetch(READY));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("fetches readiness from the versioned prefix", async () => {
    const result = await api.readiness();
    expect(result.status).toBe("ok");
    // `checks` is a Record, so an index read is possibly-undefined under
    // noUncheckedIndexedAccess. Prove the key exists, then narrow.
    const postgres = result.checks.postgres;
    expect(postgres).toBeDefined();
    if (!postgres) throw new Error("expected a postgres entry in readiness checks");
    expect(postgres.status).toBe("ok");
    expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/health"),
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("fetches liveness from the unversioned path", async () => {
    vi.stubGlobal("fetch", mockFetch({ status: "ok", service: "jobforge", version: "0.1.0" }));
    const result = await api.liveness();
    expect(result.service).toBe("jobforge");
    expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/health"),
      expect.anything(),
    );
  });

  it("returns the body on 503 instead of throwing", async () => {
    vi.stubGlobal("fetch", mockFetch({ ...READY, status: "unavailable" }, 503));
    const result = await api.readiness();
    expect(result.status).toBe("unavailable");
  });

  it("throws ApiError on a hard failure status", async () => {
    vi.stubGlobal("fetch", mockFetch({ title: "Not Found" }, 404));
    await expect(api.readiness()).rejects.toBeInstanceOf(ApiError);
  });

  it("throws a helpful ApiError when the API is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("connect ECONNREFUSED")));
    await expect(api.readiness()).rejects.toThrow(/Cannot reach the API/);
  });
});
