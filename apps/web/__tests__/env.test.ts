import { describe, expect, it } from "vitest";
import { env } from "@/lib/env";

describe("frontend env", () => {
  it("provides an API base URL", () => {
    expect(env.apiBaseUrl).toMatch(/^https?:\/\//);
  });

  it("uses the versioned API prefix", () => {
    expect(env.apiPrefix).toBe("/api/v1");
  });

  it("exposes no secret-looking values", () => {
    const serialised = JSON.stringify(env);
    expect(serialised).not.toMatch(/secret|password|sk-/i);
  });
});
