import { describe, expect, it } from "vitest";
import { SAME_ORIGIN, resolveApiBaseUrl } from "@/lib/env";

describe("API base URL", () => {
  it("same-origin makes requests relative to the page's own origin", () => {
    expect(resolveApiBaseUrl(SAME_ORIGIN)).toBe("");
  });
  it("an explicit origin is used as-is", () => {
    expect(resolveApiBaseUrl("http://127.0.0.1:8100")).toBe("http://127.0.0.1:8100");
  });
  it("unset keeps the local development default", () => {
    expect(resolveApiBaseUrl(undefined)).toBe("http://localhost:8000");
    expect(resolveApiBaseUrl("")).toBe("http://localhost:8000");
  });
});
