import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";

describe("restricted palette contract", () => {
  it("contains exactly the six approved primitive colors", () => {
    const css = readFileSync("app/globals.css", "utf8");
    expect([...css.matchAll(/--jf-[\w-]+:\s*(#[\da-f]{6})/gi)].map(m => m[1]?.toUpperCase()).sort()).toEqual([
      "#0A0F14", "#0F3D3E", "#2E6F73", "#7FB3B8", "#C6601F", "#EADCC8",
    ]);
  });
  it("rejects off-palette UI paint, alpha shades and legacy raster artwork", () => {
    expect(execFileSync(process.execPath, ["scripts/audit-colors.mjs"], { encoding: "utf8" })).toContain("PASS:");
  });
});

// Exercise the audit gate with deliberately invalid, disposable fixtures.
it.each(["body { color: #ff00ff; }", "body { color: white; }", "body { color: rgb(255,0,0); }", '"bg-blue-500"', '"bg-surface/50"', "body { background: linear-gradient(red,blue); }"])("rejects invalid paint: %s", source => {
  const root = mkdtempSync(path.join(tmpdir(), "jobforge-color-audit-"));
  try {
    for (const name of ["app", "components", "lib", "public"]) mkdirSync(path.join(root, name));
    writeFileSync(path.join(root, "app/globals.css"), source);
    writeFileSync(path.join(root, "tailwind.config.ts"), "");
    expect(() => execFileSync(process.execPath, [path.resolve("scripts/audit-colors.mjs")], { cwd: root, stdio: "pipe" })).toThrow();
  } finally { rmSync(root, { recursive: true, force: true }); }
});
