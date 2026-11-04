import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, CSRF_COOKIE, CSRF_HEADER } from "@/lib/api";
import { approvalHref, gateLabel, outcomeTone, review, reviewError } from "@/lib/review";

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(body) } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = `${CSRF_COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
});

describe("review routes", () => {
  it("sends each approval kind to the page that decides it", () => {
    const app = "a1";
    expect(approvalHref({ kind: "resume", application_id: app })).toBe("/resume-studio/a1");
    expect(approvalHref({ kind: "cover_letter", application_id: app })).toBe("/applications/a1/cover-letter");
    expect(approvalHref({ kind: "answers", application_id: app })).toBe("/applications/a1/answers");
    expect(approvalHref({ kind: "application", application_id: app })).toBe("/applications/a1");
  });
});

describe("safety-switch wording distinguishes live from dry-run", () => {
  it.each([
    [{ submission_mode: "dry_run" as const, persisted_enabled: true, live: false }, /Dry-run mode: this deployment never sends email/],
    [{ submission_mode: "enabled" as const, persisted_enabled: false, live: false }, /submission switch is off/],
    [{ submission_mode: "enabled" as const, persisted_enabled: true, live: true }, /sends a real email/],
  ])("%o", (gate, expected) => {
    expect(gateLabel(gate)).toMatch(expected);
  });

  it("only a delivered attempt is shown as success", () => {
    expect(outcomeTone("sent")).toBe("strong");
    expect(outcomeTone("ambiguous")).toBe("risk");
    expect(outcomeTone("failed")).toBe("risk");
    expect(outcomeTone("pending")).toBe("info");
  });
});

describe("errors", () => {
  it.each([
    [403, "forbidden"],
    [401, "forbidden"],
    [404, "not_found"],
    [409, "conflict"],
    [422, "invalid"],
    [429, "rate_limited"],
    [0, "failed"],
    [500, "failed"],
  ])("status %s is %s", (status, kind) => {
    expect(reviewError(new ApiError("x", status)).kind).toBe(kind);
  });

  it("uses the server's problem detail for conflicts", () => {
    const error = reviewError(new ApiError("x", 409, { detail: "Real submission is disabled" }));
    expect(error.message).toBe("Real submission is disabled");
  });

  it("never echoes an unexpected error", () => {
    expect(reviewError(new Error("SECRET stack trace")).message).not.toMatch(/SECRET/);
  });
});

describe("requests", () => {
  it("submit is a CSRF-protected POST with an empty body", async () => {
    document.cookie = `${CSRF_COOKIE}=tok`;
    const fetch = vi.fn().mockResolvedValue(response({ application_id: "a", submission_id: "s", status: "submitting", attempt: 1 }, 202));
    vi.stubGlobal("fetch", fetch);
    await review.submit("a/1");
    const [url, init] = fetch.mock.calls[0]!;
    expect(String(url)).toContain("/api/v1/applications/a%2F1/submit");
    expect(init.method).toBe("POST");
    expect(init.body).toBe("{}");
    expect(init.headers[CSRF_HEADER]).toBe("tok");
  });

  it("builds inbox queries without empty filters", async () => {
    const fetch = vi.fn().mockResolvedValue(response([]));
    vi.stubGlobal("fetch", fetch);
    await review.approvals({ status: "pending", kind: undefined, application_id: "app", limit: 50, offset: 0 });
    const url = String(fetch.mock.calls[0]![0]);
    expect(url).toContain("/api/v1/approvals?");
    expect(url).toContain("application_id=app");
    expect(url).not.toContain("kind=");
  });

  it("request changes sends the note; approval sends null for a blank note", async () => {
    const fetch = vi.fn().mockResolvedValue(response({}));
    vi.stubGlobal("fetch", fetch);
    await review.requestCoverLetterChanges("a", "Shorter please");
    await review.approveCoverLetter("a", "   ");
    expect(JSON.parse(fetch.mock.calls[0]![1].body)).toEqual({ note: "Shorter please" });
    expect(JSON.parse(fetch.mock.calls[1]![1].body)).toEqual({ decision_note: null });
  });

  it("answer edits are PATCH requests", async () => {
    const fetch = vi.fn().mockResolvedValue(response({}));
    vi.stubGlobal("fetch", fetch);
    await review.editAnswer("a", "ans", "Yes");
    const [url, init] = fetch.mock.calls[0]!;
    expect(String(url)).toContain("/applications/a/answers/ans");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).toEqual({ value: "Yes" });
  });
});
