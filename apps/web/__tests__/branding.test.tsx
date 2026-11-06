import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Brand, BrandSymbol } from "@/components/Brand";
import { BrandLockup } from "@/components/login/BrandLockup";
import { Wordmark } from "@/components/Icon";

describe("approved brand assets", () => {
  it.each(["cream", "teal"] as const)("retains the wordmark on %s surfaces", variant => {
    const { container } = render(<Brand variant={variant} />);
    expect(screen.getByRole("img", { name: "JobForge" })).toBeVisible();
    expect(container.firstChild).toHaveClass(`brand-on-${variant}`);
  });
  it("keeps the symbol decorative and preserves its aspect ratio", () => {
    const { container } = render(<BrandSymbol />);
    const svg = container.querySelector("svg")!;
    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg).toHaveAttribute("preserveAspectRatio", "xMidYMid meet");
    expect(svg.querySelector('[data-brand-layer="connecting-ribbon"]')).toHaveAttribute("fill-opacity", "0.82");
  });
  it("uses identical ribbon paths in the login, sidebar and favicon", () => {
    const { container } = render(<><Wordmark /><BrandLockup /></>);
    const marks = container.querySelectorAll("svg");
    const paths = (svg: Element) => [...svg.querySelectorAll("path[data-brand-layer]")].map(p => p.getAttribute("d"));
    expect(paths(marks[0]!)).toEqual(paths(marks[1]!));
    const favicon = readFileSync("app/icon.svg", "utf8");
    expect([...favicon.matchAll(/ d="([^"]+)"/g)].map(m => m[1])).toEqual(paths(marks[0]!));
  });
  it.each(["cream", "teal"])("keeps the %s SVG export in sync with the shared lockup", variant => {
    const { container } = render(<Brand />);
    const paths = [...container.querySelectorAll("path")].map(p => p.getAttribute("d"));
    const exported = readFileSync(`public/brand/jobforge-on-${variant}.svg`, "utf8");
    expect([...exported.matchAll(/ d="([^"]+)"/g)].map(m => m[1])).toEqual(paths);
  });
  it("preserves the supplied reference byte-for-byte", () => {
    const original = readFileSync("../../docs/brand/approved-reference.png");
    expect(createHash("sha256").update(original).digest("hex")).toBe("7b1019e9fa443baf33f2026824c621aa2e7997f11220c25781026eed8acf6163"); // pragma: allowlist secret — public reference asset SHA-256
  });
  it.each(["Sora", "Inter"])("ships %s as local WOFF2", name => {
    expect(readFileSync(`app/fonts/${name}.woff2`).subarray(0, 4).toString()).toBe("wOF2");
  });
});
